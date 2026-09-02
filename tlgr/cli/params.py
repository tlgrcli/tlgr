"""Custom Click parameter types.

Each one converts to the *model* value the daemon expects and fails as a
USAGE error naming the offending field, so a bad argument is reported the same
way whether it was caught in the CLI or over the wire.

Nothing here resolves anything. `@alice` becomes a `PeerRef`, not a user; the
network is the daemon's business (§4.3, §6.6).
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from tlgr.core.text import PARSE_MODES
from tlgr.core.timefmt import TimeFormatError, fmt_dt, parse_dt, parse_duration
from tlgr.models.peer import PeerRef, parse_message_link, parse_peer_ref, parse_user_ref

__all__ = [
    "DATETIME",
    "DURATION",
    "JSON",
    "MSGREF",
    "PARSE_MODE",
    "PATH",
    "PEER",
    "USER",
    "DateTimeParam",
    "DurationParam",
    "JsonParam",
    "MsgRefParam",
    "PathParam",
    "PeerParam",
    "UserParam",
    "for_kind",
]


class PeerParam(click.ParamType):
    """`@username`, an id, `+phone`, `me`/`saved`, or a t.me/tg:// link."""

    name = "peer"

    def convert(self, value: Any, param: Any, ctx: Any) -> Any:
        if isinstance(value, PeerRef):
            return value
        try:
            return parse_peer_ref(str(value))
        except ValueError as exc:
            self.fail(str(exc), param, ctx)


class UserParam(PeerParam):
    """`PEER` minus the forms that can only name a chat."""

    name = "user"

    def convert(self, value: Any, param: Any, ctx: Any) -> Any:
        if isinstance(value, PeerRef):
            return value
        try:
            return parse_user_ref(str(value))
        except ValueError as exc:
            self.fail(str(exc), param, ctx)


class MsgRefParam(click.ParamType):
    """A message id, or a link that carries one.

    A link is accepted anywhere a `<chat> <msg-id>` pair is expected; the
    generated command fills both and complains if they disagree (STYLE §2).
    """

    name = "msg_id"

    def convert(self, value: Any, param: Any, ctx: Any) -> Any:
        if isinstance(value, int):
            return value
        text = str(value).strip()
        try:
            return int(text)
        except ValueError:
            pass
        link = parse_message_link(text)
        if link is None:
            self.fail(f"{text!r} is neither a message id nor a message link", param, ctx)
        return link[1]


class DurationParam(click.ParamType):
    """`30s 5m 2h 7d 1w forever`. `0` is legal and means "immediately"."""

    name = "duration"

    def convert(self, value: Any, param: Any, ctx: Any) -> Any:
        if value is None or isinstance(value, int):
            return value
        try:
            return parse_duration(str(value))
        except TimeFormatError as exc:
            self.fail(str(exc), param, ctx)


class DateTimeParam(click.ParamType):
    """RFC-3339, `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM`, or a relative `-90m`/`+3d`.

    Converts to a UTC RFC-3339 string, because that is what crosses the wire;
    a naive value is read in the local zone first (COR-23).
    """

    name = "datetime"

    def convert(self, value: Any, param: Any, ctx: Any) -> Any:
        if value is None:
            return None
        try:
            parsed = parse_dt(str(value))
        except TimeFormatError as exc:
            self.fail(str(exc), param, ctx)
        return fmt_dt(parsed)


class JsonParam(click.ParamType):
    """A JSON literal, or `@path` / `-` to read it from a file or stdin."""

    name = "json"

    def convert(self, value: Any, param: Any, ctx: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value
        if text == "-":
            text = sys.stdin.read()
        elif text.startswith("@"):
            try:
                with open(text[1:], encoding="utf-8") as handle:
                    text = handle.read()
            except OSError as exc:
                self.fail(f"{text[1:]}: {exc.strerror or exc}", param, ctx)
        try:
            json.loads(text)
        except ValueError as exc:
            self.fail(f"not valid JSON: {exc}", param, ctx)
        return text


class PathParam(click.Path):
    """A filesystem path where `-` keeps its stdin/stdout meaning."""

    name = "path"

    def convert(self, value: Any, param: Any, ctx: Any) -> Any:
        if value == "-":
            return "-"
        return super().convert(value, param, ctx)


class ParseModeParam(click.Choice):
    def __init__(self) -> None:
        super().__init__(list(PARSE_MODES))


PEER = PeerParam()
USER = UserParam()
MSGREF = MsgRefParam()
DURATION = DurationParam()
DATETIME = DateTimeParam()
JSON = JsonParam()
PATH = PathParam()
PARSE_MODE = ParseModeParam()

#: `kind=` on a request field → the Click type that implements it.
_BY_KIND: dict[str, click.ParamType] = {
    "peer": PEER,
    "user": USER,
    "msg_id": MSGREF,
    "duration": DURATION,
    "datetime": DATETIME,
    "json": JSON,
    "path": PATH,
}


def for_kind(kind: str) -> click.ParamType | None:
    return _BY_KIND.get(kind)
