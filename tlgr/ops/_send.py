"""The send path, in one place: options, entities, reply targets, media.

`message send`, `message forward`, `message edit` and `draft set` all build the
same three things — a resolved peer, a `(text, entities)` pair and a reply
target — and v1 built each of them differently in each command. Everything
here is shared so that `--parse`, `--quote`, `--topic` and `--schedule` mean
exactly one thing across the surface.

**Telethon is imported inside functions, never at module scope.** `tlgr --help`
imports the registry, which imports every op module; a module-level
`import telethon` would put a 200 ms import and a hard dependency in front of
`tlgr --version` on a machine that has never connected to Telegram (§2.2).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from tlgr.core.errors import NotSupportedError, UsageError
from tlgr.core.text import default_parse_mode, entities_from_json, parse_text, utf16_len
from tlgr.core.timefmt import parse_dt
from tlgr.models.base import Request
from tlgr.models.message import Message, MessageEntity
from tlgr.models.peer import PeerRef
from tlgr.ops._params import choice, opt
from tlgr.ops._serialize import marked_id, message_to_model
from tlgr.ops._spec import OpContext

__all__ = [
    "MAX_TEXT_UTF16",
    "SCHEDULE_ONLINE",
    "SendOptions",
    "body",
    "input_media",
    "message_from_updates",
    "messages_from_updates",
    "peer_id_of",
    "reply_target",
    "resolve",
    "schedule_at",
    "split_text",
    "tl_entities",
    "typing_seconds",
]

#: `schedule_date` sentinel meaning "when the recipient is next online".
SCHEDULE_ONLINE = 0x7FFFFFFE

#: Telegram's own limit, counted in UTF-16 units like every other offset.
MAX_TEXT_UTF16 = 4096

_REPEAT_PERIODS = {
    "daily": 86400,
    "weekly": 604800,
    "biweekly": 1209600,
    "monthly": 2592000,
    "quarterly": 7776000,
    "halfyearly": 15552000,
    "yearly": 31536000,
}


class SendOptions(Request, kw_only=True):
    """The send-time options STYLE §3 gives every "send something" command.

    A base class rather than a nested struct: the CLI generator maps one
    request field to one flag, so nesting would have produced `--options-silent`.
    """

    silent: Annotated[bool, opt("--silent", help="Send without a notification sound.")] = False
    schedule: Annotated[
        str | None,
        opt(
            "--schedule",
            metavar="TS|online",
            help="Send later; 'online' means when the recipient is next online.",
        ),
    ] = None
    repeat: Annotated[
        str | None,
        choice(*_REPEAT_PERIODS, help="Repeating schedule (Premium)."),
    ] = None
    topic: Annotated[
        int | None,
        opt("--topic", metavar="ID", kind="msg_id", help="Send inside a forum topic."),
    ] = None
    send_as: Annotated[
        PeerRef | None,
        opt("--send-as", metavar="PEER", kind="peer", help="Post as a channel or anonymously."),
    ] = None
    effect: Annotated[
        str | None,
        opt("--effect", metavar="ID", help="Animated message effect (private chats)."),
    ] = None
    protect: Annotated[
        bool,
        opt("--protect", "--noforwards", help="noforwards: block forwarding and saving."),
    ] = False
    paid_stars: Annotated[
        int | None,
        opt("--paid-stars", metavar="N", help="Agree to pay N Stars per message."),
    ] = None
    quick_reply: Annotated[
        str | None,
        opt("--quick-reply", metavar="SHORTCUT", help="Send through a business quick reply."),
    ] = None
    typing: Annotated[
        float,
        opt("--typing", metavar="SECONDS", help="Show a typing action first.", ge=0),
    ] = 0.0
    typing_auto: Annotated[
        bool,
        opt("--typing-auto", help="Type for a duration estimated from the text length."),
    ] = False


# ---------------------------------------------------------------------------
# Peers
# ---------------------------------------------------------------------------


async def resolve(ctx: OpContext, ref: PeerRef | str | None) -> Any:
    """The `InputPeer` for *ref* through the account's own resolver (§6.6).

    Never `client.get_input_entity` directly: the resolver is what makes the
    NOT_FOUND / INDETERMINATE distinction, and it is per account because an
    access hash minted for one account is meaningless to another.
    """
    if ref is None:
        raise UsageError("a chat is required", field="chat")
    resolver = getattr(ctx, "resolver", None)
    if resolver is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("no peer resolver is available in this context")
    return await resolver.resolve(ref)


def peer_id_of(peer: Any) -> int:
    """The marked id of an `InputPeer`, or 0 when it cannot be determined."""
    from telethon import utils

    try:
        return int(utils.get_peer_id(peer))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def _stdin_text() -> str:
    if sys.stdin is None or sys.stdin.isatty():
        raise UsageError("'-' was given for the text but stdin is a terminal", field="text")
    return sys.stdin.read()


def body(
    text: str | None,
    *,
    parse: str | None = None,
    entities: str | None = None,
    stdin: bool = False,
) -> tuple[str, list[MessageEntity]]:
    """`(plain text, entities)` from what the caller typed.

    `--entities` wins over `--parse` for the runs it names and is *merged*
    with them otherwise, because the common case is markdown plus one custom
    emoji that markdown cannot express.
    """
    raw = text or ""
    if stdin or raw == "-":
        raw = _stdin_text()
    mode = parse if parse is not None else default_parse_mode()
    plain, parsed = parse_text(raw, mode)
    if entities:
        explicit = entities_from_json(entities)
        kinds = {e.type for e in explicit}
        parsed = [e for e in parsed if e.type not in kinds] + explicit
        parsed.sort(key=lambda e: (e.offset, e.length))
    return plain, parsed


def tl_entities(entities: list[MessageEntity]) -> list[Any] | None:
    """Model entities as the Telethon types the request wants.

    Unknown types are dropped rather than guessed at: sending an entity
    Telegram does not recognise fails the whole message, and a caller who
    typed a typo would lose the send rather than the formatting.
    """
    from telethon.tl import types

    out: list[Any] = []
    for entity in entities:
        name = "MessageEntity" + "".join(part.title() for part in entity.type.split("_"))
        klass = getattr(types, name, None)
        if klass is None:
            continue
        kwargs: dict[str, Any] = {"offset": entity.offset, "length": entity.length}
        if entity.url is not None:
            kwargs["url"] = entity.url
        if entity.language is not None:
            kwargs["language"] = entity.language
        if entity.document_id is not None:
            kwargs["document_id"] = entity.document_id
        if entity.collapsed is not None and name == "MessageEntityBlockquote":
            kwargs["collapsed"] = entity.collapsed
        if entity.user_id is not None and name == "MessageEntityMentionName":
            kwargs["user_id"] = entity.user_id
        try:
            out.append(klass(**kwargs))
        except TypeError:
            continue
    return out or None


def split_text(
    text: str, entities: list[MessageEntity], limit: int = MAX_TEXT_UTF16
) -> list[tuple[str, list[MessageEntity]]]:
    """Cut over-long text on word boundaries, re-slicing the entities.

    Telegram counts the 4096 in UTF-16 units, so a message of emoji hits the
    limit at half the characters. Splitting without moving the entities would
    put every bold run in the second part at the wrong offset.
    """
    if utf16_len(text) <= limit:
        return [(text, entities)]

    parts: list[tuple[str, list[MessageEntity]]] = []
    start = 0  # in UTF-16 units
    remaining = text
    while remaining:
        if utf16_len(remaining) <= limit:
            chunk = remaining
        else:
            # Walk characters until the next one would cross the limit.
            taken, used = [], 0
            for char in remaining:
                width = utf16_len(char)
                if used + width > limit:
                    break
                taken.append(char)
                used += width
            chunk = "".join(taken)
            cut = max(chunk.rfind(" "), chunk.rfind("\n"))
            if cut > limit // 4:
                chunk = chunk[: cut + 1]
        width = utf16_len(chunk)
        end = start + width
        sliced = [
            MessageEntity(
                type=e.type,
                offset=max(0, e.offset - start),
                length=min(e.offset + e.length, end) - max(e.offset, start),
                url=e.url,
                user_id=e.user_id,
                language=e.language,
                document_id=e.document_id,
                collapsed=e.collapsed,
            )
            for e in entities
            if e.offset < end and e.offset + e.length > start
        ]
        parts.append((chunk, [e for e in sliced if e.length > 0]))
        remaining = remaining[len(chunk) :]
        start = end
    return parts


def typing_seconds(text: str, *, requested: float = 0.0, auto: bool = False) -> float:
    """How long to show the typing action. Capped at 60 s, as v1 capped it."""
    if requested:
        return min(requested, 60.0)
    if not auto:
        return 0.0
    words = len(text.split())
    return max(2.0, min(30.0, words * 0.35 + 1.5))


async def show_typing(ctx: OpContext, peer: Any, seconds: float) -> None:
    """Hold a typing action, and still wait if the action call fails.

    The sleep is the humanising part; losing it because the `setTyping` call
    was rejected would make `--typing` silently do nothing.
    """
    if seconds <= 0:
        return
    client = getattr(ctx, "client", None)
    if client is None:  # pragma: no cover
        return
    try:
        async with client.action(peer, "typing"):
            await asyncio.sleep(seconds)
    except Exception:
        await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Reply targets and scheduling
# ---------------------------------------------------------------------------


async def reply_target(
    ctx: OpContext,
    *,
    reply_to: int | None = None,
    reply_in: PeerRef | None = None,
    quote: str | None = None,
    quote_offset: int | None = None,
    quote_parse: str | None = None,
    topic: int | None = None,
    reply_task: int | None = None,
    reply_poll_option: int | None = None,
    reply_to_story: int | None = None,
    story_peer: PeerRef | None = None,
    direct_to: PeerRef | None = None,
) -> Any:
    """The single `InputReplyTo` union every reply variant collapses into.

    Telegram has one field for "reply to a message", "reply inside a topic",
    "reply to a story", "reply to a checklist task" and "reply to a poll
    option"; expressing them as five independent flags and then building one
    union here is what keeps the combinations from contradicting each other.
    """
    from telethon.tl import types

    if reply_to_story is not None:
        peer = await resolve(ctx, story_peer) if story_peer is not None else None
        if peer is None:
            raise UsageError("--reply-to-story needs a chat to read the story from", field="chat")
        return types.InputReplyToStory(peer=peer, story_id=reply_to_story)

    if reply_to is None and topic is None and direct_to is None:
        return None

    if reply_to is None and direct_to is not None:
        return types.InputReplyToMonoForum(monoforum_peer_id=await resolve(ctx, direct_to))

    kwargs: dict[str, Any] = {"reply_to_msg_id": reply_to or topic or 0}
    if topic is not None and reply_to is not None:
        kwargs["top_msg_id"] = topic
    if reply_in is not None:
        kwargs["reply_to_peer_id"] = await resolve(ctx, reply_in)
    if quote:
        text, entities = body(quote, parse=quote_parse)
        kwargs["quote_text"] = text
        if entities:
            kwargs["quote_entities"] = tl_entities(entities)
        if quote_offset is not None:
            kwargs["quote_offset"] = quote_offset
    if reply_task is not None:
        kwargs["todo_item_id"] = reply_task
    if reply_poll_option is not None:
        kwargs["poll_option"] = str(reply_poll_option).encode()
    if direct_to is not None:
        kwargs["monoforum_peer_id"] = await resolve(ctx, direct_to)
    return types.InputReplyToMessage(**kwargs)


def schedule_at(value: str | None) -> datetime | None:
    """`--schedule` as the `schedule_date` Telegram wants.

    `online` is not a time: it is the sentinel 0x7FFFFFFE, which the server
    reads as "deliver when the recipient next comes online". Passing it as a
    datetime is the only way Telethon will serialise it.
    """
    if not value:
        return None
    if value.strip().lower() == "online":
        return datetime.fromtimestamp(SCHEDULE_ONLINE, tz=timezone.utc)
    parsed = parse_dt(value)
    if parsed is None:
        raise UsageError(f"--schedule: cannot read {value!r} as a time", field="schedule")
    return parsed


def repeat_period(value: str | None) -> int | None:
    if not value:
        return None
    period = _REPEAT_PERIODS.get(value)
    if period is None:
        raise UsageError(f"--repeat: expected one of {', '.join(_REPEAT_PERIODS)}", field="repeat")
    return period


def effect_id(value: str | None) -> int | None:
    """`--effect` as the numeric id, accepting the id itself.

    An emoji is accepted too and resolved by `message effect list`; doing the
    lookup here would put a network call inside every send.
    """
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise UsageError(
            f"--effect: {value!r} is not an effect id; run 'tlgr message effect list' "
            "to find the id for an emoji",
            field="effect",
        ) from exc


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------


async def input_media(
    ctx: OpContext,
    source: str,
    *,
    spoiler: bool = False,
    ttl: int | None = None,
    voice: bool = False,
    video_note: bool = False,
    force_file: bool = False,
    file_name: str = "",
) -> Any:
    """Turn `--file` into an `InputMedia`, uploading when it names a local file.

    A URL is handed to Telegram as `InputMediaUploadedDocument`'s URL cousin
    rather than downloaded first: the server fetches it, which is both faster
    and the only way a 2 GB link works from a laptop.
    """
    from telethon.tl import types

    if source.startswith(("http://", "https://")):
        return types.InputMediaDocumentExternal(url=source, ttl_seconds=ttl, spoiler=spoiler)

    path = Path(os.path.expanduser(source))
    if not path.exists():
        raise UsageError(f"{source} does not exist", field="file")

    upload = getattr(ctx, "upload_file", None)
    if upload is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this context cannot upload files")
    handle = await upload(path)

    suffix = path.suffix.lower()
    is_photo = suffix in (".jpg", ".jpeg", ".png", ".webp", ".bmp") and not force_file
    if is_photo and not voice and not video_note:
        return types.InputMediaUploadedPhoto(file=handle, ttl_seconds=ttl, spoiler=spoiler)

    attributes, mime = _attributes(path, voice=voice, video_note=video_note, name=file_name)
    for warning in _warnings(path, voice=voice, video_note=video_note):
        ctx.warn(warning)
    return types.InputMediaUploadedDocument(
        file=handle,
        mime_type=mime,
        attributes=attributes,
        ttl_seconds=ttl,
        spoiler=spoiler,
        force_file=force_file,
    )


def _attributes(
    path: Path, *, voice: bool, video_note: bool, name: str = ""
) -> tuple[list[Any], str]:
    """Document attributes plus a mime type, from whatever can read the file."""
    import mimetypes

    from telethon.tl import types

    facts = _probe(path)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    attributes: list[Any] = [types.DocumentAttributeFilename(file_name=name or path.name)]
    duration = int(facts.get("duration") or 0)
    if voice:
        attributes.append(types.DocumentAttributeAudio(duration=duration, voice=True))
        mime = "audio/ogg"
    elif video_note:
        attributes.append(
            types.DocumentAttributeVideo(
                duration=duration,
                w=int(facts.get("width") or 384),
                h=int(facts.get("height") or 384),
                round_message=True,
            )
        )
        mime = "video/mp4"
    elif mime.startswith("video/"):
        attributes.append(
            types.DocumentAttributeVideo(
                duration=duration,
                w=int(facts.get("width") or 0),
                h=int(facts.get("height") or 0),
                supports_streaming=True,
            )
        )
    elif mime.startswith("audio/"):
        attributes.append(types.DocumentAttributeAudio(duration=duration))
    return attributes, mime


def _probe(path: Path) -> dict[str, Any]:
    """Media facts, or nothing. Imported through the context to respect §2.2."""
    from tlgr.core.media import probe

    return probe(path)


def _warnings(path: Path, *, voice: bool, video_note: bool) -> list[str]:
    from tlgr.core.media import probe_warnings

    return probe_warnings(path, voice=voice, video_note=video_note)


# ---------------------------------------------------------------------------
# Reading the reply
# ---------------------------------------------------------------------------


def messages_from_updates(updates: Any, *, chat_id: int = 0) -> list[Message]:
    """Every message an `Updates` reply carries, as models.

    Telegram answers a send with the whole update batch rather than the
    message; picking the message out of it is the step v1 skipped, which is
    why `message send` used to report `{"id": …}` and nothing else.
    """
    out: list[Message] = []
    if updates is None:
        return out
    if hasattr(updates, "id") and hasattr(updates, "date") and not hasattr(updates, "updates"):
        return [message_to_model(updates, chat_id=chat_id or None)]
    chats = {c.id: c for c in (getattr(updates, "chats", None) or [])}
    users = {u.id: u for u in (getattr(updates, "users", None) or [])}
    for update in getattr(updates, "updates", None) or []:
        message = getattr(update, "message", None)
        if message is None or not hasattr(message, "id"):
            continue
        resolved = chat_id or _chat_of(message, chats, users)
        out.append(message_to_model(message, chat_id=resolved or None))
    return out


def _chat_of(message: Any, chats: dict[int, Any], users: dict[int, Any]) -> int:
    peer = getattr(message, "peer_id", None)
    for attribute, kind in (("channel_id", "channel"), ("chat_id", "group"), ("user_id", "user")):
        value = getattr(peer, attribute, None)
        if value is None:
            continue
        entity = chats.get(int(value)) or users.get(int(value))
        if kind == "channel" and entity is not None and getattr(entity, "megagroup", False):
            kind = "supergroup"
        return marked_id(int(value), kind)
    return 0


def message_from_updates(updates: Any, *, chat_id: int = 0, sent_text: str = "") -> Message:
    """The one message a send produced.

    When the update batch carries no message at all — which happens for a
    scheduled send, where the server acknowledges without delivering — a
    stub carrying the id from `UpdateMessageID` is returned rather than an
    error, because the send *did* happen.
    """
    found = messages_from_updates(updates, chat_id=chat_id)
    if found:
        if sent_text and not found[0].text:
            found[0].text = sent_text
        return found[0]
    message_id = 0
    for update in getattr(updates, "updates", None) or []:
        if type(update).__name__ == "UpdateMessageID":
            message_id = int(getattr(update, "id", 0) or 0)
            break
    date = getattr(updates, "date", None)
    from tlgr.core.timefmt import fmt_dt, to_unix

    return Message(
        id=message_id,
        chat_id=chat_id,
        date=fmt_dt(date) or "",
        date_unix=to_unix(date) or 0,
        text=sent_text,
        out=True,
    )


def require_supported(feature: str, reason: str) -> None:
    """Refuse a feature this build genuinely cannot perform.

    Exit 13 rather than 1: "tlgr cannot do this" is not "the operation
    failed", and an agent must be able to tell them apart (§7.3).
    """
    raise NotSupportedError(f"{feature} is not supported: {reason}")
