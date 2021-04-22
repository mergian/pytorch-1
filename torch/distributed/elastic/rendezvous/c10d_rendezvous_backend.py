# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import binascii
import codecs
import logging
import os
from datetime import timedelta
from typing import Any, Optional, Tuple, cast

from torch.distributed import Store, TCPStore

from .api import RendezvousConnectionError, RendezvousParameters, RendezvousStateError
from .dynamic_rendezvous import RendezvousBackend, Token
from .utils import _matches_machine_hostname, parse_rendezvous_endpoint

log = logging.getLogger(__name__)


class C10dRendezvousBackend(RendezvousBackend):
    """Represents a C10d-backed rendezvous backend.

    Args:
        store:
            The :py:class:`torch.distributed.Store` instance to use to
            communicate with the C10d store.
        run_id:
            The run id of the rendezvous.
    """

    # See the explanation in the __init__ method.
    _NULL_SENTINEL = "Y2FuaW1hZGFt"

    _store: Store
    _key: str

    def __init__(self, store: Store, run_id: str) -> None:
        if not run_id:
            raise ValueError("The run id must be a non-empty string.")

        self._store = store

        self._key = "torch.rendezvous." + run_id

        # The read operation of a store blocks the caller until the specified
        # key becomes available. This behavior makes it tricky to use a store
        # as a regular key-value dictionary.
        #
        # As a workaround we initially set a sentinel value as the rendezvous
        # state. Whenever this value gets returned we treat it as a None.
        self._call_store("compare_set", self._key, "", self._NULL_SENTINEL)

    @property
    def name(self) -> str:
        """See base class."""
        return "c10d-experimental"

    @property
    def store(self) -> Store:
        """Gets the :py:class:`torch.distributed.Store` instance used to
        communicate with the C10d store."""
        return self._store

    @property
    def key(self) -> str:
        """Gets the key under which the rendezvous state is stored."""
        return self._key

    def get_state(self) -> Optional[Tuple[bytes, Token]]:
        """See base class."""
        base64_state: bytes = self._call_store("get", self._key)

        return self._decode_state(base64_state)

    def set_state(
        self, state: bytes, token: Optional[Token] = None
    ) -> Optional[Tuple[bytes, Token]]:
        """See base class."""
        base64_state_str: str = codecs.encode(state, "base64").decode()

        if token:
            # Shortcut if we know for sure that the token is not valid.
            if not isinstance(token, bytes):
                return self.get_state()

            token = token.decode()
        else:
            token = self._NULL_SENTINEL

        base64_state: bytes = self._call_store("compare_set", self._key, token, base64_state_str)

        return self._decode_state(base64_state)

    def _call_store(self, store_op: str, *args, **kwargs) -> Any:
        try:
            return getattr(self._store, store_op)(*args, **kwargs)
        except (ValueError, RuntimeError) as exc:
            raise RendezvousConnectionError(
                "The connection to the C10d store has failed. See inner exception for details."
            ) from exc

    def _decode_state(self, base64_state: bytes) -> Optional[Tuple[bytes, Token]]:
        if base64_state == self._NULL_SENTINEL.encode():
            return None
        try:
            state = codecs.decode(base64_state, "base64")
        except binascii.Error as exc:
            raise RendezvousStateError(
                "The state object is corrupt. See inner exception for details."
            ) from exc

        return state, base64_state


def _create_tcp_store(params: RendezvousParameters) -> TCPStore:
    host, port = parse_rendezvous_endpoint(params.endpoint, default_port=29500)

    cfg_is_host = params.get_as_bool("is_host")
    # If the user has explicitly specified whether our process should host the
    # the store, respect it.
    if cfg_is_host is not None:
        is_host = cfg_is_host
    # Otherwise try to determine whether we are the host based on our hostname
    # and IP address.
    else:
        is_host = _matches_machine_hostname(host)

    # The timeout
    read_timeout = cast(int, params.get_as_int("read_timeout", 60))
    if read_timeout <= 0:
        raise ValueError("The read timeout must be a positive integer.")

    # In specific cases we attempt to instantiate the store twice. For details
    # see the explanation in the except clause below.
    for is_server in [is_host, False]:
        try:
            store = TCPStore(  # type: ignore[call-arg]
                host, port, is_master=is_server, timeout=timedelta(seconds=read_timeout)
            )

            if is_server:
                log.info(
                    f"Process {os.getpid()} hosts the TCP store for the C10d rendezvous backend."
                )

            break
        except (ValueError, RuntimeError) as exc:
            # If we heuristically inferred the value of is_host as True and our
            # first attempt to instantiate the TCP store has failed, try it one
            # more time with is_host set to False. As an edge case there can be
            # more than one process that is part of the same rendezvous on this
            # machine and only one of them will eventually host the store.

            if not is_server or cfg_is_host is not None:
                raise RendezvousConnectionError(
                    "The connection to the C10d store has failed. See inner exception for details."
                ) from exc

    return store


def create_backend(params: RendezvousParameters) -> C10dRendezvousBackend:
    """Creates a new :py:class:`C10dRendezvousBackend` from the specified
    parameters.

    +--------------+-----------------------------------------------------------+
    | Parameter    | Description                                               |
    +==============+===========================================================+
    | store_type   | The type of the C10d store. As of today the only          |
    |              | supported type is "tcp" which corresponds to              |
    |              | :py:class:`torch.distributed.TCPStore`. Defaults to "tcp".|
    +--------------+-----------------------------------------------------------+
    | read_timeout | The read timeout, in seconds, for store operations.       |
    |              | Defaults to 60 seconds.                                   |
    +--------------+-----------------------------------------------------------+
    | is_host      | A boolean value indicating whether this backend instance  |
    |              | will host the C10d store. If not specified it will be     |
    |              | inferred heuristically by matching the hostname or the IP |
    |              | address of this machine against the specified rendezvous  |
    |              | endpoint. Defaults to ``None``.                           |
    |              |                                                           |
    |              | Note that this configuration option only applies to       |
    |              | :py:class:`torch.distributed.TCPStore`. In normal         |
    |              | circumstances you can safely skip it; the only time when  |
    |              | it is needed is if its value cannot be correctly          |
    |              | determined (e.g. the rendezvous endpoint has a CNAME as   |
    |              | the hostname or does not match the FQDN of the machine).  |
    +--------------+-----------------------------------------------------------+
    """
    # As of today we only support TCPStore. Other store types do not have the
    # required functionality (e.g. compare_set) yet.
    store_type = params.get("store_type", "tcp").strip().lower()
    if store_type != "tcp":
        raise ValueError("The store type must be 'tcp'. Other store types are not supported yet.")

    store = _create_tcp_store(params)

    return C10dRendezvousBackend(store, params.run_id)
