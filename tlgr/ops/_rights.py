"""The one canonical vocabulary for admin and member rights.

`chat admin promote --rights`, `chat member restrict --deny`, `chat permission
set --allow` and `chat permission list` all read this table, which is why a
right is spelled the same way in every one of them and why `chat permission
get` round-trips straight back into `chat permission set`.

Two conversions live here because getting either wrong is silent.

* **Polarity.** `ChatBannedRights` is inverted — `send_messages=True` means
  *cannot* send. Every `Rights` model tlgr emits is allow-polarity, so the
  inversion happens once, here, instead of once per caller.
* **Completeness.** `channels.editBanned` and
  `messages.editChatDefaultBannedRights` replace the whole mask: a flag you
  omit is a flag you cleared. Every writer therefore builds the *full* mask
  from a name set rather than patching the request object.

Telethon 1.44 speaks layer 227. Two layer-229 rights (`manage-linked-peers`,
`manage-welcome-messages`) have no field to set, so they are listed with
`supported=false` and refused with NOT_SUPPORTED rather than dropped — a
right that vanishes quietly is a permission bug waiting to happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tlgr.core.errors import NotSupportedError, UsageError
from tlgr.core.timefmt import fmt_dt, parse_dt, parse_duration, to_unix
from tlgr.models.admin import RightInfo
from tlgr.models.peer import Rights

__all__ = [
    "ADMIN_MASK",
    "FOREVER",
    "MEMBER_MASK",
    "MaskEdit",
    "all_allowed",
    "build_admin_rights",
    "build_banned_rights",
    "catalog",
    "denied_names",
    "granted_names",
    "model_from_admin",
    "model_from_banned",
    "parse_names",
    "parse_until",
    "read_only",
    "require_supported",
    "until_label",
]

#: Telegram treats 0, anything under 30 s and anything over 366 d as "forever".
FOREVER = 0
_MIN_BAN = 30
_MAX_BAN = 366 * 86400


@dataclass(frozen=True, slots=True)
class _Right:
    name: str
    mask: str
    tl_flag: str
    summary: str
    peer_types: tuple[str, ...] = ()
    supported: bool = True
    since_layer: int | None = None


#: The admin mask, in the order `ChatAdminRights` declares its flags.
_ADMIN: tuple[_Right, ...] = (
    _Right("change-info", "admin", "change_info", "Edit the title, photo and description"),
    _Right("post-messages", "admin", "post_messages", "Post to the channel", ("channel",)),
    _Right("edit-messages", "admin", "edit_messages", "Edit others' posts", ("channel",)),
    _Right("delete-messages", "admin", "delete_messages", "Delete others' messages"),
    _Right("ban-users", "admin", "ban_users", "Ban and restrict members", ("group", "supergroup")),
    _Right("invite-users", "admin", "invite_users", "Add members and create invite links"),
    _Right("pin-messages", "admin", "pin_messages", "Pin messages", ("group", "supergroup")),
    _Right("add-admins", "admin", "add_admins", "Promote other admins"),
    _Right("anonymous", "admin", "anonymous", "Post as the group", ("group", "supergroup")),
    _Right("manage-call", "admin", "manage_call", "Start and manage video chats"),
    _Right("other", "admin", "other", "The undocumented catch-all flag"),
    _Right("manage-topics", "admin", "manage_topics", "Create and manage topics", ("forum",)),
    _Right("post-stories", "admin", "post_stories", "Post stories", ("channel",)),
    _Right("edit-stories", "admin", "edit_stories", "Edit others' stories", ("channel",)),
    _Right("delete-stories", "admin", "delete_stories", "Delete others' stories", ("channel",)),
    _Right(
        "manage-direct-messages",
        "admin",
        "manage_direct_messages",
        "Moderate the channel's direct messages",
        ("channel",),
    ),
    _Right("manage-ranks", "admin", "manage_ranks", "Set other members' custom titles"),
    _Right(
        "manage-linked-peers",
        "admin",
        "",
        "Manage a community's linked chats",
        (),
        supported=False,
        since_layer=229,
    ),
    _Right(
        "manage-welcome-messages",
        "admin",
        "",
        "Write the chat's welcome messages",
        (),
        supported=False,
        since_layer=229,
    ),
)

#: The member mask. Stored inverted by Telegram; named in allow-polarity here.
_MEMBER: tuple[_Right, ...] = (
    _Right("view-messages", "member", "view_messages", "Read the chat at all"),
    _Right("send-messages", "member", "send_messages", "Send any message"),
    _Right("send-media", "member", "send_media", "Send media of any kind"),
    _Right("send-photos", "member", "send_photos", "Send photos"),
    _Right("send-videos", "member", "send_videos", "Send videos"),
    _Right("send-audios", "member", "send_audios", "Send music"),
    _Right("send-docs", "member", "send_docs", "Send files"),
    _Right("send-voices", "member", "send_voices", "Send voice notes"),
    _Right("send-rounds", "member", "send_roundvideos", "Send video notes"),
    _Right("send-stickers", "member", "send_stickers", "Send stickers"),
    _Right("send-gifs", "member", "send_gifs", "Send GIFs"),
    _Right("send-games", "member", "send_games", "Send games"),
    _Right("send-inline", "member", "send_inline", "Use inline bots"),
    _Right("send-polls", "member", "send_polls", "Send polls"),
    _Right("send-plain", "member", "send_plain", "Send plain text"),
    _Right("send-reactions", "member", "send_reactions", "React to messages"),
    _Right("embed-links", "member", "embed_links", "Send links with a preview"),
    _Right("change-info", "member", "change_info", "Edit the title, photo and description"),
    _Right("invite-users", "member", "invite_users", "Add members"),
    _Right("pin-messages", "member", "pin_messages", "Pin messages"),
    _Right("manage-topics", "member", "manage_topics", "Create topics", ("forum",)),
    _Right("edit-rank", "member", "edit_rank", "Change their own custom title"),
    _Right(
        "manage-linked-peers",
        "member",
        "",
        "Manage a community's linked chats",
        (),
        supported=False,
        since_layer=229,
    ),
)

ADMIN_MASK: tuple[str, ...] = tuple(right.name for right in _ADMIN)
MEMBER_MASK: tuple[str, ...] = tuple(right.name for right in _MEMBER)

_BY_MASK: dict[str, dict[str, _Right]] = {
    "admin": {right.name: right for right in _ADMIN},
    "member": {right.name: right for right in _MEMBER},
}

#: The member rights `--none` leaves alone: taking view-messages away is a
#: ban, not a restriction, and `chat member ban` is the command that says so.
_READ_ONLY_KEEP = frozenset({"view-messages"})

#: Model field per TL flag. They line up one-to-one today; the mapping exists
#: so a future rename in either direction stays a one-line change.
_MODEL_FIELD = {right.tl_flag: right.tl_flag for right in (*_ADMIN, *_MEMBER) if right.tl_flag}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_names(value: str | None, *, mask: str, field: str = "rights") -> list[str]:
    """`"ban-users, pin-messages"` → the canonical names, or a USAGE error.

    Unknown names are refused rather than ignored: silently dropping a right
    somebody asked for is how a member ends up with permissions nobody
    intended.
    """
    if not value:
        return []
    known = _BY_MASK[mask]
    out: list[str] = []
    for raw in str(value).replace(",", " ").split():
        name = raw.strip().lower().replace("_", "-")
        if not name:
            continue
        if name in ("all", "*"):
            out.extend(n for n in known if n not in out)
            continue
        if name not in known:
            raise UsageError(
                f"{name!r} is not a {mask} right; see `tlgr chat permission list --mask {mask}`",
                field=field,
            )
        if name not in out:
            out.append(name)
    return out


def require_supported(names: list[str], *, mask: str) -> None:
    """Refuse the layer-229 rights this Telethon cannot express (exit 13)."""
    known = _BY_MASK[mask]
    gaps = [name for name in names if not known[name].supported]
    if gaps:
        raise NotSupportedError(
            f"{', '.join(gaps)} {'is a' if len(gaps) == 1 else 'are'} layer-229 "
            f"{'right' if len(gaps) == 1 else 'rights'} and Telethon 1.44 speaks layer 227, "
            "so tlgr has no field to set. `chat permission list` marks them supported=false"
        )


def parse_until(value: str | None) -> int:
    """`--until 7d` → an absolute unix timestamp, with Telegram's rounding.

    0, under 30 seconds and over 366 days all mean *forever* server-side, so
    they are collapsed here rather than sent and silently reinterpreted.
    """
    if value is None:
        return FOREVER
    text = str(value).strip().lower()
    if text in ("", "0", "forever", "never", "permanent"):
        return FOREVER
    seconds = parse_duration(text)
    if seconds is not None:
        if seconds < _MIN_BAN or seconds > _MAX_BAN:
            return FOREVER
        return int(datetime.now(timezone.utc).timestamp()) + int(seconds)
    moment = parse_dt(text)
    if moment is None:
        raise UsageError(f"{value!r} is neither a duration nor a timestamp", field="until")
    stamp = to_unix(moment) or 0
    delta = stamp - int(datetime.now(timezone.utc).timestamp())
    if delta < _MIN_BAN or delta > _MAX_BAN:
        return FOREVER
    return stamp


def until_label(stamp: int) -> tuple[str | None, int | None]:
    """`(RFC-3339, unix)` for a ban expiry, or `(None, None)` for forever."""
    if not stamp:
        return None, None
    moment = datetime.fromtimestamp(stamp, tz=timezone.utc)
    return fmt_dt(moment), stamp


# ---------------------------------------------------------------------------
# TL ⇄ model
# ---------------------------------------------------------------------------


def model_from_admin(raw: Any) -> Rights | None:
    """`ChatAdminRights` → the allow-polarity `Rights` model."""
    if raw is None:
        return None
    rights = Rights()
    for right in _ADMIN:
        if not right.tl_flag:
            continue
        setattr(rights, _MODEL_FIELD[right.tl_flag], bool(getattr(raw, right.tl_flag, False)))
    return rights


def model_from_banned(raw: Any) -> Rights | None:
    """`ChatBannedRights` → `Rights`, inverted so True still means *allowed*."""
    if raw is None:
        return None
    rights = Rights()
    for right in _MEMBER:
        if not right.tl_flag:
            continue
        setattr(rights, _MODEL_FIELD[right.tl_flag], not bool(getattr(raw, right.tl_flag, False)))
    # Telethon hands back a datetime, but a mask tlgr just built carries the
    # int it will serialise; both have to read the same way in the response.
    until = getattr(raw, "until_date", None)
    if isinstance(until, int):
        until = datetime.fromtimestamp(until, tz=timezone.utc) if until else None
    if until is not None:
        rights.until = fmt_dt(until)
        rights.until_unix = to_unix(until)
    return rights


def granted_names(rights: Rights | None, *, mask: str) -> list[str]:
    """The names whose value is True, in table order."""
    if rights is None:
        return []
    table = _ADMIN if mask == "admin" else _MEMBER
    return [
        right.name
        for right in table
        if right.tl_flag and getattr(rights, _MODEL_FIELD[right.tl_flag], None) is True
    ]


def denied_names(rights: Rights | None, *, mask: str) -> list[str]:
    if rights is None:
        return []
    table = _ADMIN if mask == "admin" else _MEMBER
    return [
        right.name
        for right in table
        if right.tl_flag and getattr(rights, _MODEL_FIELD[right.tl_flag], None) is False
    ]


def build_admin_rights(names: list[str] | set[str]) -> Any:
    """The `ChatAdminRights` for exactly *names*; everything else is False."""
    from telethon.tl import types

    wanted = set(names)
    return types.ChatAdminRights(
        **{right.tl_flag: right.name in wanted for right in _ADMIN if right.tl_flag}
    )


def build_banned_rights(allowed: list[str] | set[str], *, until: int = FOREVER) -> Any:
    """The `ChatBannedRights` allowing exactly *allowed*.

    The mask is always complete, because the server replaces it wholesale.
    """
    from telethon.tl import types

    wanted = set(allowed)
    flags = {right.tl_flag: right.name not in wanted for right in _MEMBER if right.tl_flag}
    return types.ChatBannedRights(until_date=until, **flags)


def all_allowed(*, mask: str = "member") -> list[str]:
    """Every supported name in a mask — the `--all` shorthand."""
    table = _ADMIN if mask == "admin" else _MEMBER
    return [right.name for right in table if right.supported]


def read_only() -> list[str]:
    """`--none`: nothing but reading."""
    return sorted(_READ_ONLY_KEEP)


def catalog(*, mask: str = "all", grantable: dict[str, bool] | None = None) -> list[RightInfo]:
    """The vocabulary as data, for `chat permission list`."""
    tables: list[tuple[str, tuple[_Right, ...]]] = []
    if mask in ("admin", "all"):
        tables.append(("admin", _ADMIN))
    if mask in ("member", "all"):
        tables.append(("member", _MEMBER))
    out: list[RightInfo] = []
    for _label, table in tables:
        for right in table:
            out.append(
                RightInfo(
                    name=right.name,
                    mask=right.mask,
                    tl_flag=right.tl_flag or "-",
                    polarity="allow" if right.mask == "admin" else "deny",
                    supported=right.supported,
                    since_layer=right.since_layer,
                    peer_types=list(right.peer_types),
                    grantable=(grantable or {}).get(f"{right.mask}:{right.name}"),
                    summary=right.summary,
                )
            )
    return out


@dataclass
class MaskEdit:
    """A `--rights/--grant/--revoke/--all/--none` request, resolved once.

    Every rights-taking command has the same four shapes (set absolutely,
    patch, everything, nothing) and v1 would have implemented them four
    times. Resolving them here means `chat admin promote --grant ban-users`
    and `chat member restrict --allow send-media` agree about what "patch"
    means.
    """

    mask: str
    current: set[str] = field(default_factory=set)

    def resolve(
        self,
        *,
        absolute: list[str] | set[str] | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        everything: bool = False,
        nothing: bool = False,
        exclude: list[str] | None = None,
        ceiling: set[str] | None = None,
    ) -> set[str]:
        if everything:
            base = set(ceiling) if ceiling is not None else set(all_allowed(mask=self.mask))
            return base - set(exclude or ())
        if nothing:
            return set(read_only()) if self.mask == "member" else set()
        if absolute is not None:
            return set(absolute)
        out = set(self.current)
        out |= set(add or ())
        out -= set(remove or ())
        return out
