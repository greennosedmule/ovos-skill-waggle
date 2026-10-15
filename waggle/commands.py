"""Waggle command builders for the hub (see WAGGLE.md "waggle.command").

Each builder returns a validated :class:`~waggle.messages.Command` with a new
request id (unless ``id`` is given) and a short human ``description``, which
the phone shows if a rule makes it ask. Bad arguments raise ``WaggleError``
with ``bad_request``, from the model's own validation.
"""
from __future__ import annotations

from typing import Optional

from waggle.messages import (
    DND_SET, READ_ALOUD, REPLY, RINGER_SET, TORCH_SET, VOLUME_SET, Command, _drop_none,
    new_request_id,
)


def _command(name: str, params: dict, description: str, id: Optional[str]) -> Command:
    return Command(id=new_request_id() if id is None else id, name=name, params=params,
                   description=description)


def torch(on: bool, *, id: Optional[str] = None) -> Command:
    """Turn the flashlight on or off."""
    return _command(TORCH_SET, {"on": on}, f"Turn the flashlight {'on' if on else 'off'}", id)


def volume(stream: str = "media", *, level: Optional[int] = None, step: Optional[str] = None,
           id: Optional[str] = None) -> Command:
    """Set a stream's volume to ``level`` percent, or one step ``up`` or ``down``."""
    params = {"stream": stream}
    if level is not None:
        params["level"] = level
        description = f"Set the {stream} volume to {level}%"
    else:
        params["step"] = step
        description = f"Turn the {stream} volume {step}"
    return _command(VOLUME_SET, params, description, id)


def ringer(mode: str, *, id: Optional[str] = None) -> Command:
    """Set the ringer to ``normal``, ``vibrate`` or ``silent``."""
    return _command(RINGER_SET, {"mode": mode}, f"Set the ringer to {mode}", id)


def dnd(on: bool, *, id: Optional[str] = None) -> Command:
    """Turn Do Not Disturb on (priority only) or off."""
    return _command(DND_SET, {"on": on}, f"Turn Do Not Disturb {'on' if on else 'off'}", id)


def read_aloud(*, app: Optional[str] = None, sender: Optional[str] = None, limit: int = 5,
               id: Optional[str] = None) -> Command:
    """Have the phone read its notifications aloud itself; ``sender`` is the wire's ``from``."""
    who = f" from {sender}" if sender else ""
    return _command(READ_ALOUD, _drop_none({"app": app, "from": sender, "limit": limit}),
                    f"Read your notifications{who} aloud", id)


def reply(text: str, *, ref: Optional[str] = None, to: Optional[str] = None,
          name: Optional[str] = None, id: Optional[str] = None) -> Command:
    """Reply ``text`` to the notification ``ref``, or to the conversation matching ``to``.

    ``name`` is who it goes to, for the description the phone shows.
    """
    who = name or to or "the conversation"
    return _command(REPLY, _drop_none({"ref": ref, "to": to, "text": text}),
                    f'Reply to {who}: "{text}"', id)
