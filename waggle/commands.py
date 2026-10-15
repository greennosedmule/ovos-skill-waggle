"""Waggle command builders for the hub (see WAGGLE.md "waggle.command").

Each builder returns a validated :class:`~waggle.messages.Command` with a new
request id (unless ``id`` is given) and a short human ``description``, which
the phone shows if a rule makes it ask. Bad arguments raise ``WaggleError``
with ``bad_request``, from the model's own validation.
"""
from __future__ import annotations

from typing import Optional

from waggle.messages import (
    DND_SET, RINGER_SET, TORCH_SET, VOLUME_SET, Command, new_request_id,
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
