"""The plumbing the nine settings modules share.

`profile`, `privacy`, `notify`, `settings`, `business`, `premium`, `stars`,
`gift` and `giveaway` are one GUI screen each, but they keep meeting the same
four problems, and a second copy of any of them is how two commands start
disagreeing about the same server field:

* **a Stars amount is `(amount, nanos)`**, and TON arrives in the same shape
  with nine decimals — collapsing either to a float loses the digit a ledger
  reconciliation needs;
* **half of this surface replaces a whole constructor**, so the read half of
  a read-modify-write is shared rather than re-derived per flag;
* **a gift is addressed by a reference string**, and the three spellings
  (`msg:<id>`, `<peer>:<saved_id>`, a bare slug) must parse and *print* the
  same way in every module;
* **five payments methods are absent from Telethon 1.44**, and the refusal
  has to say which one and why, in one sentence, everywhere.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Any

from tlgr.core.errors import NotSupportedError, PermissionError_, UsageError
from tlgr.core.timefmt import fmt_dt, fmt_unix
from tlgr.models.peer import Peer, PeerRef
from tlgr.ops._common import client
from tlgr.ops._spec import OpContext

__all__ = [
    "ABSENT_METHODS",
    "NO_SPEND",
    "app_config",
    "client",
    "color_int",
    "color_text",
    "entity_map",
    "gift_ref_text",
    "input_gift",
    "input_user",
    "iso",
    "method_gap",
    "on_off",
    "peer_model",
    "peer_of",
    "refuse_spend",
    "resolve",
    "slug_of",
    "sound_text",
    "sound_value",
    "stars_of",
    "unix",
]

#: The one sentence every refusal to move value ends with. PR-10 settled the
#: policy for the `payment` group (`ops/payment.py`); PR-12 inherits it rather
#: than opening a second door onto the same money.
NO_SPEND = (
    "tlgr never spends money: it reads the price and refuses to sign the form. "
    "Complete the purchase in an official client if you want it"
)

#: `payments.*` methods this build has no request class for, and what each
#: one would have answered. Named here so the refusal, the docs and
#: `agent capabilities` cannot describe the gap three different ways.
ABSENT_METHODS: dict[str, str] = {
    "payments.canSendStarGift": "whether a specific recipient accepts a specific gift",
    "payments.getStarGiftCraftCandidates": "which of my gifts can be melted into a given one",
    "payments.getStarGiftAttributes": "the full attribute table of a gift type",
    "payments.getStarGiftValueInfo": "the floor price and last sale of a collectible",
    "payments.getPrepaidGiveaways": "a channel's prepaid giveaways as a list of their own",
}


def method_gap(feature: str, method: str) -> Any:
    """Refuse a feature whose only MTProto method this Telethon lacks (exit 13).

    Distinct from `_layer.py`: those are layer-229 *features* with no
    constructor at all, these are individual methods missing from a Telethon
    that otherwise speaks the surface. The command still exists and the rest
    of it still works — only the flag that needs the method refuses.
    """
    answers = ABSENT_METHODS.get(method, "")
    raise NotSupportedError(
        f"{feature} needs {method}, which Telethon 1.44 has no request class for"
        + (f" (it is what answers {answers})" if answers else "")
        + "; the rest of this command works, and the flag starts working with "
        "the Telethon uplift without its spelling changing"
    )


def refuse_spend(what: str) -> Any:
    """Refuse an operation that would move a financial asset (exit 6)."""
    raise PermissionError_(f"{what}: {NO_SPEND}")


# ---------------------------------------------------------------------------
# Peers
# ---------------------------------------------------------------------------


async def resolve(ctx: OpContext, ref: PeerRef | str | None) -> Any:
    """The `InputPeer` for *ref*, through the account's own resolver."""
    from tlgr.ops import _send

    return await _send.resolve(ctx, ref)


async def input_user(ctx: OpContext, ref: PeerRef | str | None, *, field: str = "user") -> Any:
    """The `InputUser` a `users.*`/`account.*` request wants."""
    from tlgr.ops import _bots

    return await _bots.input_user(ctx, ref, field=field)


def peer_of(peer: Any) -> int:
    """The marked id of a resolved `InputPeer`."""
    from tlgr.ops import _send

    return _send.peer_id_of(peer)


def peer_model(entity: Any) -> Peer | None:
    """A `User`/`Chat`/`Channel` as the shared `Peer` shape."""
    if entity is None:
        return None
    from tlgr.ops._serialize import entity_to_peer

    return entity_to_peer(entity)


def entity_map(result: Any) -> dict[int, Any]:
    """`{raw id: entity}` for the users *and* chats an answer carried.

    Every `payments.*` and `account.*` answer in this group ships its peers in
    two parallel vectors; a single map is what lets one lookup fill in a name
    without caring which vector it came from.
    """
    found: dict[int, Any] = {}
    for name in ("users", "chats"):
        for entity in getattr(result, name, None) or []:
            with_id = getattr(entity, "id", None)
            if with_id is not None:
                found[int(with_id)] = entity
    return found


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------


def iso(value: Any) -> str | None:
    """An RFC-3339 string for a datetime or a unix int, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return fmt_unix(int(value)) if value else None
    return fmt_dt(value)


def unix(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) or None
    from tlgr.core.timefmt import to_unix

    return to_unix(value)


def stars_of(amount: Any) -> tuple[int, int]:
    """`starsAmount` as `(amount, nanos)`.

    An int is accepted because half the payments surface still reports a bare
    Star count; the nanos are then genuinely zero rather than unknown.
    """
    if amount is None:
        return 0, 0
    if isinstance(amount, (int, float)):
        return int(amount), 0
    return int(getattr(amount, "amount", 0) or 0), int(getattr(amount, "nanos", 0) or 0)


def on_off(value: str | None, *, field: str) -> bool | None:
    """`on`/`off` as a bool, `None` for "the caller did not say"."""
    if value is None:
        return None
    text = value.strip().lower()
    if text in ("on", "true", "yes", "1"):
        return True
    if text in ("off", "false", "no", "0"):
        return False
    raise UsageError(f"--{field.replace('_', '-')} takes on or off", field=field)


def color_int(text: str | None, *, field: str = "color") -> int | None:
    """`#RRGGBB`, `0xRRGGBB` or a decimal, as the int the API wants."""
    if text is None:
        return None
    raw = str(text).strip().lstrip("#")
    if raw.lower().startswith("0x"):
        raw = raw[2:]
    try:
        return int(raw, 16) if not raw.isdigit() or len(raw) == 6 else int(raw)
    except ValueError as exc:
        raise UsageError(f"{text!r} is not a colour (use #RRGGBB)", field=field) from exc


def color_text(value: Any) -> str:
    """An int colour as `#RRGGBB`, which is how a human reads one back."""
    return f"#{int(value or 0) & 0xFFFFFF:06X}"


def sound_value(text: str | None) -> Any:
    """`default | none | local:<title> | ringtone:<id> | <id>` as a constructor."""
    from telethon.tl import types

    if text is None:
        return None
    value = text.strip()
    if value in ("none", "off", "silent"):
        return types.NotificationSoundNone()
    if value in ("default", ""):
        return types.NotificationSoundDefault()
    if value.startswith("local:"):
        title = value.split(":", 1)[1]
        return types.NotificationSoundLocal(title=title, data=title)
    if value.startswith("ringtone:"):
        value = value.split(":", 1)[1]
    try:
        return types.NotificationSoundRingtone(id=int(value))
    except ValueError as exc:
        raise UsageError(
            "--sound takes default, none, local:<title> or ringtone:<id>", field="sound"
        ) from exc


def sound_text(value: Any) -> str | None:
    """The inverse of `sound_value`, so a read can be piped into a write."""
    from tlgr.ops._serialize import _sound

    return _sound(value)


# ---------------------------------------------------------------------------
# Gift references
# ---------------------------------------------------------------------------


async def input_gift(ctx: OpContext, ref: str, *, field: str = "ref") -> Any:
    """One `inputSavedStarGift*` from tlgr's single reference spelling.

    `msg:<id>` is a gift received in a private chat, `<peer>:<saved_id>` one
    held by a channel, and anything else is a collectible slug (a `t.me/nft/`
    link is accepted and reduced to its slug). Three server constructors, one
    string a caller can copy out of a listing.
    """
    from telethon.tl import types

    text = str(ref).strip()
    if not text:
        raise UsageError("give a gift reference", field=field)
    head, sep, tail = text.partition(":")
    if sep and head.lower() in ("msg", "message"):
        if not tail.lstrip("-").isdigit():
            raise UsageError(f"{text!r}: msg:<id> wants a message id", field=field)
        return types.InputSavedStarGiftUser(msg_id=int(tail))
    if sep and tail.lstrip("-").isdigit() and not text.startswith("http"):
        return types.InputSavedStarGiftChat(peer=await resolve(ctx, head), saved_id=int(tail))
    return types.InputSavedStarGiftSlug(slug=slug_of(text))


def slug_of(text: str) -> str:
    """`t.me/nft/PlushPepe-42` → `PlushPepe-42`; a bare slug passes through."""
    value = str(text).strip()
    for marker in ("/nft/", "t.me/", "tg://nft?slug="):
        if marker in value:
            value = value.split(marker, 1)[1]
    return value.split("?", 1)[0].split("#", 1)[0].strip("/")


def gift_ref_text(raw: Any) -> str:
    """The reference string for a `savedStarGift` the server just handed us."""
    msg_id = getattr(raw, "msg_id", None)
    if msg_id:
        return f"msg:{msg_id}"
    saved_id = getattr(raw, "saved_id", None)
    if saved_id:
        return f"saved:{saved_id}"
    gift = getattr(raw, "gift", None)
    return str(getattr(gift, "slug", "") or "")


# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------


async def app_config(ctx: OpContext) -> dict[str, Any]:
    """`help.getAppConfig` as plain Python. Never hardcode a server limit."""
    from tlgr.ops import _media

    return await _media.app_config(ctx)
