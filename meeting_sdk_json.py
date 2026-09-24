"""meeting_sdk_json -- the wire codec for the meeting sdk apis.

The local sdk answers with values of the meeting ADT (datetimes, enums,
dataclasses out of meeting_adt, dicts, lists, sets). The remote binding has to
move those values between two processes, so both ends use these two functions:

    encode(value) -> json-safe data
    decode(value) -> the value back

A tagged value is a dict with ``__adt__`` (a kind, or the name of an
meeting_adt class); a plain dict keeps its own keys, so a dict of file states
travels as itself.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

import meeting_adt as adt

TAG = "__adt__"

__all__ = ["encode", "decode"]


def encode(value: Any) -> Any:
    """One value -> json-safe data."""
    if isinstance(value, _dt.datetime):
        return {TAG: "datetime", "iso": value.isoformat()}
    if isinstance(value, Enum):
        return {TAG: "enum", "class": type(value).__name__, "value": value.value}
    if isinstance(value, (set, frozenset)):
        return {TAG: "set", "items": [encode(item) for item in sorted(value, key=repr)]}
    if isinstance(value, tuple):
        return {TAG: "tuple", "items": [encode(item) for item in value]}
    if _is_adt_object(value):
        payload = {TAG: type(value).__name__}
        for field in fields(value):
            payload[field.name] = encode(getattr(value, field.name))
        return payload
    if isinstance(value, dict):
        return {str(key): encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"cannot put {type(value).__name__} on the wire")


def decode(value: Any) -> Any:
    """json-safe data -> the value back."""
    if isinstance(value, dict):
        if TAG in value:
            return _decode_tagged(value)
        return {key: decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode(item) for item in value]
    return value


def _decode_tagged(payload: dict) -> Any:
    kind = payload[TAG]
    if kind == "datetime":
        return _dt.datetime.fromisoformat(payload["iso"])
    if kind == "enum":
        return getattr(adt, payload["class"])(payload["value"])
    if kind == "set":
        return set(decode(item) for item in payload["items"])
    if kind == "tuple":
        return tuple(decode(item) for item in payload["items"])
    cls = getattr(adt, kind, None)
    if cls is None or not is_dataclass(cls):
        raise TypeError(f"unknown adt class on the wire: {kind}")
    return cls(**{name: decode(item) for name, item in payload.items() if name != TAG})


def _is_adt_object(value: Any) -> bool:
    return is_dataclass(value) and not isinstance(value, type) and type(value).__module__ == adt.__name__
