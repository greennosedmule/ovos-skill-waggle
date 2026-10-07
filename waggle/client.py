"""The hub side of Waggle's request/response (see WAGGLE.md "Common rules").

:class:`WaggleClient` sends a ``waggle.intent`` or ``waggle.query`` as a reply
to the message that triggered it, so HiveMind routes it to the right peer, and
waits for the response carrying the same ``id``. It works on any bus with
``on``, ``remove`` and ``emit``: ``FakeBus`` or a real ``MessageBusClient``.
"""
from __future__ import annotations

import threading
from typing import Optional

from ovos_bus_client.message import Message
from ovos_utils.log import LOG

from waggle.messages import (
    INTENT, QUERY, RESPONSE_TYPES, Intent, Query, Response, new_request_id,
)


class WaggleClient:
    """Sends Waggle requests and waits for their responses.

    Each request registers its own response handler before emitting (``FakeBus``
    answers synchronously inside ``emit``) and removes it afterwards, so
    concurrent requests from several threads don't interfere.
    """

    def __init__(self, bus):
        self.bus = bus

    def send_intent(self, origin: Optional[Message], intent: Intent,
                    timeout_s: float) -> Optional[Response]:
        """Send ``waggle.intent``; the response, or None if none came in time."""
        return self._request(origin, INTENT, intent.to_dict(), intent.id, timeout_s)

    def send_query(self, origin: Optional[Message], query: Query,
                   timeout_s: float) -> Optional[Response]:
        """Send ``waggle.query``; the response, or None if none came in time."""
        return self._request(origin, QUERY, query.to_dict(), query.id, timeout_s)

    def send_raw(self, origin: Optional[Message], msg_type: str, data: dict,
                 timeout_s: float) -> Optional[Response]:
        """Send ``data`` unvalidated, adding an ``id`` only if it has none.

        For negative tests. A response is expected to echo a string ``id`` as
        given, and any other ``id`` as ``""``.
        """
        if msg_type not in RESPONSE_TYPES:
            raise ValueError(f"not a Waggle request type: {msg_type!r}")
        data = dict(data)
        if "id" not in data:
            data["id"] = new_request_id()
        request_id = data["id"] if isinstance(data["id"], str) else ""
        return self._request(origin, msg_type, data, request_id, timeout_s)

    def _request(self, origin: Optional[Message], msg_type: str, data: dict,
                 request_id: str, timeout_s: float) -> Optional[Response]:
        response_type = RESPONSE_TYPES[msg_type]
        done = threading.Event()
        result: list[Response] = []

        def on_response(message: Message) -> None:
            payload = message.data
            if not isinstance(payload, dict) or payload.get("id") != request_id or done.is_set():
                return
            try:
                response = Response.from_dict(payload)
            except Exception as e:  # one bad response mustn't end the wait
                LOG.warning(f"Ignoring malformed {response_type} for {request_id!r}: {e}")
                return
            result.append(response)
            done.set()

        self.bus.on(response_type, on_response)
        try:
            message = origin.reply(msg_type, data) if origin is not None else Message(msg_type, data)
            self.bus.emit(message)
            if not done.wait(timeout_s):
                LOG.info(f"No {response_type} for {request_id!r} within {timeout_s}s")
                return None
            return result[0]
        finally:
            self.bus.remove(response_type, on_response)

