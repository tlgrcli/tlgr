"""The `notify` group: Settings ▸ Notifications and Sounds.

One screen in every official client, three unrelated server APIs behind it —
`account.getNotifySettings` for the scopes and the chats,
`account.getReactionsNotifySettings` for the reaction alerts,
`account.getContactSignUpNotification` for "X joined Telegram". `notify get`
and `notify set` take a *target* and pick the right one, because which RPC
answers a question is the server's business and not the caller's.

Two hazards live here.

* **`mute_until` is an absolute UNIX timestamp.** v1 computed it from the
  asyncio event loop's clock — an arbitrary monotonic origin — so "mute for
  an hour" produced a timestamp in 1970 and the chat was never muted. The
  arithmetic is `int(time.time()) + seconds`, once, in `_mute_until`.
* **`inputPeerNotifySettings` fields are optional.** A field you do not send
  is left untouched, which is why every switch here is `on|off|default` and
  `default` means *remove the exception* rather than "set it to off".

The two whole-constructor APIs — reactions and contact-joined — are
read-modify-written, like every other replace-the-world call in this PR.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Annotated, Any

from tlgr.core.errors import UsageError
from tlgr.core.pagination import PageKind
from tlgr.core.timefmt import parse_duration
from tlgr.models.base import Request
from tlgr.models.notify import (
    ExceptionsCleared,
    NotifyException,
    NotifyReset,
    NotifyTarget,
    Ringtone,
    RingtoneSaved,
)
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _settings
from tlgr.ops._common import client
from tlgr.ops._params import arg, opt
from tlgr.ops._serialize import notify_settings
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: `mute_until` for "forever". Telegram's own sentinel, and the reason the
#: field is an int rather than a duration.
FOREVER = 2**31 - 1

#: The scope words `notify get`/`notify set` accept, and the `inputNotify*`
#: class each one names.
SCOPES: dict[str, str] = {
    "private": "InputNotifyUsers",
    "users": "InputNotifyUsers",
    "groups": "InputNotifyChats",
    "chats": "InputNotifyChats",
    "channels": "InputNotifyBroadcasts",
    "broadcasts": "InputNotifyBroadcasts",
    "stories": "InputNotifyUsers",
}

#: The three targets that are not a notify scope at all.
SPECIAL = ("reactions", "contact-joined")

#: `reactionNotificationsFrom*` ⇄ the word `--messages`/`--stories` take.
FROM_WORDS = {
    "ReactionNotificationsFromContacts": "contacts",
    "ReactionNotificationsFromAll": "all",
}


def _mute_until(value: str | None) -> int | None:
    """`5m`/`2h`/`forever` as the absolute UNIX second the server wants.

    `int(time.time())`, not the event loop's clock: `loop.time()` counts from
    an arbitrary origin, and v1 used it here.
    """
    if value is None:
        return None
    text = value.strip().lower()
    if text in ("forever", "always"):
        return FOREVER
    seconds = parse_duration(text)
    if seconds is None:
        raise UsageError("--mute takes a duration (30s, 5m, 2h, 7d) or 'forever'", field="mute")
    return int(time.time()) + int(seconds)


def _scope_tl(name: str) -> Any:
    from telethon.tl import types

    return getattr(types, SCOPES[name])()


def _from_tl(word: str | None, *, field: str) -> Any:
    """`contacts|all|off` as a `ReactionNotificationsFrom*`, or None for off."""
    from telethon.tl import types

    if word is None:
        return ...
    text = word.strip().lower()
    if text in ("off", "none", "nobody"):
        return None
    if text == "contacts":
        return types.ReactionNotificationsFromContacts()
    if text == "all":
        return types.ReactionNotificationsFromAll()
    raise UsageError(f"--{field} takes contacts, all or off", field=field)


async def _target(ctx: OpContext, target: str, topic: int | None) -> tuple[str, Any, int | None]:
    """`(kind, inputNotifyPeer or None, chat id)` for a target word or a chat ref."""
    from telethon.tl import types

    name = target.strip().lower()
    if name in SPECIAL:
        return name, None, None
    if name in SCOPES:
        return "scope", _scope_tl(name), None
    peer = await _settings.resolve(ctx, target)
    chat_id = _settings.peer_of(peer)
    notify = (
        types.InputNotifyForumTopic(peer=peer, top_msg_id=topic)
        if topic is not None
        else types.InputNotifyPeer(peer=peer)
    )
    return "peer", notify, chat_id


def _fill(model: NotifyTarget, raw: Any) -> NotifyTarget:
    """Copy a `peerNotifySettings` onto the flat answer, tri-state intact."""
    settings = notify_settings(raw)
    model.settings = settings
    if settings is None:
        return model
    model.muted = settings.muted
    model.mute_until = settings.mute_until
    model.mute_until_unix = settings.mute_until_unix
    model.show_previews = settings.show_previews
    model.sound = settings.sound
    model.stories_muted = settings.stories_muted
    model.stories_hide_sender = settings.stories_hide_sender
    model.stories_sound = settings.stories_sound
    return model


# ---------------------------------------------------------------------------
# notify get / set
# ---------------------------------------------------------------------------


class GetReq(Request):
    target: Annotated[
        str,
        arg(
            0,
            metavar="TARGET",
            help="private | groups | channels | stories | reactions | contact-joined | <chat>",
        ),
    ]
    topic: Annotated[int | None, opt("--topic", metavar="ID", help="A forum topic id.")] = None


async def get(ctx: OpContext, req: GetReq) -> NotifyTarget:
    """Read notification settings for a scope, a chat, a topic, reactions
    or the contact-joined toggle.

    The `sound` is normalised to `default | none | local:<title> |
    ringtone:<id>` — the same vocabulary `notify set --sound` accepts — even
    though `peerNotifySettings` carries three per-platform sound fields and
    the input constructor takes exactly one.
    """
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    kind, notify, chat_id = await _target(ctx, req.target, req.topic)
    model = NotifyTarget(target=req.target, kind=kind, chat_id=chat_id, topic=req.topic)

    if kind == "reactions":
        raw = await handle(fn.GetReactionsNotifySettingsRequest())
        model.messages_from = FROM_WORDS.get(
            type(getattr(raw, "messages_notify_from", None)).__name__, "off"
        )
        model.stories_from = FROM_WORDS.get(
            type(getattr(raw, "stories_notify_from", None)).__name__, "off"
        )
        model.poll_votes_from = FROM_WORDS.get(
            type(getattr(raw, "poll_votes_notify_from", None)).__name__, "off"
        )
        model.show_previews = getattr(raw, "show_previews", None)
        model.sound = _settings.sound_text(getattr(raw, "sound", None))
        return model

    if kind == "contact-joined":
        raw = await handle(fn.GetContactSignUpNotificationRequest())
        # Stored inverted on the wire: `silent=true` means the notification
        # is OFF, which is exactly the sort of double negative a CLI should
        # absorb rather than pass on.
        model.contact_joined = not bool(raw)
        return model

    return _fill(model, await handle(fn.GetNotifySettingsRequest(peer=notify)))


SPEC_GET = OperationSpec(
    id="notify.get",
    request=GetReq,
    response=NotifyTarget,
    impl=get,
    summary="Read notification settings for a scope, chat, topic, reactions or contact-joined",
    description=(
        "One command over three server APIs, because the GUI presents them as "
        "one Notifications screen. `contact-joined` is reported the way a "
        "human reads it: `true` means the notification is on, even though the "
        "wire stores the opposite."
    ),
    idempotent=True,
    columns=("target", "muted", "mute_until", "show_previews", "sound"),
    headers=("Target", "Muted", "Until", "Previews", "Sound"),
    example={
        "target": "private",
        "kind": "scope",
        "muted": False,
        "show_previews": True,
        "sound": "default",
    },
    example_args="notify get private",
    covers=(
        "dialogs.reactions-notify",
        "notify.contact-joined",
        "notify.peer",
        "notify.scope-channels",
        "notify.scope-groups",
        "notify.scope-private",
        "notify.stories",
    ),
    covers_partial=("notify.forum-topic", "notify.reactions", "notify.sound-selection"),
    coverage_note="Writing any of them is `notify set`; the sound list is `notify ringtone list`.",
    tags=frozenset({"agent-safe"}),
)


class SetReq(Request):
    target: Annotated[
        str,
        arg(
            0,
            metavar="TARGET",
            help="private | groups | channels | stories | reactions | contact-joined | <chat>",
        ),
    ]
    mute: Annotated[
        str | None, opt("--mute", metavar="FOR", help="Mute for this long, or 'forever'.")
    ] = None
    unmute: Annotated[bool, opt("--unmute", help="Unmute (mute_until = 0).")] = False
    preview: Annotated[
        str | None, opt("--preview", metavar="ON|OFF", help="Message text in notifications.")
    ] = None
    sound: Annotated[
        str | None,
        opt("--sound", metavar="SOUND", help="default | none | local:<title> | ringtone:<id>."),
    ] = None
    topic: Annotated[int | None, opt("--topic", metavar="ID", help="A forum topic id.")] = None
    stories_mute: Annotated[
        str | None, opt("--stories-mute", metavar="ON|OFF", help="Mute this peer's stories.")
    ] = None
    stories_hide_sender: Annotated[
        str | None,
        opt("--stories-hide-sender", metavar="ON|OFF", help="Hide the author on story alerts."),
    ] = None
    stories_sound: Annotated[
        str | None, opt("--stories-sound", metavar="SOUND", help="Sound for story alerts.")
    ] = None
    messages: Annotated[
        str | None,
        opt("--messages", metavar="WHO", help="reactions: contacts|all|off for message reactions."),
    ] = None
    stories: Annotated[
        str | None, opt("--stories", metavar="WHO", help="reactions: story-reaction alerts.")
    ] = None
    poll_votes: Annotated[
        str | None, opt("--poll-votes", metavar="WHO", help="reactions: poll-vote alerts.")
    ] = None
    on: Annotated[bool, opt("--on", help="contact-joined: enable the notification.")] = False
    off: Annotated[bool, opt("--off", help="contact-joined: disable the notification.")] = False


async def set_(ctx: OpContext, req: SetReq) -> NotifyTarget:
    """Change notification settings for a scope, chat, topic, reactions or
    the contact-joined toggle.

    For a scope or a chat only the named fields are sent, because
    `inputPeerNotifySettings` leaves an omitted field alone — that is what
    makes "mute this chat" not also reset its sound. The reactions and
    contact-joined APIs replace their whole constructor, so those two are
    read first and written back complete.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    kind, notify, chat_id = await _target(ctx, req.target, req.topic)
    changed: list[str] = []

    if kind == "contact-joined":
        if req.on == req.off:
            raise UsageError("contact-joined takes --on or --off", field="on")
        await handle(fn.SetContactSignUpNotificationRequest(silent=req.off))
        ctx.emit("notify_set", {"target": "contact-joined", "enabled": req.on})
        return NotifyTarget(
            target=req.target, kind=kind, contact_joined=req.on, changed=["contact_joined"]
        )

    if kind == "reactions":
        current = await handle(fn.GetReactionsNotifySettingsRequest())
        values: dict[str, Any] = {
            "messages_notify_from": getattr(current, "messages_notify_from", None),
            "stories_notify_from": getattr(current, "stories_notify_from", None),
            "poll_votes_notify_from": getattr(current, "poll_votes_notify_from", None),
            "sound": getattr(current, "sound", None) or types.NotificationSoundDefault(),
            "show_previews": bool(getattr(current, "show_previews", False)),
        }
        for flag, field in (
            ("messages", "messages_notify_from"),
            ("stories", "stories_notify_from"),
            ("poll_votes", "poll_votes_notify_from"),
        ):
            value = _from_tl(getattr(req, flag), field=flag)
            if value is not ...:
                values[field] = value
                changed.append(field)
        if req.sound is not None:
            values["sound"] = _settings.sound_value(req.sound)
            changed.append("sound")
        preview = _settings.on_off(req.preview, field="preview")
        if preview is not None:
            values["show_previews"] = preview
            changed.append("show_previews")
        if not changed:
            raise UsageError(
                "nothing to change: pass --messages, --stories, --poll-votes, --sound or --preview",
                field="messages",
            )
        await handle(
            fn.SetReactionsNotifySettingsRequest(settings=types.ReactionsNotifySettings(**values))
        )
        ctx.emit("notify_set", {"target": "reactions", "changed": changed})
        result = await get(ctx, GetReq(target=req.target))
        result.changed = changed
        return result

    kwargs: dict[str, Any] = {}
    if req.unmute:
        kwargs["mute_until"] = 0
        changed.append("mute_until")
    elif req.mute is not None:
        kwargs["mute_until"] = _mute_until(req.mute)
        changed.append("mute_until")
    for flag, field in (
        ("preview", "show_previews"),
        ("stories_mute", "stories_muted"),
        ("stories_hide_sender", "stories_hide_sender"),
    ):
        value = _settings.on_off(getattr(req, flag), field=flag)
        if value is not None:
            kwargs[field] = value
            changed.append(field)
    if req.sound is not None:
        kwargs["sound"] = _settings.sound_value(req.sound)
        changed.append("sound")
    if req.stories_sound is not None:
        kwargs["stories_sound"] = _settings.sound_value(req.stories_sound)
        changed.append("stories_sound")
    if not changed:
        raise UsageError(
            "nothing to change: pass --mute, --unmute, --preview or --sound", field="mute"
        )

    await handle(
        fn.UpdateNotifySettingsRequest(
            peer=notify, settings=types.InputPeerNotifySettings(**kwargs)
        )
    )
    ctx.emit("notify_set", {"target": req.target, "chat_id": chat_id, "changed": changed})
    result = await get(ctx, GetReq(target=req.target, topic=req.topic))
    result.changed = changed
    return result


SPEC_SET = OperationSpec(
    id="notify.set",
    request=SetReq,
    response=NotifyTarget,
    impl=set_,
    summary="Change notification settings for a scope, chat, topic, reactions or contact-joined",
    description=(
        "`mute_until` is an absolute UNIX timestamp; `--mute 2h` is turned "
        "into one from the wall clock, which is the bug v1 had (it used the "
        "event loop's clock and muted nothing). v1's `chat mute` is still its "
        "own operation and keeps that path; this is the scope-and-target form."
    ),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("target", "muted", "mute_until", "show_previews", "sound"),
    headers=("Target", "Muted", "Until", "Previews", "Sound"),
    example={"target": "private", "muted": True, "mute_until": "2026-09-04T12:00:00Z"},
    example_args="notify set private --mute 2h",
    covers=(
        "dialogs.notify-scope-defaults",
        "gifts.channel-notifications",
        "notify.forum-topic",
        "notify.reactions",
        "notify.sound-selection",
        "stories.notify-global",
        "stories.notify-peer",
        "stories.notify-reactions",
    ),
    covers_partial=(
        "notify.contact-joined",
        "notify.peer",
        "notify.scope-channels",
        "notify.scope-groups",
        "notify.scope-private",
        "notify.stories",
    ),
    coverage_note="Reading any of them back is `notify get`.",
)


# ---------------------------------------------------------------------------
# notify exception list / clear
# ---------------------------------------------------------------------------


class ExceptionListReq(Request):
    scope: Annotated[
        str | None, opt("--scope", metavar="SCOPE", help="private | groups | channels.")
    ] = None
    compare_sound: Annotated[
        bool, opt("--compare-sound", help="Count a differing sound as an exception.")
    ] = False
    compare_stories: Annotated[
        bool, opt("--compare-stories", help="Count differing story settings as an exception.")
    ] = False


def _scope_of(chat_id: int) -> str:
    """Which scope a chat inherits from, decided from its marked id."""
    if chat_id > 0:
        return "private"
    return "channels" if str(chat_id).startswith("-100") else "groups"


async def exception_list(ctx: OpContext, req: ExceptionListReq) -> Page[NotifyException]:
    """Chats whose notification settings differ from their scope default.

    The server answers with an `Updates` container rather than a list: the
    exceptions arrive as `updateNotifySettings` entries alongside the users
    and chats vectors, so the rows are assembled from the updates and named
    from the vectors.
    """
    from telethon.tl.functions import account as fn

    result = await client(ctx)(
        fn.GetNotifyExceptionsRequest(
            compare_sound=req.compare_sound or None,
            compare_stories=req.compare_stories or None,
        )
    )
    known = _settings.entity_map(result)
    rows: list[NotifyException] = []
    for update in getattr(result, "updates", None) or []:
        peer = getattr(getattr(update, "peer", None), "peer", None)
        if peer is None:
            continue
        from tlgr.ops._serialize import peer_id_of

        chat_id = peer_id_of(peer)
        if chat_id is None:
            continue
        settings = notify_settings(getattr(update, "notify_settings", None))
        entity = known.get(abs(chat_id) if chat_id < 0 else chat_id)
        scope = _scope_of(chat_id)
        if req.scope and scope != req.scope.strip().lower():
            continue
        rows.append(
            NotifyException(
                chat_id=chat_id,
                chat=_settings.peer_model(entity),
                title=str(
                    getattr(entity, "title", None) or getattr(entity, "first_name", "") or ""
                ),
                muted=bool(settings and settings.muted),
                mute_until=settings.mute_until if settings else None,
                mute_until_unix=settings.mute_until_unix if settings else None,
                show_previews=settings.show_previews if settings else None,
                sound=settings.sound if settings else None,
                stories_muted=settings.stories_muted if settings else None,
                scope=scope,
            )
        )
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_EXCEPTION_LIST = OperationSpec(
    id="notify.exception.list",
    request=ExceptionListReq,
    response=Page[NotifyException],
    impl=exception_list,
    summary="List chats whose notification settings differ from their scope default",
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("chat_id", "title", "muted", "mute_until", "sound", "scope"),
    headers=("Chat", "Title", "Muted", "Until", "Sound", "Scope"),
    example={
        "items": [{"chat_id": -1001, "title": "Noisy group", "muted": True, "scope": "groups"}],
        "has_more": False,
    },
    example_args="notify exception list",
    covers=("dialogs.notify-exceptions", "stories.notify-exceptions"),
    covers_partial=("notify.exceptions-list",),
    coverage_note="Dropping an exception is `notify exception clear`.",
    tags=frozenset({"agent-safe"}),
)


class ExceptionClearReq(Request):
    chat: Annotated[
        tuple[PeerRef, ...],
        arg(0, metavar="CHAT", required=False, variadic=True, kind="peer", help="Chats to reset."),
    ] = ()
    every: Annotated[bool, opt("--every", help="Clear every exception in --scope.")] = False
    scope: Annotated[str | None, opt("--scope", metavar="SCOPE", help="Scope for --every.")] = None


async def exception_clear(ctx: OpContext, req: ExceptionClearReq) -> ExceptionsCleared:
    """Drop per-chat overrides so those chats follow their scope default.

    There is no "delete exception" method: an empty `inputPeerNotifySettings`
    is what removes one, because every field of it is optional and an unset
    field means "inherit".
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    targets: list[int] = []
    peers: list[Any] = []
    for ref in req.chat:
        peer = await _settings.resolve(ctx, ref)
        peers.append(peer)
        targets.append(_settings.peer_of(peer))

    if req.every:
        rows = await exception_list(ctx, ExceptionListReq(scope=req.scope))
        for row in rows.items:
            if row.chat_id in targets:
                continue
            peers.append(await _settings.resolve(ctx, str(row.chat_id)))
            targets.append(row.chat_id)
    elif not peers:
        raise UsageError("give one or more chats, or --every --scope <scope>", field="chat")

    if not peers:
        return ExceptionsCleared(cleared=0, scope=req.scope, already=True)
    for peer in peers:
        await handle(
            fn.UpdateNotifySettingsRequest(
                peer=types.InputNotifyPeer(peer=peer),
                settings=types.InputPeerNotifySettings(),
            )
        )
    ctx.emit("notify_exceptions_cleared", {"chat_ids": targets})
    return ExceptionsCleared(cleared=len(targets), chat_ids=targets, scope=req.scope)


SPEC_EXCEPTION_CLEAR = OperationSpec(
    id="notify.exception.clear",
    request=ExceptionClearReq,
    response=ExceptionsCleared,
    impl=exception_clear,
    summary="Drop per-chat notification overrides so the chats follow their scope default",
    mutating=True,
    idempotent=True,
    rate_class="bulk",
    columns=("cleared", "chat_ids", "scope"),
    headers=("Cleared", "Chats", "Scope"),
    example={"cleared": 2, "chat_ids": [-1001, 777123]},
    example_args="notify exception clear @noisy",
    covers=("notify.exceptions-list",),
    covers_partial=("notify.peer",),
    coverage_note="Setting one chat's exception is `notify set <chat>`.",
)


# ---------------------------------------------------------------------------
# notify reset
# ---------------------------------------------------------------------------


class ResetReq(Request):
    pass


async def reset(ctx: OpContext, req: ResetReq) -> NotifyReset:
    """Reset every notification setting — scopes and per-chat — to the defaults.

    Irreversible in the only sense that matters: the exceptions are gone and
    the server does not say what they were. `notify exception list` before
    running this is the backup.
    """
    from telethon.tl.functions import account as fn

    await client(ctx)(fn.ResetNotifySettingsRequest())
    ctx.emit("notify_reset", {})
    return NotifyReset(ok=True)


SPEC_RESET = OperationSpec(
    id="notify.reset",
    request=ResetReq,
    response=NotifyReset,
    impl=reset,
    summary="Reset every notification setting (scopes and per-chat) to Telegram's defaults",
    aliases=("notify.reset-all",),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("ok",),
    headers=("OK",),
    example={"ok": True},
    example_args="notify reset",
    covers=("notify.reset-all",),
)


# ---------------------------------------------------------------------------
# notify ringtone list / set
# ---------------------------------------------------------------------------


class RingtoneListReq(Request):
    pass


async def ringtone_list(ctx: OpContext, req: RingtoneListReq) -> Page[Ringtone]:
    """Saved notification sounds, with the ids `notify set --sound` takes."""
    from telethon.tl.functions import account as fn

    result = await client(ctx)(fn.GetSavedRingtonesRequest(hash=0))
    rows = [
        Ringtone(
            id=int(getattr(document, "id", 0) or 0),
            access_hash=getattr(document, "access_hash", None),
            file_name=next(
                (
                    str(getattr(attribute, "file_name", ""))
                    for attribute in getattr(document, "attributes", None) or []
                    if type(attribute).__name__ == "DocumentAttributeFilename"
                ),
                "",
            ),
            mime_type=str(getattr(document, "mime_type", "") or ""),
            size=int(getattr(document, "size", 0) or 0),
            duration=next(
                (
                    int(getattr(attribute, "duration", 0) or 0)
                    for attribute in getattr(document, "attributes", None) or []
                    if type(attribute).__name__ == "DocumentAttributeAudio"
                ),
                None,
            ),
        )
        for document in getattr(result, "ringtones", None) or []
    ]
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_RINGTONE_LIST = OperationSpec(
    id="notify.ringtone.list",
    request=RingtoneListReq,
    response=Page[Ringtone],
    impl=ringtone_list,
    summary="List saved notification sounds",
    description="The `id` of a row is what `notify set --sound ringtone:<id>` takes.",
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("id", "file_name", "mime_type", "size", "duration"),
    headers=("Id", "File", "Type", "Bytes", "Seconds"),
    example={
        "items": [{"id": 8811, "file_name": "chime.ogg", "mime_type": "audio/ogg", "size": 20480}],
        "has_more": False,
    },
    example_args="notify ringtone list",
    covers=("notify.ringtones-list",),
    tags=frozenset({"agent-safe"}),
)


class RingtoneSetReq(Request):
    file: Annotated[
        str | None,
        arg(0, metavar="FILE", required=False, kind="path", help="MP3 or OGG/OPUS to upload."),
    ] = None
    from_message: Annotated[
        str | None,
        opt("--from-message", metavar="CHAT:ID", help="Save a voice message as a ringtone."),
    ] = None
    remove: Annotated[
        str | None, opt("--remove", metavar="ID", help="Document id of a saved ringtone.")
    ] = None


async def ringtone_set(ctx: OpContext, req: RingtoneSetReq) -> RingtoneSaved:
    """Upload a notification sound, save an existing voice message, or remove one.

    Saving an *existing* document can hand back a different one:
    `account.savedRingtoneConverted` carries a NEW document id, and using the
    old one afterwards fails. `converted: true` says that happened, and `id`
    is always the id that now works.
    """
    import mimetypes

    from telethon.tl import types
    from telethon.tl.functions import account as fn

    handle = client(ctx)

    if req.remove:
        if not req.remove.strip().isdigit():
            raise UsageError("--remove wants a saved ringtone's document id", field="remove")
        document = await _saved_ringtone(ctx, int(req.remove))
        await handle(fn.SaveRingtoneRequest(id=document, unsave=True))
        return RingtoneSaved(id=int(req.remove), removed=True)

    if req.from_message:
        from tlgr.ops import _media

        chat, _, msg_id = req.from_message.rpartition(":")
        if not chat or not msg_id.strip().lstrip("-").isdigit():
            raise UsageError("--from-message wants '<chat>:<msg_id>'", field="from_message")
        peer = await _settings.resolve(ctx, chat)
        message = await _media.fetch_message(ctx, peer, int(msg_id))
        document = _media.input_document(_media.document_of(getattr(message, "media", None)))
        answer = await handle(fn.SaveRingtoneRequest(id=document, unsave=False))
        converted = type(answer).__name__ == "AccountSavedRingtoneConverted"
        new_document = getattr(answer, "document", None)
        return RingtoneSaved(
            id=int(getattr(new_document, "id", 0) or 0) or int(msg_id),
            converted=converted,
        )

    if not req.file:
        raise UsageError("give a FILE, --from-message or --remove", field="file")
    path = Path(os.path.expanduser(req.file))
    if not path.exists():
        raise UsageError(f"{req.file} does not exist", field="file")
    limits = await _settings.app_config(ctx)
    size_max = int(limits.get("ringtone_size_max") or 0)
    if size_max and path.stat().st_size > size_max:
        raise UsageError(
            f"{path.name} is larger than the server's ringtone_size_max ({size_max} bytes)",
            field="file",
        )
    upload = getattr(ctx, "upload_file", None)
    if upload is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this context cannot upload files")
    uploaded = await handle(
        fn.UploadRingtoneRequest(
            file=await upload(path),
            file_name=path.name,
            mime_type=mimetypes.guess_type(path.name)[0] or "audio/mpeg",
        )
    )
    document = types.InputDocument(
        id=getattr(uploaded, "id", 0),
        access_hash=getattr(uploaded, "access_hash", 0),
        file_reference=getattr(uploaded, "file_reference", b"") or b"",
    )
    answer = await handle(fn.SaveRingtoneRequest(id=document, unsave=False))
    ctx.emit("ringtone_saved", {"file_name": path.name})
    return RingtoneSaved(
        id=int(getattr(uploaded, "id", 0) or 0),
        file_name=path.name,
        converted=type(answer).__name__ == "AccountSavedRingtoneConverted",
    )


async def _saved_ringtone(ctx: OpContext, document_id: int) -> Any:
    """The `InputDocument` for a saved ringtone, with its live file reference."""
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    result = await client(ctx)(fn.GetSavedRingtonesRequest(hash=0))
    for document in getattr(result, "ringtones", None) or []:
        if int(getattr(document, "id", 0) or 0) == document_id:
            return types.InputDocument(
                id=document.id,
                access_hash=document.access_hash,
                file_reference=getattr(document, "file_reference", b"") or b"",
            )
    raise UsageError(f"{document_id} is not a saved ringtone", field="remove")


SPEC_RINGTONE_SET = OperationSpec(
    id="notify.ringtone.set",
    request=RingtoneSetReq,
    response=RingtoneSaved,
    impl=ringtone_set,
    summary="Upload a notification sound, save a voice message as one, or remove one",
    description=(
        "Saving an existing document may return a *converted* one with a new "
        "id; `converted: true` says so and `id` is always the usable one."
    ),
    mutating=True,
    rate_class="file",
    timeout_s=300,
    columns=("id", "file_name", "converted", "removed"),
    headers=("Id", "File", "Converted", "Removed"),
    example={"id": 8811, "file_name": "chime.ogg", "converted": False},
    example_args="notify ringtone set chime.ogg",
    covers=(
        "notify.ringtone-remove",
        "notify.ringtone-upload",
        "ringtone.manage",
        "ringtone.set-for-chat",
    ),
)

__all__ = [name for name in dir() if name.startswith("SPEC_")]
