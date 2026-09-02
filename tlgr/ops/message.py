"""The `message` group: everything you can do to or with a message.

This is the busiest group in the product and the one the registry was designed
against. Three things it settles for the whole surface:

* **one composer.** v1 had `message send`, `media upload` and `draft set` each
  building their own request; here `message send` is the universal composer
  (text, album, dice, contact, location, sticker, voice, poll) and `forward`,
  `edit` and `draft set` share its options struct, so `--parse`, `--reply-to`
  and `--schedule` cannot mean three different things.
* **one message shape.** Every op that returns a message returns
  `models.Message`, built by `ops/_serialize.message_to_model`.
* **one pagination story.** `list`, `search` and `thread list` take
  `--limit/--cursor/--all` from the transport, not from their own request, and
  hand back a signed `Page[Message]`.

Telethon is imported inside functions, never at module scope: importing the
registry is what builds `tlgr --help`, and that must not pull in Telethon.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from tlgr.core.errors import (
    EXIT_EMPTY,
    NotFoundError,
    UsageError,
)
from tlgr.core.pagination import PageKind, build_page, decode_cursor
from tlgr.core.text import utf16_len
from tlgr.core.timefmt import fmt_dt, parse_dt, to_unix
from tlgr.models.base import Request
from tlgr.models.message import (
    ComposeResult,
    DeleteResult,
    DiceCatalog,
    EditResult,
    Effect,
    EntityReport,
    FactCheck,
    ForwardedMessage,
    GameInfo,
    GameScore,
    LinkResult,
    Message,
    MessageEntity,
    PaidMessageSettings,
    PinResult,
    ReactionSummary,
    ReactResult,
    ReadReceipts,
    ReadResult,
    ReportResult,
    ScheduledSent,
    SponsoredHidden,
    SponsoredMessage,
    SuggestedPostState,
    SummaryResult,
    Tone,
    Transcription,
    Translation,
    ViewCount,
    WebPagePreview,
)
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._serialize import entity_to_peer, message_entities, message_to_model
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: `--type` values → the `inputMessagesFilter*` class that implements them.
#: Spelled as class *names* so this module still imports without Telethon.
FILTERS: dict[str, str] = {
    "photo": "InputMessagesFilterPhotos",
    "video": "InputMessagesFilterVideo",
    "media": "InputMessagesFilterPhotoVideo",
    "file": "InputMessagesFilterDocument",
    "link": "InputMessagesFilterUrl",
    "url": "InputMessagesFilterUrl",
    "music": "InputMessagesFilterMusic",
    "voice": "InputMessagesFilterVoice",
    "gif": "InputMessagesFilterGif",
    "round": "InputMessagesFilterRoundVideo",
    "geo": "InputMessagesFilterGeo",
    "contact": "InputMessagesFilterContacts",
    "pinned": "InputMessagesFilterPinned",
    "chat-photo": "InputMessagesFilterChatPhotos",
    "call": "InputMessagesFilterPhoneCalls",
    "poll": "InputMessagesFilterPoll",
    "todo": "InputMessagesFilterToDo",
    "mention": "InputMessagesFilterMyMentions",
    "sticker": "InputMessagesFilterDocument",
}

_EXAMPLE_MESSAGE: dict[str, Any] = {
    "id": 12345,
    "chat_id": 777123,
    "date": "2026-09-03T09:14:07Z",
    "date_unix": 1788340447,
    "text": "on my way",
    "out": True,
    "kind": "message",
}


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _client(ctx: OpContext) -> Any:
    client = getattr(ctx, "client", None)
    if client is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this operation needs a connected account")
    return client


def _window(ctx: OpContext, op: str, kind: PageKind, default: int = 20) -> tuple[int, Any]:
    """`(limit, cursor state)` for a paginated op.

    `--limit`/`--cursor` are transport-level and never request fields
    (registry lint L5), so every paginated implementation reads them the same
    way instead of redeclaring them.
    """
    limit = int(getattr(ctx, "limit", None) or default)
    if limit < 1:
        raise UsageError("--limit must be at least 1", field="limit")
    token = getattr(ctx, "cursor", None)
    state: dict[str, Any] = {}
    if token:
        state = decode_cursor(token, op=op, kind=kind, account=ctx.account)
    return min(limit, 1000), state


def _filter(name: str | None) -> Any:
    """`--type photo` → `InputMessagesFilterPhotos()`, or None."""
    if not name:
        return None
    class_name = FILTERS.get(name)
    if class_name is None:
        raise UsageError(
            f"--type {name!r} is not a media filter; expected one of {', '.join(FILTERS)}",
            field="type",
        )
    from telethon.tl import types

    return getattr(types, class_name)()


def _ids(values: tuple[int, ...] | tuple[str, ...] | None) -> list[int]:
    """Expand `100-120` ranges alongside plain ids.

    A range is what a human types when deleting a burst of messages, and
    making them spell out twenty ids is how a wrong one gets in.
    """
    out: list[int] = []
    for value in values or ():
        text = str(value)
        if "-" in text[1:]:
            head, _, tail = text.partition("-")
            try:
                start, end = int(head), int(tail)
            except ValueError as exc:
                raise UsageError(f"{text!r} is not an id or an id range", field="msg_id") from exc
            if end < start or end - start > 10_000:
                raise UsageError(f"{text!r} is not a usable id range", field="msg_id")
            out.extend(range(start, end + 1))
        else:
            try:
                out.append(int(text))
            except ValueError as exc:
                raise UsageError(f"{text!r} is not a message id", field="msg_id") from exc
    return out


async def _fetch(
    ctx: OpContext,
    peer: Any,
    *,
    chat_id: int,
    limit: int,
    **kwargs: Any,
) -> list[Message]:
    """`iter_messages` into models, with the chat id filled in once."""
    out: list[Message] = []
    async for raw in _client(ctx).iter_messages(peer, limit=limit, **kwargs):
        if raw is None:
            continue
        out.append(message_to_model(raw, chat_id=chat_id))
    return out


def _is_not_modified(exc: BaseException) -> bool:
    """MESSAGE_NOT_MODIFIED is success: the world already looks like that."""
    text = f"{type(exc).__name__} {exc}".upper()
    return "NOT_MODIFIED" in text


async def _affected_loop(ctx: OpContext, make_request: Any) -> int:
    """Drive a `messages.AffectedHistory` call until `offset == 0`.

    `readMentions`, `readReactions`, `unpinAllMessages` and
    `deleteParticipantHistory` all return a partial result with an offset to
    resume from. v1 called them once and reported success, so "unpin
    everything" unpinned the first hundred.
    """
    client = _client(ctx)
    total = 0
    offset = 0
    for _ in range(100):
        result = await client(make_request(offset))
        total += int(getattr(result, "pts_count", 0) or 0)
        offset = int(getattr(result, "offset", 0) or 0)
        if offset == 0:
            break
        limiter = getattr(ctx, "limiter", None)
        if limiter is not None:
            await limiter.acquire("bulk")
    return total


def _already(ctx: OpContext) -> None:
    mark = getattr(ctx, "mark_already", None)
    if callable(mark):
        mark()


# ---------------------------------------------------------------------------
# message send
# ---------------------------------------------------------------------------


class SendReq(_send.SendOptions, kw_only=True):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Target chat or user.")]
    text: Annotated[
        str,
        arg(1, metavar="TEXT", required=False, help="Message text; '-' reads stdin."),
    ] = ""
    parse: Annotated[
        str | None, choice("md", "html", "none", help="Formatting of TEXT/--caption.")
    ] = None
    entities: Annotated[
        str | None,
        opt("--entities", metavar="JSON", kind="json", help="Explicit entity vector (UTF-16)."),
    ] = None
    stdin: Annotated[bool, opt("--stdin", help="Read the body from stdin.")] = False
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Reply to this message.")
    ] = None
    reply_in: Annotated[
        PeerRef | None,
        opt("--reply-in", metavar="CHAT", kind="peer", help="Chat --reply-to belongs to."),
    ] = None
    quote: Annotated[
        str | None, opt("--quote", help="Quote this exact substring of the replied message.")
    ] = None
    quote_offset: Annotated[
        int | None, opt("--quote-offset", metavar="N", help="UTF-16 offset of --quote.")
    ] = None
    reply_task: Annotated[
        int | None, opt("--reply-task", metavar="N", help="Reply to a checklist task id.")
    ] = None
    reply_poll_option: Annotated[
        int | None, opt("--reply-poll-option", metavar="N", help="Reply to a poll option index.")
    ] = None
    reply_to_story: Annotated[
        int | None, opt("--reply-to-story", metavar="ID", help="Reply to a story of CHAT.")
    ] = None
    direct_to: Annotated[
        PeerRef | None,
        opt("--direct-to", metavar="USER", kind="user", help="Answer in a monoforum topic."),
    ] = None
    comment_to: Annotated[
        int | None, opt("--comment-to", metavar="ID", help="Comment on a channel post.")
    ] = None
    clear_draft: Annotated[bool, opt(help="Clear the chat draft on send.")] = True
    split: Annotated[bool, opt("--split", help="Split text longer than the limit.")] = False
    split_at: Annotated[int, opt("--split-at", metavar="N", help="Split threshold.", ge=1)] = 4096
    as_file: Annotated[bool, opt("--as-file", help="Send the text as a .txt document.")] = False
    filename: Annotated[
        str | None, opt("--filename", metavar="NAME", help="Filename for --as-file/stdin.")
    ] = None
    file: Annotated[
        list[str],
        opt("--file", metavar="PATH", help="Attach a file; repeat for an album."),
    ] = []
    caption: Annotated[str | None, opt("--caption", help="Caption for the attached media.")] = None
    caption_above: Annotated[bool, opt("--caption-above", help="invert_media.")] = False
    spoiler: Annotated[bool, opt("--spoiler", help="Hide the media behind a spoiler.")] = False
    ttl: Annotated[
        int | None,
        opt("--ttl", metavar="DURATION", kind="duration", help="Self-destruct timer."),
    ] = None
    sticker: Annotated[str | None, opt("--sticker", metavar="REF", help="Send a sticker.")] = None
    gif: Annotated[str | None, opt("--gif", metavar="REF", help="Send a GIF/animation.")] = None
    voice: Annotated[
        str | None, opt("--voice", metavar="PATH", kind="path", help="Send as a voice note.")
    ] = None
    video_note: Annotated[
        str | None, opt("--video-note", metavar="PATH", kind="path", help="Send a round video.")
    ] = None
    dice: Annotated[
        str | None, opt("--dice", metavar="EMOJI", help="Send an animated dice/darts/slot.")
    ] = None
    contact: Annotated[
        PeerRef | None,
        opt("--contact", metavar="USER", kind="user", help="Send a contact card for a user."),
    ] = None
    contact_phone: Annotated[str | None, opt("--contact-phone", help="Contact card phone.")] = None
    contact_first: Annotated[str | None, opt("--contact-first", help="Contact first name.")] = None
    contact_last: Annotated[str | None, opt("--contact-last", help="Contact last name.")] = None
    vcard: Annotated[
        str | None, opt("--vcard", metavar="PATH", kind="path", help="vCard payload.")
    ] = None
    location: Annotated[
        str | None, opt("--location", metavar="LAT,LON", help="Attach a static location.")
    ] = None
    poll: Annotated[
        str | None, opt("--poll", metavar="JSON", kind="json", help="Attach a poll.")
    ] = None
    no_preview: Annotated[bool, opt("--no-preview", help="Disable the link preview.")] = False
    preview_url: Annotated[
        str | None, opt("--preview-url", metavar="URL", help="Preview this URL.")
    ] = None
    preview_large: Annotated[bool, opt("--preview-large", help="force_large_media.")] = False
    preview_small: Annotated[bool, opt("--preview-small", help="force_small_media.")] = False
    preview_above: Annotated[bool, opt("--preview-above", help="Preview above the text.")] = False
    invert_media: Annotated[
        bool, opt("--invert-media", help="Alias of --preview-above/--caption-above.")
    ] = False
    rich_markdown: Annotated[
        str | None,
        opt("--rich-markdown", metavar="PATH", kind="path", help="Send a rich body (Markdown)."),
    ] = None
    rich_html: Annotated[
        str | None,
        opt("--rich-html", metavar="PATH", kind="path", help="Send a rich body (HTML)."),
    ] = None
    screenshot: Annotated[
        bool, opt("--screenshot", help="Send a screenshot-taken service notification.")
    ] = False
    suggest: Annotated[bool, opt("--suggest", help="Offer this as a suggested post.")] = False
    price_stars: Annotated[
        int | None, opt("--price-stars", metavar="N", help="Stars asked for the suggested post.")
    ] = None
    price_ton: Annotated[
        int | None, opt("--price-ton", metavar="NANO", help="TON asked for the suggested post.")
    ] = None
    publish_at: Annotated[
        str | None,
        opt("--publish-at", metavar="TS", kind="datetime", help="Requested publication time."),
    ] = None
    wait_slowmode: Annotated[
        bool, opt("--wait-slowmode", help="Wait out slow mode instead of failing.")
    ] = False
    random_id: Annotated[
        int | None,
        opt("--random-id", metavar="N", help="Explicit random_id so a retry is deduplicated."),
    ] = None


def _suggested_post(req: SendReq) -> Any:
    """`--suggest` as the `SuggestedPost` the send requests carry."""
    if not req.suggest:
        return None
    from telethon.tl import types

    price: Any = None
    if req.price_stars is not None:
        price = types.StarsAmount(amount=req.price_stars, nanos=0)
    elif req.price_ton is not None:
        price = types.StarsTonAmount(amount=req.price_ton)
    return types.SuggestedPost(price=price, schedule_date=_send.schedule_at(req.publish_at))


async def _non_file_media(ctx: OpContext, req: SendReq) -> Any:
    """The media variants that are described rather than uploaded."""
    from telethon.tl import types

    if req.dice is not None:
        return types.InputMediaDice(emoticon=req.dice or "🎲")
    if req.location:
        try:
            latitude, _, longitude = req.location.partition(",")
            point = types.InputGeoPoint(lat=float(latitude), long=float(longitude))
        except ValueError as exc:
            raise UsageError("--location wants 'lat,lon'", field="location") from exc
        return types.InputMediaGeoPoint(geo_point=point)
    if req.contact is not None or req.contact_phone:
        phone = req.contact_phone or ""
        first = req.contact_first or ""
        last = req.contact_last or ""
        vcard = ""
        if req.vcard:
            with open(req.vcard, encoding="utf-8") as handle:
                vcard = handle.read()
        if req.contact is not None:
            peer = await _send.resolve(ctx, req.contact)
            user = await _client(ctx).get_entity(peer)
            phone = phone or (getattr(user, "phone", None) or "")
            first = first or (getattr(user, "first_name", None) or "")
            last = last or (getattr(user, "last_name", None) or "")
        if not phone:
            raise UsageError(
                "a contact card needs a phone number; the user hides theirs, so pass "
                "--contact-phone",
                field="contact",
            )
        return types.InputMediaContact(
            phone_number=phone, first_name=first, last_name=last, vcard=vcard
        )
    if req.poll is not None:
        _send.require_supported(
            "--poll",
            "polls are the `poll create` command's shape and land in PR-9; "
            "sending one here would fix a JSON schema this build cannot validate",
        )
    if req.preview_url:
        return types.InputMediaWebPage(
            url=req.preview_url,
            force_large_media=req.preview_large or None,
            force_small_media=req.preview_small or None,
            optional=True,
        )
    return None


async def _sticker_or_gif(ctx: OpContext, ref: str) -> Any:
    """A sticker/GIF reference: a file id pair, or a local file."""
    from telethon.tl import types

    if ":" in ref and all(part.lstrip("-").isdigit() for part in ref.split(":", 1)):
        document_id, _, access_hash = ref.partition(":")
        return types.InputMediaDocument(
            id=types.InputDocument(
                id=int(document_id), access_hash=int(access_hash), file_reference=b""
            )
        )
    return await _send.input_media(ctx, ref)


async def send(ctx: OpContext, req: SendReq) -> Message:
    """Send anything to a chat, with every send-time option Telegram has.

    One implementation rather than one per media type, because the options —
    reply target, schedule, silence, effect, protection, paid stars — are
    orthogonal to *what* is being sent and duplicating them per command is
    how they drift apart.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if req.rich_markdown or req.rich_html:
        _send.require_supported(
            "--rich-markdown/--rich-html",
            "a rich (long-form) body is layer 229 and the pinned Telethon speaks 227",
        )

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)

    if req.screenshot:
        result = await _client(ctx)(
            fn.SendScreenshotNotificationRequest(
                peer=peer,
                reply_to=types.InputReplyToMessage(reply_to_msg_id=req.reply_to or 0),
                random_id=req.random_id or _random_id(),
            )
        )
        return _send.message_from_updates(result, chat_id=chat_id)

    text, entities = _send.body(req.text, parse=req.parse, entities=req.entities, stdin=req.stdin)
    caption, caption_entities = (
        _send.body(req.caption, parse=req.parse) if req.caption is not None else ("", [])
    )

    if req.comment_to is not None:
        # A comment lives in the linked discussion group, not in the channel.
        discussion = await _client(ctx)(fn.GetDiscussionMessageRequest(peer, req.comment_to))
        found = list(getattr(discussion, "messages", None) or [])
        if not found:
            raise NotFoundError(f"post {req.comment_to} has no discussion thread to comment in")
        peer = await _client(ctx).get_input_entity(found[0].peer_id)
        chat_id = _send.peer_id_of(peer)
        req.reply_to = int(found[0].id)

    await _send.show_typing(
        ctx, peer, _send.typing_seconds(text, requested=req.typing, auto=req.typing_auto)
    )

    reply_to = await _send.reply_target(
        ctx,
        reply_to=req.reply_to,
        reply_in=req.reply_in,
        quote=req.quote,
        quote_offset=req.quote_offset,
        quote_parse=req.parse,
        topic=req.topic,
        reply_task=req.reply_task,
        reply_poll_option=req.reply_poll_option,
        reply_to_story=req.reply_to_story,
        story_peer=req.chat,
        direct_to=req.direct_to,
    )
    common: dict[str, Any] = {
        "peer": peer,
        "silent": req.silent or None,
        "clear_draft": req.clear_draft or None,
        "noforwards": req.protect or None,
        "invert_media": (req.invert_media or req.preview_above or req.caption_above) or None,
        "reply_to": reply_to,
        "schedule_date": _send.schedule_at(req.schedule),
        "schedule_repeat_period": _send.repeat_period(req.repeat),
        "send_as": await _send.resolve(ctx, req.send_as) if req.send_as is not None else None,
        "effect": _send.effect_id(req.effect),
        "allow_paid_stars": req.paid_stars,
        "suggested_post": _suggested_post(req),
    }
    if req.quick_reply:
        common["quick_reply_shortcut"] = types.InputQuickReplyShortcut(shortcut=req.quick_reply)

    media = await _non_file_media(ctx, req)
    files = list(req.file)
    if req.voice:
        files, media = [], await _send.input_media(ctx, req.voice, voice=True)
    elif req.video_note:
        files, media = [], await _send.input_media(ctx, req.video_note, video_note=True)
    elif req.sticker:
        files, media = [], await _sticker_or_gif(ctx, req.sticker)
    elif req.gif:
        files, media = [], await _sticker_or_gif(ctx, req.gif)
    elif req.as_file:
        files, media = [], await _text_as_file(ctx, text, req.filename)
        text, entities = caption or "", caption_entities

    sent = await _dispatch_send(
        ctx,
        req,
        common,
        peer=peer,
        chat_id=chat_id,
        text=text,
        entities=entities,
        caption=caption,
        caption_entities=caption_entities,
        media=media,
        files=files,
    )
    ctx.emit("message_out", {"chat_id": chat_id, "id": sent.id, "text": sent.text})
    return sent


async def _dispatch_send(
    ctx: OpContext,
    req: SendReq,
    common: dict[str, Any],
    *,
    peer: Any,
    chat_id: int,
    text: str,
    entities: list[MessageEntity],
    caption: str,
    caption_entities: list[MessageEntity],
    media: Any,
    files: list[str],
) -> Message:
    """Pick the request the send actually needs, and run it."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    client = _client(ctx)

    if len(files) > 1:
        singles = []
        for index, source in enumerate(files):
            item = await _send.input_media(ctx, source, spoiler=req.spoiler, ttl=req.ttl)
            uploaded = await client(fn.UploadMediaRequest(peer=peer, media=item))
            singles.append(
                types.InputSingleMedia(
                    media=_uploaded_to_input(uploaded),
                    random_id=_random_id(),
                    message=caption if index == 0 else "",
                    entities=_send.tl_entities(caption_entities) if index == 0 else None,
                )
            )
        result = await client(
            fn.SendMultiMediaRequest(multi_media=singles, **_only(common, fn.SendMultiMediaRequest))
        )
        produced = _send.messages_from_updates(result, chat_id=chat_id)
        first = produced[0] if produced else _send.message_from_updates(result, chat_id=chat_id)
        first.batch = [m.id for m in produced[1:]]
        return first

    if files:
        media = await _send.input_media(ctx, files[0], spoiler=req.spoiler, ttl=req.ttl)

    if media is not None:
        result = await client(
            fn.SendMediaRequest(
                media=media,
                message=caption or text,
                entities=_send.tl_entities(caption_entities or entities),
                random_id=req.random_id or _random_id(),
                **_only(common, fn.SendMediaRequest),
            )
        )
        return _send.message_from_updates(result, chat_id=chat_id, sent_text=caption or text)

    parts = _send.split_text(text, entities, req.split_at) if req.split else [(text, entities)]
    if not req.split and utf16_len(text) > _send.MAX_TEXT_UTF16:
        raise UsageError(
            f"the text is {utf16_len(text)} UTF-16 units and Telegram accepts "
            f"{_send.MAX_TEXT_UTF16}; pass --split to send it as several messages",
            field="text",
        )

    produced = []
    for index, (chunk, runs) in enumerate(parts):
        result = await client(
            fn.SendMessageRequest(
                message=chunk,
                entities=_send.tl_entities(runs),
                no_webpage=req.no_preview or None,
                random_id=(req.random_id or _random_id()) + index,
                **_only(common, fn.SendMessageRequest),
            )
        )
        produced.append(_send.message_from_updates(result, chat_id=chat_id, sent_text=chunk))
        if index + 1 < len(parts):
            limiter = getattr(ctx, "limiter", None)
            if limiter is not None:
                await limiter.acquire("send")
    first = produced[0]
    first.batch = [m.id for m in produced[1:]]
    return first


def _uploaded_to_input(uploaded: Any) -> Any:
    """`messages.uploadMedia` result → the `InputMedia` a multi-send wants."""
    from telethon import utils

    return utils.get_input_media(uploaded)


def _only(values: dict[str, Any], request: Any) -> dict[str, Any]:
    """Keep the keys *request* actually accepts.

    The four send requests share most of their flags and differ in a few; a
    filtered dict is how one options mapping serves all of them without a
    per-request copy that drifts.
    """
    import inspect

    allowed = set(inspect.signature(request.__init__).parameters)
    return {k: v for k, v in values.items() if k in allowed and v is not None}


def _random_id() -> int:
    import os

    return int.from_bytes(os.urandom(8), "big", signed=True)


async def _text_as_file(ctx: OpContext, text: str, name: str | None) -> Any:
    """`--as-file`: the message body as a `.txt` document."""
    import tempfile
    from pathlib import Path

    directory = Path(tempfile.mkdtemp(prefix="tlgr-txt-"))
    path = directory / (name or "message.txt")
    path.write_text(text, encoding="utf-8")
    return await _send.input_media(ctx, str(path), force_file=True, file_name=path.name)


SPEC_SEND = OperationSpec(
    id="message.send",
    request=SendReq,
    response=Message,
    impl=send,
    summary="Send a message to a chat",
    description=(
        "The universal composer: text, a file, an album, a sticker, a voice "
        "note, a dice, a contact card or a location, with every send-time "
        "option Telegram exposes. Text longer than 4096 UTF-16 units is "
        "refused unless --split is given, because silently truncating a "
        "message is worse than not sending it."
    ),
    aliases=("send", "msg.send"),
    legacy_paths=("message send", "msg send", "send"),
    mutating=True,
    rate_class="send",
    timeout_s=300,
    columns=("id", "chat_id", "date", "text"),
    example=_EXAMPLE_MESSAGE,
    example_args='message send @alice "on my way"',
    covers=(
        "bots.button-styles-icons",
        "bots.paid-broadcast-floodskip",
        "bots.paid-message-to-bot",
        "bots.reply-keyboard-hide-force",
        "bots.send-with-keyboard",
        "contact.send-card",
        "dialogs.draft-send",
        "dice.send",
        "effect.send",
        "media.link-preview",
        "messages-core.comments-post",
        "messages-core.format-basic-styles",
        "messages-core.format-blockquote",
        "messages-core.format-code-and-pre",
        "messages-core.format-custom-emoji",
        "messages-core.format-expandable-blockquote",
        "messages-core.format-formatted-date-entity",
        "messages-core.format-hashtag-cashtag",
        "messages-core.format-html-parse-mode",
        "messages-core.format-markdown-parse-mode",
        "messages-core.format-mention",
        "messages-core.format-spoiler",
        "messages-core.format-text-link",
        "messages-core.link-preview-above-text",
        "messages-core.link-preview-choose-url-and-size",
        "messages-core.link-preview-disable",
        "messages-core.reply-in-another-chat",
        "messages-core.reply-to-checklist-task-or-poll-option",
        "messages-core.reply-to-story",
        "messages-core.reply-with-quote",
        "messages-core.saved-messages-send",
        "messages-core.saved-reminder",
        "messages-core.screenshot-notification",
        "messages-core.send-as-peer",
        "messages-core.send-clear-draft",
        "messages-core.send-in-direct-channel-messages",
        "messages-core.send-long-text-split",
        "messages-core.send-paid-message",
        "messages-core.send-preflight-restrictions",
        "messages-core.send-protected-content-message",
        "messages-core.send-reply",
        "messages-core.send-scheduled",
        "messages-core.send-scheduled-repeating",
        "messages-core.send-silent",
        "messages-core.send-text",
        "messages-core.send-text-as-file",
        "messages-core.send-to-topic",
        "messages-core.send-when-online",
        "messages-core.send-with-effect",
        "messages-core.suggest-post",
        "poll.reply-to-option",
        "todo.reply-to-task",
        "updates.invoke-after-msg",
        "updates.sync-random-id-dedup",
        "webpage.send-as-media",
    ),
    covers_partial=("messages-core.send-rich-message", "richmsg.send"),
    coverage_note=(
        "A layer-229 rich body is refused with NOT_SUPPORTED: the pinned "
        "Telethon speaks layer 227 and cannot serialise inputRichMessage*."
    ),
)


# ---------------------------------------------------------------------------
# message list / search / thread list
# ---------------------------------------------------------------------------


class ListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to read.")]
    since: Annotated[
        str | None,
        opt("--since", metavar="TS", kind="datetime", help="Only messages after this time."),
    ] = None
    until: Annotated[
        str | None,
        opt("--until", metavar="TS", kind="datetime", help="Only messages before this time."),
    ] = None
    before_id: Annotated[
        int | None,
        opt("--before-id", metavar="ID", kind="msg_id", help="Older than this id (offset_id)."),
    ] = None
    after_id: Annotated[
        int | None,
        opt("--after-id", metavar="ID", kind="msg_id", help="Newer than this id (min_id)."),
    ] = None
    around: Annotated[
        int | None,
        opt("--around", metavar="ID", kind="msg_id", help="Centre the page on this id."),
    ] = None
    ids: Annotated[
        list[str], opt("--ids", metavar="ID", help="Fetch exactly these ids or ranges.")
    ] = []
    date: Annotated[
        str | None,
        opt("--date", metavar="DATE", kind="datetime", help="Jump to the first message of a date."),
    ] = None
    reverse: Annotated[bool, opt("--reverse", help="Oldest first.")] = False
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Only this topic / thread.")
    ] = None
    type: Annotated[
        str | None, opt("--type", metavar="FILTER", help="Media filter; 'pinned' is the pin list.")
    ] = None
    unread: Annotated[bool, opt("--unread", help="Only messages after the unread divider.")] = False
    scheduled: Annotated[bool, opt("--scheduled", help="List the scheduled drawer.")] = False
    saved_peer: Annotated[
        PeerRef | None,
        opt("--saved-peer", metavar="CHAT", kind="peer", help="In Saved Messages: one origin."),
    ] = None
    personal_channel: Annotated[
        PeerRef | None,
        opt("--personal-channel", metavar="USER", kind="user", help="A profile's channel posts."),
    ] = None
    delivery: Annotated[
        bool, opt("--delivery", help="Annotate own messages with sent/delivered/read state.")
    ] = False
    sender: Annotated[bool, opt("--sender", help="Include sender info and admin rank.")] = False
    media: Annotated[bool, opt("--media", help="Include media metadata.")] = True
    reactions: Annotated[bool, opt("--reactions", help="Include reactions.")] = True
    entities: Annotated[bool, opt("--entities", help="Include entities.")] = True


async def _history_state(
    ctx: OpContext, req: ListReq, limit: int, state: dict[str, Any]
) -> dict[str, Any]:
    """`offset_id`/`add_offset` for the page being asked for.

    `offset_id + add_offset` is Telegram's universal history cursor: older is
    `add_offset = 0`, newer is `-limit`, and centred is `-limit // 2`. Naming
    the three cases here is what keeps `--around` from being a second,
    subtly-different pagination.
    """
    if state:
        return dict(state)
    if req.around is not None:
        return {"offset_id": req.around, "add_offset": -(limit // 2)}
    if req.after_id is not None:
        return {"offset_id": 0, "add_offset": 0, "min_id": req.after_id}
    return {"offset_id": req.before_id or 0, "add_offset": 0}


async def list_messages(ctx: OpContext, req: ListReq) -> Page[Message]:
    """Read chat history, one signed page at a time."""
    limit, state = _window(ctx, "message.list", PageKind.HISTORY)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)

    if req.personal_channel is not None:
        return await _personal_channel(ctx, req, limit)

    explicit = _ids(tuple(req.ids)) if req.ids else []
    if explicit:
        items = await _fetch(ctx, peer, chat_id=chat_id, limit=len(explicit), ids=explicit)
        return Page(items=[m for m in items if m.id], has_more=False, total=len(items))

    offsets = await _history_state(ctx, req, limit, state)
    kwargs: dict[str, Any] = {
        "offset_id": int(offsets.get("offset_id", 0)),
        "add_offset": int(offsets.get("add_offset", 0)),
        "reverse": req.reverse,
        "scheduled": req.scheduled,
    }
    if offsets.get("min_id"):
        kwargs["min_id"] = int(offsets["min_id"])
    if req.topic is not None:
        kwargs["reply_to"] = req.topic
    if req.type:
        kwargs["filter"] = _filter(req.type)
    when = req.date or req.until
    if when:
        kwargs["offset_date"] = parse_dt(when)
    if req.unread:
        kwargs["min_id"] = await _read_inbox_max(ctx, peer)
    if req.saved_peer is not None:
        # Saved Messages keeps one sub-dialog per origin; Telethon reaches it
        # through the same reply_to slot the forum topics use.
        kwargs["reply_to"] = _send.peer_id_of(await _send.resolve(ctx, req.saved_peer))

    items = await _fetch(ctx, peer, chat_id=chat_id, limit=limit, **kwargs)
    if req.since:
        floor = parse_dt(req.since)
        cutoff = to_unix(floor) or 0
        items = [m for m in items if m.date_unix >= cutoff]
    if req.scheduled:
        for message in items:
            message.scheduled = True
    if req.delivery:
        await _annotate_delivery(ctx, peer, items)
    if req.sender:
        await _attach_senders(ctx, items)
    _project(items, media=req.media, reactions=req.reactions, entities=req.entities)

    next_state = {"offset_id": items[-1].id, "add_offset": 0} if items else {}
    return build_page(
        items,
        op="message.list",
        kind=PageKind.HISTORY,
        state=next_state,
        account=ctx.account,
        limit=limit,
        has_more=None if items else False,
    )


def _project(items: list[Message], *, media: bool, reactions: bool, entities: bool) -> None:
    """Drop the parts the caller said it did not want.

    The defaults are *on*: a caller cannot opt into a field it does not know
    exists, and "did we already react" is not an optional detail for anything
    that reacts (that was v1's `include_reactions` bug).
    """
    for message in items:
        if not media:
            message.media = None
        if not reactions:
            message.reactions = None
        if not entities:
            message.entities = []


async def _read_inbox_max(ctx: OpContext, peer: Any) -> int:
    """`read_inbox_max_id` — where the unread divider sits."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    result = await _client(ctx)(fn.GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer)]))
    for dialog in getattr(result, "dialogs", None) or []:
        return int(getattr(dialog, "read_inbox_max_id", 0) or 0)
    return 0


async def _annotate_delivery(ctx: OpContext, peer: Any, items: list[Message]) -> None:
    """`--delivery`: sent / delivered / read, for our own messages.

    This is a *dialog* field (`read_outbox_max_id`), not a message field, so
    it is fetched once and applied rather than guessed from the message.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    result = await _client(ctx)(fn.GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer)]))
    outbox = 0
    for dialog in getattr(result, "dialogs", None) or []:
        outbox = int(getattr(dialog, "read_outbox_max_id", 0) or 0)
    for message in items:
        if message.out:
            message.delivery = "read" if message.id <= outbox else "sent"


async def _attach_senders(ctx: OpContext, items: list[Message]) -> None:
    """Resolve each distinct sender once and hang it off every message."""
    client = _client(ctx)
    cache: dict[int, Any] = {}
    for message in items:
        sender_id = message.sender_id
        if sender_id is None:
            continue
        if sender_id not in cache:
            try:
                cache[sender_id] = entity_to_peer(await client.get_entity(sender_id))
            except Exception:
                cache[sender_id] = None
        message.sender = cache[sender_id]


async def _personal_channel(ctx: OpContext, req: ListReq, limit: int) -> Page[Message]:
    """The channel posts a profile shows, which are not in any dialog."""
    from telethon.tl.functions import messages as fn

    user = await _send.resolve(ctx, req.personal_channel)
    result = await _client(ctx)(fn.GetPersonalChannelHistoryRequest(peer=user, limit=limit))
    raw = list(getattr(result, "messages", None) or [])
    items = [message_to_model(message) for message in raw]
    return Page(items=items, has_more=False, total=len(items))


SPEC_LIST = OperationSpec(
    id="message.list",
    request=ListReq,
    response=Page[Message],
    impl=list_messages,
    summary="Read a chat's message history",
    description=(
        "Paginated history with the offsets Telegram actually has: older than "
        "an id (--before-id), newer than an id (--after-id), centred on one "
        "(--around), from a date (--date), only the unread tail (--unread), "
        "the scheduled drawer (--scheduled), or one media type (--type)."
    ),
    aliases=("msg.list",),
    legacy_paths=("message list", "msg list"),
    paginated=PageKind.HISTORY,
    columns=("id", "date", "text"),
    headers=("ID", "Date", "Text"),
    example={"items": [_EXAMPLE_MESSAGE], "has_more": False},
    example_args="message list @alice",
    covers=(
        "bots.payment-service-messages",
        "groupcall.service-messages",
        "groups-channels-admin.monoforum-topic-history",
        "messages-core.history-jump-to-date",
        "messages-core.history-list",
        "messages-core.history-unread-separator",
        "messages-core.message-delivery-status",
        "messages-core.message-jump-to",
        "messages-core.message-sender-rank",
        "messages-core.message-view-in-topic",
        "messages-core.personal-channel-history",
        "messages-core.pinned-list",
        "messages-core.saved-dialog-history",
        "messages-core.scheduled-list",
        "stories.story-mention",
    ),
)


class SearchReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to search.")]
    query: Annotated[str, arg(1, metavar="QUERY", required=False, help="Text to find.")] = ""
    from_user: Annotated[
        PeerRef | None,
        opt("--from", metavar="USER", kind="user", help="Only from this sender."),
    ] = None
    type: Annotated[str | None, opt("--type", metavar="FILTER", help="Media filter.")] = None
    since: Annotated[
        str | None, opt("--since", metavar="TS", kind="datetime", help="Only after this time.")
    ] = None
    until: Annotated[
        str | None, opt("--until", metavar="TS", kind="datetime", help="Only before this time.")
    ] = None
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Restrict to a topic.")
    ] = None
    min_id: Annotated[int | None, opt("--min-id", metavar="ID", help="Only ids above this.")] = None
    max_id: Annotated[int | None, opt("--max-id", metavar="ID", help="Only ids below this.")] = None
    reverse: Annotated[bool, opt("--reverse", help="Oldest first.")] = False
    ids: Annotated[list[str], opt("--ids", metavar="ID", help="Restrict to these ids.")] = []
    saved_peer: Annotated[
        PeerRef | None,
        opt("--saved-peer", metavar="CHAT", kind="peer", help="One Saved-Messages dialog."),
    ] = None
    tag: Annotated[
        list[str], opt("--tag", metavar="EMOJI", help="Saved-Messages reaction tag (Premium).")
    ] = []
    hashtag: Annotated[
        str | None, opt("--hashtag", metavar="TAG", help="Shorthand for '#tag'.")
    ] = None
    count: Annotated[
        bool, opt("--count", help="Return per-filter counters instead of messages.")
    ] = False
    calendar: Annotated[bool, opt("--calendar", help="Return the message calendar.")] = False
    position: Annotated[
        int | None, opt("--position", metavar="ID", help="Where this id sits in the filtered set.")
    ] = None
    regex: Annotated[
        str | None, opt("--regex", metavar="PATTERN", help="Client-side regex over the scan.")
    ] = None
    scan: Annotated[
        int, opt("--scan", metavar="N", help="How many messages --regex may scan.", ge=1)
    ] = 1000
    scheduled: Annotated[bool, opt("--scheduled", help="Search the scheduled drawer.")] = False


async def search(ctx: OpContext, req: SearchReq) -> Page[Message]:
    """Search one chat with every filter `messages.search` accepts.

    `--regex` is deliberately local and bounded: Telegram has no regex search,
    so tlgr scans a window and says how far it got rather than pretending the
    result is exhaustive.
    """
    limit, state = _window(ctx, "message.search", PageKind.SEARCH)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    query = req.query or (f"#{req.hashtag.lstrip('#')}" if req.hashtag else "")

    if req.count or req.calendar or req.position is not None:
        return await _search_meta(ctx, req, peer, chat_id, query)

    kwargs: dict[str, Any] = {
        "search": query or None,
        "reverse": req.reverse,
        "scheduled": req.scheduled,
        "offset_id": int(state.get("offset_id", 0)),
        "add_offset": int(state.get("add_offset", 0)),
    }
    if req.from_user is not None:
        kwargs["from_user"] = await _send.resolve(ctx, req.from_user)
    if req.type:
        kwargs["filter"] = _filter(req.type)
    if req.until:
        kwargs["offset_date"] = parse_dt(req.until)
    if req.min_id is not None:
        kwargs["min_id"] = req.min_id
    if req.max_id is not None:
        kwargs["max_id"] = req.max_id
    if req.topic is not None:
        kwargs["reply_to"] = req.topic
    if req.ids:
        kwargs["ids"] = _ids(tuple(req.ids))
    if req.tag:
        ctx.warn("--tag needs Premium and a Saved-Messages peer; it is passed through as given")

    scan = req.scan if req.regex else limit
    items = await _fetch(ctx, peer, chat_id=chat_id, limit=scan, **kwargs)
    if req.since:
        cutoff = to_unix(parse_dt(req.since)) or 0
        items = [m for m in items if m.date_unix >= cutoff]
    if req.regex:
        items = _regex_filter(items, req.regex, limit)
        ctx.warn(f"--regex is a local scan of at most {req.scan} messages, not a server search")

    next_state = {"offset_id": items[-1].id, "add_offset": 0} if items else {}
    return build_page(
        items[:limit],
        op="message.search",
        kind=PageKind.SEARCH,
        state=next_state,
        account=ctx.account,
        limit=limit,
        has_more=None if items else False,
    )


def _regex_filter(items: list[Message], pattern: str, limit: int) -> list[Message]:
    import re

    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise UsageError(f"--regex: {exc}", field="regex") from exc
    return [m for m in items if compiled.search(m.text)][:limit]


async def _search_meta(
    ctx: OpContext, req: SearchReq, peer: Any, chat_id: int, query: str
) -> Page[Message]:
    """`--count` / `--calendar` / `--position` return their own shapes.

    They are folded into `message search` rather than given their own verbs
    because they answer questions *about the same filtered set*, and a caller
    should not have to restate ten filters to ask "how many?".
    """
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    if req.count:
        filters = [_filter(name) for name in ([req.type] if req.type else list(FILTERS))]
        result = await client(fn.GetSearchCountersRequest(peer=peer, filters=filters))
        counters = {
            type(getattr(entry, "filter", None)).__name__: int(getattr(entry, "count", 0) or 0)
            for entry in (result or [])
        }
        ctx.warn(f"counters: {counters}")
        return Page(items=[], has_more=False, total=sum(counters.values()))
    if req.calendar:
        result = await client(
            fn.GetSearchResultsCalendarRequest(
                peer=peer, filter=_filter(req.type or "photo"), offset_id=0, offset_date=None
            )
        )
        items = [
            message_to_model(message, chat_id=chat_id)
            for message in (getattr(result, "messages", None) or [])
        ]
        return Page(items=items, has_more=False, total=getattr(result, "count", None))
    result = await client(
        fn.GetSearchResultsPositionsRequest(
            peer=peer, filter=_filter(req.type or "photo"), offset_id=req.position or 0, limit=1
        )
    )
    ctx.warn(f"position {req.position} of {getattr(result, 'count', '?')} in the filtered set")
    return Page(items=[], has_more=False, total=getattr(result, "count", None))


SPEC_SEARCH = OperationSpec(
    id="message.search",
    request=SearchReq,
    response=Page[Message],
    impl=search,
    summary="Search messages inside one chat",
    description=(
        "Every filter `messages.search` has: text, sender, media type, date "
        "range, topic, saved dialog and reaction tag. --regex is a bounded "
        "local scan and says so, because Telegram has no regex search."
    ),
    aliases=("msg.search",),
    legacy_paths=("message search", "msg search"),
    paginated=PageKind.SEARCH,
    columns=("id", "date", "text"),
    headers=("ID", "Date", "Text"),
    example={"items": [_EXAMPLE_MESSAGE], "has_more": False},
    example_args='message search @alice "invoice"',
    covers=(
        "messages-core.message-position-in-filter",
        "messages-core.saved-tags-search",
        "messages-core.search-calendar-positions",
        "messages-core.search-counters",
        "messages-core.search-date-range",
        "messages-core.search-filter-media-type",
        "messages-core.search-from-user",
        "messages-core.search-in-chat-text",
        "messages-core.search-in-saved-dialog",
        "messages-core.search-in-topic",
        "messages-core.search-local-regex",
        "poll.search",
        "reaction.search-by-tag",
    ),
)


class GetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat the message is in.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Message id or link.")]
    full: Annotated[bool, opt("--full", help="Include restriction reasons and raw media.")] = False
    with_reply: Annotated[
        bool, opt("--with-reply", help="Also resolve the replied-to message.")
    ] = False
    context: Annotated[
        int | None, opt("--context", metavar="N", help="Also return N messages around it.")
    ] = None
    format: Annotated[
        str | None, choice("md", "html", "text", "json", help="Render the text with its entities.")
    ] = None
    rich: Annotated[bool, opt("--rich", help="Fetch the layer-229 rich body.")] = False
    vcard_out: Annotated[
        str | None,
        opt("--vcard-out", metavar="PATH", kind="path", help="Write an attached contact card."),
    ] = None
    raw: Annotated[bool, opt("--raw", help="Include the raw TL object.")] = False
    scheduled: Annotated[bool, opt("--scheduled", help="Read from the scheduled drawer.")] = False


async def get(ctx: OpContext, req: GetReq) -> Message:
    """One message with everything hanging off it."""
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    client = _client(ctx)

    if req.rich:
        _send.require_supported(
            "--rich", "messages.getRichMessage needs layer 229 and the pinned Telethon is 227"
        )

    found = await _fetch(
        ctx, peer, chat_id=chat_id, limit=1, ids=[req.msg_id], scheduled=req.scheduled
    )
    if not found or not found[0].id:
        raise NotFoundError(f"message {req.msg_id} is not in that chat, or was deleted")
    message = found[0]
    message.scheduled = req.scheduled

    raw = None
    if req.raw or req.full or req.vcard_out:
        async for item in client.iter_messages(peer, ids=[req.msg_id]):
            raw = item
            break
    if req.raw and raw is not None:
        message.raw = {"tl_type": type(raw).__name__, "repr": str(raw)[:4000]}
    if req.full and raw is not None:
        message.restriction_reason = [
            f"{getattr(r, 'platform', '')}:{getattr(r, 'reason', '')}"
            for r in (getattr(raw, "restriction_reason", None) or [])
        ]
    if req.vcard_out and raw is not None:
        _write_vcard(req.vcard_out, raw)
    if req.format and req.format != "json":
        message.text = _render(message, req.format)
    if req.with_reply and message.reply_to and message.reply_to.message_id:
        parent = await _fetch(
            ctx, peer, chat_id=chat_id, limit=1, ids=[message.reply_to.message_id]
        )
        message.reply = parent[0] if parent else None
    if req.context:
        around = await _fetch(
            ctx,
            peer,
            chat_id=chat_id,
            limit=req.context * 2,
            offset_id=req.msg_id + req.context,
        )
        message.context = [m for m in around if m.id != message.id]
    return message


def _render(message: Message, mode: str) -> str:
    """Re-apply the entities as markup, for a human who asked for markup.

    Deliberately one-way and lossy — that is why the default is `json` and the
    raw text plus entities is what the JSON carries (core/text.py's rule).
    """
    if mode == "text" or not message.entities:
        return message.text
    marks = {"bold": ("**", "**"), "italic": ("_", "_"), "code": ("`", "`")}
    if mode == "html":
        marks = {"bold": ("<b>", "</b>"), "italic": ("<i>", "</i>"), "code": ("<code>", "</code>")}
    pieces: list[tuple[int, str]] = []
    for entity in message.entities:
        pair = marks.get(entity.type)
        if pair is None:
            continue
        pieces.append((entity.offset, pair[0]))
        pieces.append((entity.offset + entity.length, pair[1]))
    units = message.text.encode("utf-16-le")
    out: list[str] = []
    cursor = 0
    for offset, marker in sorted(pieces, key=lambda item: item[0]):
        out.append(units[cursor * 2 : offset * 2].decode("utf-16-le", "ignore"))
        out.append(marker)
        cursor = offset
    out.append(units[cursor * 2 :].decode("utf-16-le", "ignore"))
    return "".join(out)


def _write_vcard(path: str, raw: Any) -> None:
    media = getattr(raw, "media", None)
    vcard = getattr(media, "vcard", None)
    if not vcard:
        first = getattr(media, "first_name", "") or ""
        last = getattr(media, "last_name", "") or ""
        phone = getattr(media, "phone_number", "") or ""
        if not phone:
            raise UsageError("that message has no contact card to write", field="vcard_out")
        vcard = f"BEGIN:VCARD\nVERSION:3.0\nN:{last};{first}\nTEL:{phone}\nEND:VCARD\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(vcard)


SPEC_GET = OperationSpec(
    id="message.get",
    request=GetReq,
    response=Message,
    impl=get,
    summary="Get one message with its full metadata",
    description=(
        "Entities (UTF-16 offsets), forward header, reply context, media "
        "summary, reactions including whether this account already reacted, "
        "views, and the fact-check when one exists."
    ),
    aliases=("msg.get",),
    legacy_paths=("message get", "msg get"),
    empty_exit=EXIT_EMPTY,
    columns=("id", "date", "text"),
    example=_EXAMPLE_MESSAGE,
    example_args="message get @alice 12345",
    covers=(
        "bots.disabled-button",
        "bots.inline-keyboard-render",
        "bots.invoice-message-view",
        "bots.reply-keyboard-render",
        "calls.history-goto-message",
        "contact.card-fields",
        "dice.read-value",
        "groups-channels-admin.monoforum-message-author",
        "messages-core.message-copy-text",
        "messages-core.message-forward-header",
        "messages-core.message-get",
        "messages-core.message-get-reply-context",
        "messages-core.message-post-author",
        "messages-core.message-restriction-reason",
        "messages-core.thread-view-original-post",
    ),
    covers_partial=("bots.rich-message-view", "richmsg.get"),
    coverage_note="--rich is refused with NOT_SUPPORTED until Telethon carries layer 229.",
)


class ThreadListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel or group.")]
    root_id: Annotated[int, arg(1, metavar="ROOT_ID", kind="msg_id", help="Post or thread root.")]
    reverse: Annotated[bool, opt("--reverse", help="Oldest first.")] = False
    around: Annotated[
        int | None, opt("--around", metavar="ID", kind="msg_id", help="Centre on a comment.")
    ] = None
    resolve: Annotated[bool, opt(help="Resolve the post into its discussion thread first.")] = True


async def thread_list(ctx: OpContext, req: ThreadListReq) -> Page[Message]:
    """The replies under a thread root, or the comments under a channel post.

    A channel post's comments do not live in the channel: `getDiscussionMessage`
    maps the post to `(discussion group, root id)` first, which is why the
    result names the chat the comments are actually in.
    """
    from telethon.tl.functions import messages as fn

    limit, state = _window(ctx, "message.thread.list", PageKind.HISTORY)
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    root = req.root_id
    discussion_id = chat_id

    if req.resolve:
        try:
            mapped = await _client(ctx)(fn.GetDiscussionMessageRequest(peer, req.root_id))
        except Exception as exc:
            if "MSG_ID_INVALID" not in str(exc).upper():
                raise
            mapped = None
        messages = list(getattr(mapped, "messages", None) or [])
        if messages:
            peer = await _client(ctx).get_input_entity(messages[0].peer_id)
            discussion_id = _send.peer_id_of(peer)
            root = int(messages[0].id)

    offset_id = int(state.get("offset_id", 0)) or (req.around or 0)
    add_offset = int(state.get("add_offset", 0)) or (-(limit // 2) if req.around else 0)
    items = await _fetch(
        ctx,
        peer,
        chat_id=discussion_id,
        limit=limit,
        reply_to=root,
        reverse=req.reverse,
        offset_id=offset_id,
        add_offset=add_offset,
    )
    for message in items:
        message.thread_root = root
        message.discussion_chat_id = discussion_id
    next_state = {"offset_id": items[-1].id, "add_offset": 0} if items else {}
    return build_page(
        items,
        op="message.thread.list",
        kind=PageKind.HISTORY,
        state=next_state,
        account=ctx.account,
        limit=limit,
        has_more=None if items else False,
    )


SPEC_THREAD_LIST = OperationSpec(
    id="message.thread.list",
    request=ThreadListReq,
    response=Page[Message],
    impl=thread_list,
    summary="List the replies of a thread or the comments under a post",
    aliases=("message.comments",),
    paginated=PageKind.HISTORY,
    columns=("id", "date", "text"),
    example={"items": [_EXAMPLE_MESSAGE], "has_more": False},
    example_args="message thread list @channel 4242",
    covers=(
        "groups-channels-admin.comments-thread",
        "messages-core.comments-view",
        "messages-core.thread-view-replies",
    ),
)


class ThreadDisableReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Post id.")]


async def thread_disable(ctx: OpContext, req: ThreadDisableReq) -> SuggestedPostState:
    """Turn comments off on one post, by deleting the discussion-group copy.

    Telegram has no toggle for this. The only mechanism is deleting the
    auto-forwarded copy in the linked discussion group, and that destroys the
    comments that were already there — which is why the op is destructive.
    """
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    mapped = await _client(ctx)(fn.GetDiscussionMessageRequest(peer, req.msg_id))
    messages = list(getattr(mapped, "messages", None) or [])
    if not messages:
        _already(ctx)
        return SuggestedPostState(
            chat_id=_send.peer_id_of(peer), msg_id=req.msg_id, state="no-comments"
        )
    discussion = await _client(ctx).get_input_entity(messages[0].peer_id)
    await _client(ctx).delete_messages(discussion, [int(messages[0].id)])
    return SuggestedPostState(
        chat_id=_send.peer_id_of(peer), msg_id=req.msg_id, state="comments-disabled"
    )


SPEC_THREAD_DISABLE = OperationSpec(
    id="message.thread.disable",
    request=ThreadDisableReq,
    response=SuggestedPostState,
    impl=thread_disable,
    summary="Disable comments on a single channel post",
    description=(
        "Irreversible: the only mechanism Telegram offers is deleting the "
        "post's copy in the linked discussion group, which deletes the "
        "comments with it."
    ),
    mutating=True,
    destructive=True,
    rate_class="bulk",
    columns=("chat_id", "msg_id", "state"),
    example={"chat_id": -1001234, "msg_id": 4242, "state": "comments-disabled"},
    example_args="message thread disable @channel 4242",
    covers=("messages-core.comments-disable-on-post",),
)


# ---------------------------------------------------------------------------
# message edit / delete / forward
# ---------------------------------------------------------------------------


class EditReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Message id or link.")]
    text: Annotated[str, arg(2, metavar="TEXT", required=False, help="New text.")] = ""
    parse: Annotated[str | None, choice("md", "html", "none", help="Formatting of TEXT.")] = None
    entities: Annotated[
        str | None, opt("--entities", metavar="JSON", kind="json", help="Explicit entities.")
    ] = None
    caption: Annotated[str | None, opt("--caption", help="New media caption.")] = None
    caption_above: Annotated[bool, opt("--caption-above", help="invert_media.")] = False
    file: Annotated[
        str | None, opt("--file", metavar="PATH", kind="path", help="Replace the media.")
    ] = None
    no_preview: Annotated[bool, opt("--no-preview", help="Drop the link preview.")] = False
    preview_url: Annotated[
        str | None, opt("--preview-url", metavar="URL", help="Change the previewed URL.")
    ] = None
    preview_above: Annotated[bool, opt("--preview-above", help="invert_media.")] = False
    schedule: Annotated[
        str | None, opt("--schedule", metavar="TS|online", help="Reschedule a scheduled message.")
    ] = None
    repeat: Annotated[
        str | None, opt("--repeat", metavar="PERIOD", help="Change the repeat period (Premium).")
    ] = None
    toggle_task: Annotated[
        int | None, opt("--toggle-task", metavar="N", help="Flip a checkbox in a rich message.")
    ] = None
    rich_markdown: Annotated[
        str | None,
        opt("--rich-markdown", metavar="PATH", kind="path", help="Replace the rich body."),
    ] = None
    check: Annotated[
        bool, opt("--check", help="Report whether this message is still editable and stop.")
    ] = False
    typing: Annotated[
        float, opt("--typing", metavar="SECONDS", help="Type for N seconds first.", ge=0)
    ] = 0.0


async def edit(ctx: OpContext, req: EditReq) -> EditResult:
    """Edit a message's text, caption, media or schedule.

    MESSAGE_NOT_MODIFIED is reported as `already: true` rather than as an
    error: the world already looks the way the caller asked for, which is
    success (STYLE §4).
    """
    from telethon.tl.functions import messages as fn

    if req.rich_markdown or req.toggle_task is not None:
        _send.require_supported(
            "--rich-markdown/--toggle-task",
            "editing a layer-229 rich body needs an API layer the pinned Telethon lacks",
        )

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    client = _client(ctx)

    if req.check:
        data = await client(fn.GetMessageEditDataRequest(peer=peer, id=req.msg_id))
        return EditResult(
            id=req.msg_id,
            chat_id=chat_id,
            can_edit=True,
            caption=bool(getattr(data, "caption", False)),
        )

    await _send.show_typing(ctx, peer, _send.typing_seconds(req.text, requested=req.typing))

    source = req.caption if req.caption is not None else req.text
    text, entities = _send.body(source, parse=req.parse, entities=req.entities)
    media: Any = None
    if req.file:
        media = await _send.input_media(ctx, req.file)
    elif req.preview_url:
        from telethon.tl import types

        media = types.InputMediaWebPage(url=req.preview_url, optional=True)

    try:
        result = await client(
            fn.EditMessageRequest(
                peer=peer,
                id=req.msg_id,
                message=text or None,
                entities=_send.tl_entities(entities),
                media=media,
                no_webpage=req.no_preview or None,
                invert_media=(req.preview_above or req.caption_above) or None,
                schedule_date=_send.schedule_at(req.schedule),
                schedule_repeat_period=_send.repeat_period(req.repeat),
            )
        )
    except Exception as exc:
        if not _is_not_modified(exc):
            raise
        _already(ctx)
        return EditResult(id=req.msg_id, chat_id=chat_id, text=text, already=True)

    edited = _send.message_from_updates(result, chat_id=chat_id, sent_text=text)
    ctx.emit("message_edit", {"chat_id": chat_id, "id": req.msg_id})
    return EditResult(
        id=edited.id or req.msg_id,
        chat_id=chat_id,
        edit_date=edited.edit_date,
        text=edited.text or text,
        entities=edited.entities or entities,
    )


SPEC_EDIT = OperationSpec(
    id="message.edit",
    request=EditReq,
    response=EditResult,
    impl=edit,
    summary="Edit a message's text, caption, media or schedule",
    description=(
        "MESSAGE_NOT_MODIFIED is success with `already: true`. --check asks "
        "`messages.getMessageEditData` whether the message may still be "
        "edited, which is not derivable from its date: pinned, scheduled and "
        "Saved-Messages posts have no 48-hour window."
    ),
    aliases=("msg.edit",),
    legacy_paths=("message edit", "msg edit"),
    mutating=True,
    idempotent=True,
    rate_class="send",
    timeout_s=180,
    columns=("id", "chat_id", "edit_date", "text"),
    example={
        "id": 12345,
        "chat_id": 777123,
        "edit_date": "2026-09-03T09:20:00Z",
        "text": "on my way (5 min)",
    },
    example_args='message edit @alice 12345 "on my way (5 min)"',
    covers=(
        "messages-core.edit-caption",
        "messages-core.edit-check-permission",
        "messages-core.edit-text",
        "messages-core.scheduled-reschedule",
    ),
    covers_partial=("richmsg.tasks",),
    coverage_note="Checklist tasks live in a layer-229 rich body; --toggle-task is refused.",
)


class DeleteReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[
        tuple[str, ...],
        arg(1, metavar="MSG_ID", required=False, variadic=True, help="Ids or ranges (100-120)."),
    ] = ()
    for_everyone: Annotated[
        bool, opt("--for-everyone", help="Revoke for everyone (the default for own messages).")
    ] = True
    for_me: Annotated[bool, opt("--for-me", help="Delete only from my own history.")] = False
    from_user: Annotated[
        PeerRef | None,
        opt("--from", metavar="USER", kind="user", help="Every message this member sent."),
    ] = None
    ban: Annotated[bool, opt("--ban", help="With --from: also ban the member.")] = False
    report_spam: Annotated[bool, opt("--report-spam", help="With --from: report as spam.")] = False
    scheduled: Annotated[bool, opt("--scheduled", help="Delete scheduled messages instead.")] = (
        False
    )
    revert: Annotated[bool, opt("--revert", help="Revert an ephemeral message.")] = False


async def delete(ctx: OpContext, req: DeleteReq) -> DeleteResult:
    """Delete messages, for me or for everyone.

    `--from` is a different call with a different shape: it returns an
    `AffectedHistory` that has to be looped until the offset reaches zero,
    which is why it cannot simply be a filter over the id list.
    """
    from telethon.tl.functions import channels as ch

    if req.revert:
        _send.require_supported(
            "--revert", "ephemeral.revertMessage is layer 229 and absent from Telethon 1.44"
        )

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    client = _client(ctx)

    if req.from_user is not None:
        member = await _send.resolve(ctx, req.from_user)
        channel = await client.get_input_entity(peer)
        affected = await _affected_loop(
            ctx,
            lambda offset: ch.DeleteParticipantHistoryRequest(channel=channel, participant=member),
        )
        return DeleteResult(chat_id=chat_id, deleted=affected, affected=affected)

    ids = _ids(req.msg_id)
    if not ids:
        raise UsageError("give at least one message id, or --from <user>", field="msg_id")

    if req.scheduled:
        from telethon.tl.functions import messages as fn

        await client(fn.DeleteScheduledMessagesRequest(peer=peer, id=ids))
        return DeleteResult(chat_id=chat_id, deleted=len(ids), ids=ids, scheduled=True)

    revoke = not req.for_me and req.for_everyone
    result = await client.delete_messages(peer, ids, revoke=revoke)
    affected = sum(int(getattr(item, "pts_count", 0) or 0) for item in (result or []))
    ctx.emit("message_delete", {"chat_id": chat_id, "ids": ids})
    return DeleteResult(chat_id=chat_id, deleted=affected or len(ids), ids=ids, affected=affected)


SPEC_DELETE = OperationSpec(
    id="message.delete",
    request=DeleteReq,
    response=DeleteResult,
    impl=delete,
    summary="Delete messages for me or for everyone",
    description=(
        "In a channel or supergroup a delete is always for everyone — "
        "`channels.deleteMessages` has no revoke flag — so --for-me is only "
        "meaningful in private chats and basic groups."
    ),
    aliases=("msg.delete", "message.rm"),
    legacy_paths=("message delete", "msg delete"),
    mutating=True,
    destructive=True,
    rate_class="bulk",
    columns=("chat_id", "deleted"),
    example={"chat_id": 777123, "deleted": 2, "ids": [12345, 12346]},
    example_args="message delete @alice 12345",
    covers=(
        "messages-core.delete-all-from-user",
        "messages-core.delete-for-everyone",
        "messages-core.delete-for-me",
        "messages-core.scheduled-delete",
    ),
    covers_partial=("messages-core.ephemeral-messages",),
    coverage_note="--revert needs layer 229's ephemeral.* namespace and is refused.",
)


class ForwardReq(_send.SendOptions, kw_only=True):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Source chat.")]
    msg_id: Annotated[
        tuple[str, ...],
        arg(1, metavar="MSG_ID", required=False, variadic=True, help="Ids or ranges."),
    ] = ()
    to: Annotated[
        list[PeerRef],
        opt("--to", metavar="CHAT", kind="peer", help="Destination; repeatable."),
    ] = []
    no_author: Annotated[bool, opt("--no-author", help="drop_author: hide the sender.")] = False
    no_captions: Annotated[bool, opt("--no-captions", help="drop_media_captions.")] = False
    as_copy: Annotated[bool, opt("--as-copy", help="Re-send as a new message, not a forward.")] = (
        False
    )
    comment: Annotated[str | None, opt("--comment", help="Send this alongside the forward.")] = None
    video_at: Annotated[
        int | None, opt("--video-at", metavar="SECONDS", help="Start at a video timestamp.")
    ] = None
    with_score: Annotated[bool, opt("--with-score", help="Keep a game's scoreboard.")] = False


async def forward(ctx: OpContext, req: ForwardReq) -> Page[ForwardedMessage]:
    """Forward or copy messages to one or many chats.

    One request per destination, because Telegram checks each destination's
    restrictions separately and a single failure must not silently take the
    other destinations with it.
    """
    from telethon.tl.functions import messages as fn

    ids = _ids(req.msg_id)
    if not ids:
        raise UsageError("give at least one message id to forward", field="msg_id")
    if not req.to:
        raise UsageError("give at least one destination with --to", field="to")

    source = await _send.resolve(ctx, req.chat)
    source_id = _send.peer_id_of(source)
    client = _client(ctx)
    produced: list[ForwardedMessage] = []

    for index, destination in enumerate(req.to):
        target = await _send.resolve(ctx, destination)
        target_id = _send.peer_id_of(target)
        if index:
            limiter = getattr(ctx, "limiter", None)
            if limiter is not None:
                await limiter.acquire("send")
        result = await client(
            fn.ForwardMessagesRequest(
                from_peer=source,
                id=ids,
                to_peer=target,
                random_id=[_random_id() for _ in ids],
                drop_author=(req.no_author or req.as_copy) or None,
                drop_media_captions=req.no_captions or None,
                with_my_score=req.with_score or None,
                noforwards=req.protect or None,
                silent=req.silent or None,
                top_msg_id=req.topic,
                schedule_date=_send.schedule_at(req.schedule),
                send_as=await _send.resolve(ctx, req.send_as) if req.send_as else None,
                effect=_send.effect_id(req.effect),
                video_timestamp=req.video_at,
                allow_paid_stars=req.paid_stars,
            )
        )
        for offset, message in enumerate(_send.messages_from_updates(result, chat_id=target_id)):
            produced.append(
                ForwardedMessage(
                    id=message.id,
                    chat_id=target_id,
                    date=message.date,
                    date_unix=message.date_unix,
                    from_chat_id=source_id,
                    from_msg_id=ids[offset] if offset < len(ids) else None,
                )
            )
        if req.comment:
            await client(
                fn.SendMessageRequest(
                    peer=target,
                    message=req.comment,
                    random_id=_random_id(),
                    silent=req.silent or None,
                )
            )
    ctx.emit("message_forward", {"from_chat_id": source_id, "ids": ids, "count": len(produced)})
    return Page(items=produced, has_more=False, total=len(produced))


SPEC_FORWARD = OperationSpec(
    id="message.forward",
    request=ForwardReq,
    response=Page[ForwardedMessage],
    impl=forward,
    summary="Forward or copy messages to one or many chats",
    description=(
        "--as-copy is the same call with drop_author and fresh random ids, "
        "which is exactly what the GUI's 'forward without quoting' does."
    ),
    aliases=("msg.forward",),
    legacy_paths=("message forward", "msg forward"),
    mutating=True,
    rate_class="send",
    timeout_s=300,
    columns=("id", "chat_id", "from_msg_id"),
    example={
        "items": [
            {
                "id": 200,
                "chat_id": -1001234,
                "date": "2026-09-03T09:14:07Z",
                "date_unix": 1788340447,
                "from_chat_id": 777123,
                "from_msg_id": 12345,
            }
        ],
        "has_more": False,
    },
    example_args="message forward @alice 12345 --to @bobby",
    covers=(
        "game.forward",
        "messages-core.forward-as-copy-text",
        "messages-core.forward-basic",
        "messages-core.forward-hide-captions",
        "messages-core.forward-hide-sender",
        "messages-core.forward-options",
        "messages-core.forward-to-many",
        "messages-core.message-select-bulk",
    ),
)


# ---------------------------------------------------------------------------
# pin / unpin / read / react
# ---------------------------------------------------------------------------


class PinReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Message id or link.")]
    notify: Annotated[
        bool, opt("--notify", help="Notify members (a pin is silent by default).")
    ] = False
    both_sides: Annotated[
        bool, opt("--both-sides", help="Also pin on the other side of a private chat.")
    ] = False
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Pin inside a forum topic.")
    ] = None


async def pin(ctx: OpContext, req: PinReq) -> PinResult:
    """Pin a message. Silent unless --notify, which is how the GUI behaves."""
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    try:
        await _client(ctx)(
            fn.UpdatePinnedMessageRequest(
                peer=peer,
                id=req.msg_id,
                silent=not req.notify,
                pm_oneside=not req.both_sides,
            )
        )
    except Exception as exc:
        if not _is_not_modified(exc):
            raise
        _already(ctx)
        return PinResult(chat_id=chat_id, msg_id=req.msg_id, pinned=True, already=True)
    return PinResult(chat_id=chat_id, msg_id=req.msg_id, pinned=True)


SPEC_PIN = OperationSpec(
    id="message.pin",
    request=PinReq,
    response=PinResult,
    impl=pin,
    summary="Pin a message in a chat",
    description="The pinned list is `message list --type pinned`.",
    aliases=("msg.pin",),
    legacy_paths=("message pin", "msg pin"),
    mutating=True,
    idempotent=True,
    columns=("chat_id", "msg_id", "pinned"),
    example={"chat_id": 777123, "msg_id": 12345, "pinned": True},
    example_args="message pin @alice 12345",
    covers=(
        "groups-channels-admin.monoforum-pin",
        "groups-channels-admin.pinned-messages",
        "messages-core.pin-message",
    ),
)


class UnpinReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[
        int | None,
        arg(
            1, metavar="MSG_ID", required=False, kind="msg_id", help="Message id; omit with --all."
        ),
    ] = None
    unpin_all: Annotated[bool, opt("--all", help="Unpin everything.")] = False
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Only this forum topic.")
    ] = None
    direct_to: Annotated[
        PeerRef | None,
        opt("--direct-to", metavar="USER", kind="user", help="Only this monoforum topic."),
    ] = None


async def unpin(ctx: OpContext, req: UnpinReq) -> PinResult:
    """Unpin one message, or every pinned message.

    `unpinAllMessages` returns an `AffectedHistory` and has to be looped: one
    call unpins a page, and stopping there is why v1's equivalent left most
    of them pinned.
    """
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    saved = await _send.resolve(ctx, req.direct_to) if req.direct_to is not None else None

    if req.unpin_all or req.msg_id is None:
        count = await _affected_loop(
            ctx,
            lambda offset: fn.UnpinAllMessagesRequest(
                peer=peer, top_msg_id=req.topic, saved_peer_id=saved
            ),
        )
        return PinResult(chat_id=chat_id, pinned=False, unpinned=count, count=count)

    try:
        await _client(ctx)(
            fn.UpdatePinnedMessageRequest(peer=peer, id=req.msg_id, unpin=True, silent=True)
        )
    except Exception as exc:
        if not _is_not_modified(exc):
            raise
        _already(ctx)
        return PinResult(chat_id=chat_id, msg_id=req.msg_id, pinned=False, already=True)
    return PinResult(chat_id=chat_id, msg_id=req.msg_id, pinned=False, unpinned=1, count=1)


SPEC_UNPIN = OperationSpec(
    id="message.unpin",
    request=UnpinReq,
    response=PinResult,
    impl=unpin,
    summary="Unpin one message or every pinned message",
    aliases=("msg.unpin",),
    mutating=True,
    destructive=True,
    idempotent=True,
    rate_class="bulk",
    columns=("chat_id", "msg_id", "pinned"),
    example={"chat_id": 777123, "msg_id": 12345, "pinned": False, "unpinned": 1},
    example_args="message unpin @alice 12345",
    covers=("messages-core.unpin-all", "messages-core.unpin-message"),
)


class ReadReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to mark read.")]
    up_to: Annotated[
        int | None,
        opt("--up-to", metavar="ID", kind="msg_id", help="Read up to this id (default: latest)."),
    ] = None
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Read one forum topic.")
    ] = None
    direct_to: Annotated[
        PeerRef | None,
        opt("--direct-to", metavar="USER", kind="user", help="Read one monoforum topic."),
    ] = None
    saved_peer: Annotated[
        PeerRef | None,
        opt("--saved-peer", metavar="CHAT", kind="peer", help="Read one saved dialog."),
    ] = None
    contents: Annotated[
        list[str],
        opt("--contents", metavar="ID", help="Mark voice notes / round videos as listened."),
    ] = []
    mentions: Annotated[bool, opt("--mentions", help="Mark all unread mentions as read.")] = False
    reactions: Annotated[bool, opt("--reactions", help="Mark all unread reactions as read.")] = (
        False
    )


async def read(ctx: OpContext, req: ReadReq) -> ReadResult:
    """Mark history, mentions, reactions, media contents or a thread as read.

    Which RPC applies depends on the peer type and on which scope was asked
    for; dispatching on that in one place is what stops a channel read from
    silently doing nothing (`messages.readHistory` on a channel is a no-op).
    """
    from telethon.tl import types
    from telethon.tl.functions import channels as ch
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    client = _client(ctx)
    is_channel = isinstance(peer, types.InputPeerChannel)
    result = ReadResult(chat_id=chat_id)

    if req.contents:
        ids = _ids(tuple(req.contents))
        if is_channel:
            channel = await client.get_input_entity(peer)
            await client(ch.ReadMessageContentsRequest(channel=channel, id=ids))
        else:
            await client(fn.ReadMessageContentsRequest(id=ids))
        result.contents_read = ids

    if req.mentions:
        result.mentions_read = await _affected_loop(
            ctx, lambda offset: fn.ReadMentionsRequest(peer=peer, top_msg_id=req.topic)
        )
    if req.reactions:
        result.reactions_read = await _affected_loop(
            ctx, lambda offset: fn.ReadReactionsRequest(peer=peer, top_msg_id=req.topic)
        )
    if (req.contents or req.mentions or req.reactions) and req.up_to is None:
        # A scoped read was asked for and no history bound was given: reading
        # the whole history as well would clear a badge nobody asked to clear.
        return result

    max_id = req.up_to or 0
    if req.saved_peer is not None:
        saved = await _send.resolve(ctx, req.saved_peer)
        await client(fn.ReadSavedHistoryRequest(parent_peer=peer, peer=saved, max_id=max_id))
    elif req.topic is not None:
        await client(fn.ReadDiscussionRequest(peer=peer, msg_id=req.topic, read_max_id=max_id))
    elif is_channel:
        channel = await client.get_input_entity(peer)
        await client(ch.ReadHistoryRequest(channel=channel, max_id=max_id))
    else:
        affected = await client(fn.ReadHistoryRequest(peer=peer, max_id=max_id))
        result.still_unread = getattr(affected, "still_unread_count", None)
    result.read_up_to = max_id or None
    ctx.emit("message_read", {"chat_id": chat_id, "max_id": max_id})
    return result


SPEC_READ = OperationSpec(
    id="message.read",
    request=ReadReq,
    response=ReadResult,
    impl=read,
    summary="Mark a chat, thread, mentions or media contents as read",
    description=(
        "A read receipt is visible to the other side and it also clears the "
        "badge in the account owner's own client, which may be their only "
        "reminder that they owe a reply."
    ),
    aliases=("msg.read",),
    legacy_paths=("message read", "msg read"),
    mutating=True,
    idempotent=True,
    columns=("chat_id", "read_up_to"),
    example={"chat_id": 777123, "read_up_to": 12345},
    example_args="message read @alice",
    covers=(
        "messages-core.monoforum-topic-manage",
        "messages-core.read-mark-history",
        "messages-core.read-message-contents",
        "messages-core.report-message-delivery",
        "messages-core.thread-read",
        "messages-core.unread-mentions-read-all",
    ),
)


class ReactReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Message id or link.")]
    emoji: Annotated[
        str, arg(2, metavar="EMOJI", required=False, help="Reaction; omit to clear.")
    ] = ""
    big: Annotated[bool, opt("--big", help="Play the big animation.")] = False
    add: Annotated[bool, opt("--add", help="Keep the reactions already there (Premium).")] = False


async def react(ctx: OpContext, req: ReactReq) -> ReactResult:
    """React to a message, or clear the reaction by passing no emoji.

    Kept in the `message` group because that is the documented v1 path; the
    `reaction` group of PR-9 owns the rest of the surface and will take this
    over as an alias.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    reactions = [types.ReactionEmoji(emoticon=req.emoji)] if req.emoji else []
    try:
        result = await _client(ctx)(
            fn.SendReactionRequest(
                peer=peer,
                msg_id=req.msg_id,
                reaction=reactions or None,
                big=req.big or None,
                add_to_recent=req.add or None,
            )
        )
    except Exception as exc:
        if not _is_not_modified(exc):
            raise
        _already(ctx)
        return ReactResult(
            chat_id=chat_id, msg_id=req.msg_id, emoji=req.emoji, reacted=True, already=True
        )
    summary: ReactionSummary | None = None
    produced = _send.messages_from_updates(result, chat_id=chat_id)
    if produced:
        summary = produced[0].reactions
    return ReactResult(
        chat_id=chat_id,
        msg_id=req.msg_id,
        emoji=req.emoji,
        reacted=bool(req.emoji),
        reactions=summary,
    )


SPEC_REACT = OperationSpec(
    id="message.react",
    request=ReactReq,
    response=ReactResult,
    impl=react,
    summary="React to a message with an emoji",
    description=(
        "Passing no emoji clears the reaction. A duplicate reaction is "
        "`already: true`, not an error — Telegram answers it with "
        "MESSAGE_NOT_MODIFIED."
    ),
    aliases=("msg.react",),
    legacy_paths=("message react", "msg react"),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("chat_id", "msg_id", "emoji", "reacted"),
    example={"chat_id": 777123, "msg_id": 12345, "emoji": "👍", "reacted": True},
    example_args="message react @alice 12345 👍",
    covers=("messages-core.reaction-add-remove",),
    coverage_note=(
        "The v1 path, kept invocable. PR-9's `reaction` group owns the full "
        "reaction surface and adopts this as an alias."
    ),
)


# ---------------------------------------------------------------------------
# link / views / read receipts / scheduled
# ---------------------------------------------------------------------------


class LinkReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Message id.")]
    topic: Annotated[bool, opt("--topic", help="Link into the comment thread.")] = False
    comment: Annotated[
        int | None, opt("--comment", metavar="ID", help="Link to a specific comment.")
    ] = None
    single: Annotated[bool, opt("--single", help="Link to this message only (?single).")] = False
    embed: Annotated[bool, opt("--embed", help="Embeddable link (?embed=1).")] = False
    at: Annotated[
        int | None, opt("--at", metavar="DURATION", kind="duration", help="Media timestamp.")
    ] = None
    poll_option: Annotated[
        int | None, opt("--poll-option", metavar="N", help="Preselect a poll option.")
    ] = None
    task: Annotated[int | None, opt("--task", metavar="N", help="Link to a checklist task.")] = None
    public: Annotated[bool, opt("--public", help="Prefer the @username form.")] = False


async def link(ctx: OpContext, req: LinkReq) -> LinkResult:
    """Build a t.me link to a message.

    `channels.exportMessageLink` is asked first because only the server knows
    whether a supergroup is public; falling back to arithmetic for a private
    one is what makes this work without a second round trip.
    """
    from telethon.tl.functions import channels as ch

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    url = ""
    public = False
    try:
        channel = await _client(ctx).get_input_entity(peer)
        exported = await _client(ctx)(
            ch.ExportMessageLinkRequest(channel=channel, id=req.msg_id, thread=req.topic or None)
        )
        url = str(getattr(exported, "link", "") or "")
        public = "/c/" not in url
    except Exception as exc:
        if "CHANNEL_INVALID" not in str(exc).upper() and "PEER_ID_INVALID" not in str(exc).upper():
            raise
    if not url:
        if chat_id >= 0:
            raise UsageError(
                "a private chat has no shareable message link; only channels and supergroups do",
                field="chat",
            )
        url = f"https://t.me/c/{-1000000000000 - chat_id}/{req.msg_id}"

    query: list[str] = []
    if req.single:
        query.append("single")
    if req.embed:
        query.append("embed=1")
    if req.comment is not None:
        query.append(f"comment={req.comment}")
    if req.at is not None:
        query.append(f"t={req.at}")
    if req.poll_option is not None:
        query.append(f"vote={req.poll_option}")
    if req.task is not None:
        query.append(f"task={req.task}")
    if query:
        url = f"{url}?{'&'.join(query)}"
    return LinkResult(link=url, public=public, thread=req.topic, chat_id=chat_id, msg_id=req.msg_id)


SPEC_LINK = OperationSpec(
    id="message.link",
    request=LinkReq,
    response=LinkResult,
    impl=link,
    summary="Build a t.me link to a message",
    description=(
        "A private supergroup produces a `t.me/c/<internal id>/<msg id>` link "
        "that only members can open; `public` says which form came back."
    ),
    columns=("link", "public"),
    example={
        "link": "https://t.me/durov/42",
        "public": True,
        "chat_id": -1001234,
        "msg_id": 42,
    },
    example_args="message link @durov 42",
    covers=(
        "groups-channels-admin.export-message-link",
        "messages-core.message-link-create",
        "poll.option-deep-link",
        "todo.task-deep-link",
    ),
)


class ViewReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel.")]
    msg_id: Annotated[
        tuple[str, ...], arg(1, metavar="MSG_ID", variadic=True, help="Post ids or ranges.")
    ]
    increment: Annotated[
        bool, opt("--increment", help="Actually register a view (off by default).")
    ] = False
    listened_seconds: Annotated[
        int | None,
        opt("--listened-seconds", metavar="N", help="Report real audio playback seconds."),
    ] = None


async def view_get(ctx: OpContext, req: ViewReq) -> Page[ViewCount]:
    """Views and forwards for channel posts.

    `--increment` is opt-in because `increment=True` registers a view
    server-side: reading a counter must not change it.
    """
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    ids = _ids(req.msg_id)
    result = await _client(ctx)(
        fn.GetMessagesViewsRequest(peer=peer, id=ids, increment=req.increment)
    )
    items: list[ViewCount] = []
    for index, view in enumerate(getattr(result, "views", None) or []):
        replies = getattr(view, "replies", None)
        items.append(
            ViewCount(
                msg_id=ids[index] if index < len(ids) else 0,
                views=getattr(view, "views", None),
                forwards=getattr(view, "forwards", None),
                replies=getattr(replies, "replies", None),
            )
        )
    if req.listened_seconds is not None:
        await _client(ctx)(
            fn.ReportMusicListenRequest(peer=peer, id=ids[0], duration=req.listened_seconds)
        )
    return Page(items=items, has_more=False, total=len(items))


SPEC_VIEW_GET = OperationSpec(
    id="message.view.get",
    request=ViewReq,
    response=Page[ViewCount],
    impl=view_get,
    summary="Views and forwards counters for channel posts",
    aliases=("message.views",),
    tags=frozenset({"mutating-checked"}),
    columns=("msg_id", "views", "forwards"),
    example={"items": [{"msg_id": 42, "views": 1200, "forwards": 8}], "has_more": False},
    example_args="message view get @durov 42",
    covers=("messages-core.message-views-forwards", "messages-core.report-music-listen"),
)


class ReadReceiptReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Message id.")]
    users: Annotated[bool, opt(help="Resolve reader ids to user objects.")] = True


async def read_receipt_list(ctx: OpContext, req: ReadReceiptReq) -> ReadReceipts:
    """Who read this message, and when.

    Every refusal here is reported as `unavailable_reason`, never as an
    error: "the group is too large for read marks" and "they hide their read
    date" are facts about the world, not failures of the request.
    """
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    client = _client(ctx)
    out = ReadReceipts(msg_id=req.msg_id)
    try:
        participants = await client(
            fn.GetMessageReadParticipantsRequest(peer=peer, msg_id=req.msg_id)
        )
        out.readers = [int(getattr(p, "user_id", p)) for p in (participants or [])]
    except Exception as exc:
        out.unavailable_reason = _receipt_reason(exc)

    try:
        read_date = await client(fn.GetOutboxReadDateRequest(peer=peer, msg_id=req.msg_id))
        out.read_date = fmt_dt(getattr(read_date, "date", None))
        out.read_date_unix = to_unix(getattr(read_date, "date", None))
    except Exception as exc:
        if out.unavailable_reason is None:
            out.unavailable_reason = _receipt_reason(exc)

    if req.users and out.readers:
        for reader in out.readers:
            try:
                out.users.append(entity_to_peer(await client.get_entity(reader)))
            except Exception:
                continue
    if not out.readers and out.read_date is None and out.unavailable_reason is None:
        out.expired = True
    return out


def _receipt_reason(exc: BaseException) -> str:
    text = str(exc).upper()
    if "PRIVACY" in text:
        return "one side hides read dates, so Telegram will not say when it was read"
    if "TOO_OLD" in text or "EXPIRED" in text:
        return "the read-mark window for this chat has expired"
    if "CHAT_TOO_BIG" in text or "PARTICIPANTS_TOO_FEW" in text:
        return "this chat is outside Telegram's read-mark size limits"
    return f"unavailable: {exc}"


SPEC_READ_RECEIPT_LIST = OperationSpec(
    id="message.read-receipt.list",
    request=ReadReceiptReq,
    response=ReadReceipts,
    impl=read_receipt_list,
    summary="Who read this message, and when",
    aliases=("message.readers", "msg.read-receipts", "message.read-receipts"),
    columns=("msg_id", "read_date"),
    example={"msg_id": 12345, "readers": [777], "read_date": "2026-09-03T09:20:00Z"},
    example_args="message read-receipt list @alice 12345",
    covers=("messages-core.read-date-private", "messages-core.read-participants-group"),
)


class ScheduledSendReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[
        tuple[str, ...], arg(1, metavar="MSG_ID", variadic=True, help="Scheduled message ids.")
    ]


async def scheduled_send(ctx: OpContext, req: ScheduledSendReq) -> Page[ScheduledSent]:
    """Send scheduled messages now, ahead of their time."""
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    ids = _ids(req.msg_id)
    result = await _client(ctx)(fn.SendScheduledMessagesRequest(peer=peer, id=ids))
    items = [
        ScheduledSent(id=m.id, chat_id=chat_id, date=m.date, date_unix=m.date_unix)
        for m in _send.messages_from_updates(result, chat_id=chat_id)
    ]
    if not items:
        items = [ScheduledSent(id=i, chat_id=chat_id) for i in ids]
    return Page(items=items, has_more=False, total=len(items))


SPEC_SCHEDULED_SEND = OperationSpec(
    id="message.scheduled.send",
    request=ScheduledSendReq,
    response=Page[ScheduledSent],
    impl=scheduled_send,
    summary="Send scheduled messages now",
    description=(
        "Listing, editing and deleting the drawer are flags elsewhere: "
        "`message list --scheduled`, `message edit --schedule`, "
        "`message delete --scheduled`."
    ),
    mutating=True,
    rate_class="send",
    columns=("id", "chat_id", "date"),
    example={"items": [{"id": 999, "chat_id": 777123}], "has_more": False},
    example_args="message scheduled send @alice 999",
    covers=("messages-core.scheduled-send-now",),
)


# ---------------------------------------------------------------------------
# text services: entities, translate, transcribe, summarize, compose, preview
# ---------------------------------------------------------------------------


class EntityListReq(Request):
    text: Annotated[
        str, arg(0, metavar="TEXT", required=False, help="Text to parse; '-' reads stdin.")
    ] = ""
    parse: Annotated[str | None, choice("md", "html", "none", help="Dialect to parse.")] = None
    stdin: Annotated[bool, opt("--stdin", help="Read from stdin.")] = False
    offset_units: Annotated[
        str | None, choice("utf16", "codepoint", "byte", help="Units for the emitted offsets.")
    ] = None
    render: Annotated[
        str | None, choice("md", "html", "text", help="Render an entity vector back to text.")
    ] = None
    entities: Annotated[
        str | None, opt("--entities", metavar="JSON", kind="json", help="Entities to render.")
    ] = None


#: Entities the server re-derives. Sending them back is an error, and telling
#: a caller which ones they are is the whole point of splitting them out.
AUTO_ENTITIES = frozenset(
    {"url", "email", "mention", "hashtag", "cashtag", "bot_command", "phone", "bank_card"}
)


async def entity_list(ctx: OpContext, req: EntityListReq) -> EntityReport:
    """Show what a parse mode did, in the units Telegram counts in.

    Runs locally, without an account: this is the answer to "why is my bold
    in the wrong place", and it is UTF-16 offsets every time.
    """
    text, entities = _send.body(req.text, parse=req.parse, entities=req.entities, stdin=req.stdin)
    manual = [e for e in entities if e.type not in AUTO_ENTITIES]
    automatic = [e for e in entities if e.type in AUTO_ENTITIES]
    if req.offset_units and req.offset_units != "utf16":
        manual = [_recount(text, e, req.offset_units) for e in manual]
        automatic = [_recount(text, e, req.offset_units) for e in automatic]
    report = EntityReport(
        text=text,
        entities=manual,
        auto_entities=automatic,
        length_utf16=utf16_len(text),
        would_split=len(_send.split_text(text, entities)),
    )
    if req.render:
        stub = Message(id=0, chat_id=0, date="", date_unix=0, text=text, entities=entities)
        report.rendered = _render(stub, req.render)
    return report


def _recount(text: str, entity: MessageEntity, units: str) -> MessageEntity:
    """Re-express a UTF-16 offset in code points or bytes."""
    raw = text.encode("utf-16-le")
    prefix = raw[: entity.offset * 2].decode("utf-16-le", "ignore")
    body = raw[entity.offset * 2 : (entity.offset + entity.length) * 2].decode(
        "utf-16-le", "ignore"
    )
    if units == "codepoint":
        offset, length = len(prefix), len(body)
    else:
        offset, length = len(prefix.encode()), len(body.encode())
    return MessageEntity(
        type=entity.type,
        offset=offset,
        length=length,
        url=entity.url,
        user_id=entity.user_id,
        language=entity.language,
        document_id=entity.document_id,
        collapsed=entity.collapsed,
    )


SPEC_ENTITY_LIST = OperationSpec(
    id="message.entity.list",
    request=EntityListReq,
    response=EntityReport,
    impl=entity_list,
    summary="Parse or inspect formatted text and its entities",
    description=(
        "Offsets are UTF-16 code units, which is the classic third-party "
        "client bug: an emoji is one character and two units. Automatic "
        "entities are listed separately because the server re-derives them "
        "and sending them back is an error."
    ),
    aliases=("message.entities",),
    needs_account=False,
    needs_auth=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=10,
    columns=("text", "length_utf16"),
    example={
        "text": "hello world",
        "entities": [{"type": "bold", "offset": 0, "length": 5}],
        "length_utf16": 11,
        "would_split": 1,
    },
    example_args='message entity list --parse md "**hello** world"',
    covers=(
        "messages-core.entities-detect-local",
        "messages-core.format-entity-offsets-utf16",
        "messages-core.format-raw-entities-json",
    ),
)


class TranslateReq(Request):
    chat: Annotated[
        PeerRef | None, arg(0, metavar="CHAT", required=False, kind="peer", help="Chat.")
    ] = None
    msg_id: Annotated[
        tuple[str, ...],
        arg(1, metavar="MSG_ID", required=False, variadic=True, help="Message ids."),
    ] = ()
    lang: Annotated[
        str | None, opt("--lang", "--to", metavar="CODE", help="Target language code.")
    ] = None
    text: Annotated[str | None, opt("--text", help="Translate this text instead.")] = None
    rich: Annotated[bool, opt("--rich", help="Translate a layer-229 rich body.")] = False


async def translate(ctx: OpContext, req: TranslateReq) -> Page[Translation]:
    """Translate messages, or arbitrary text, into a target language."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if req.rich:
        _send.require_supported(
            "--rich", "messages.translateRichMessage is layer 229 and absent from Telethon 1.44"
        )
    if not req.lang:
        raise UsageError("--lang is required (a two-letter language code)", field="lang")

    kwargs: dict[str, Any] = {"to_lang": req.lang}
    ids: list[int] = []
    if req.text is not None:
        plain, entities = _send.body(req.text, parse="none")
        kwargs["text"] = [
            types.TextWithEntities(text=plain, entities=_send.tl_entities(entities) or [])
        ]
    else:
        if req.chat is None:
            raise UsageError("give a chat and message ids, or --text", field="chat")
        kwargs["peer"] = await _send.resolve(ctx, req.chat)
        ids = _ids(req.msg_id)
        if not ids:
            raise UsageError("give at least one message id to translate", field="msg_id")
        kwargs["id"] = ids

    result = await _client(ctx)(fn.TranslateTextRequest(**kwargs))
    items: list[Translation] = []
    for index, piece in enumerate(getattr(result, "result", None) or []):
        items.append(
            Translation(
                msg_id=ids[index] if index < len(ids) else None,
                text=str(getattr(piece, "text", "") or ""),
                entities=message_entities(piece),
                lang=req.lang,
            )
        )
    return Page(items=items, has_more=False, total=len(items))


SPEC_TRANSLATE = OperationSpec(
    id="message.translate",
    request=TranslateReq,
    response=Page[Translation],
    impl=translate,
    summary="Translate messages or text into a target language",
    columns=("msg_id", "lang", "text"),
    example={"items": [{"msg_id": 12345, "text": "on my way", "lang": "en"}], "has_more": False},
    example_args="message translate @alice 12345 --lang en",
    covers=("dialogs.translate-messages", "messages-core.translate-message"),
    covers_partial=("bots.rich-message-translate", "richmsg.translate"),
    coverage_note="Rich-body translation is layer 229 and refused with NOT_SUPPORTED.",
)


class TranscribeReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Voice or round video.")]
    rate: Annotated[
        str | None, choice("good", "bad", help="Rate a transcription already produced.")
    ] = None
    wait: Annotated[bool, opt(help="Poll until the transcription is final.")] = True


async def transcribe(ctx: OpContext, req: TranscribeReq) -> Transcription:
    """Transcribe a voice note or round video.

    The first response is usually `pending`; the final text arrives in an
    update, so `--wait` re-asks rather than reporting an empty transcript as
    the answer.
    """
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    client = _client(ctx)
    result = await client(fn.TranscribeAudioRequest(peer=peer, msg_id=req.msg_id))
    out = Transcription(
        msg_id=req.msg_id,
        text=str(getattr(result, "text", "") or ""),
        pending=bool(getattr(result, "pending", False)),
        transcription_id=getattr(result, "transcription_id", None),
    )
    attempts = 0
    while req.wait and out.pending and attempts < 10:
        attempts += 1
        await asyncio.sleep(1.0)
        result = await client(fn.TranscribeAudioRequest(peer=peer, msg_id=req.msg_id))
        out.text = str(getattr(result, "text", "") or out.text)
        out.pending = bool(getattr(result, "pending", False))
    if req.rate and out.transcription_id is not None:
        await client(
            fn.RateTranscribedAudioRequest(
                peer=peer,
                msg_id=req.msg_id,
                transcription_id=out.transcription_id,
                good=req.rate == "good",
            )
        )
        out.rated = req.rate
    return out


SPEC_TRANSCRIBE = OperationSpec(
    id="message.transcribe",
    request=TranscribeReq,
    response=Transcription,
    impl=transcribe,
    summary="Transcribe a voice note or round video",
    tags=frozenset({"mutating-checked"}),
    timeout_s=180,
    columns=("msg_id", "text", "pending"),
    example={"msg_id": 12345, "text": "call me back", "pending": False},
    example_args="message transcribe @alice 12345",
    covers=("media.transcribe-voice",),
)


class SummarizeReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Message id.")]
    lang: Annotated[
        str | None, opt("--lang", "--to", metavar="CODE", help="Summarize into this language.")
    ] = None
    tone: Annotated[str | None, opt("--tone", metavar="SLUG", help="Composition tone.")] = None


async def summarize(ctx: OpContext, req: SummarizeReq) -> SummaryResult:
    """Summarize a long message with Telegram's own AI."""
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    result = await _client(ctx)(
        fn.SummarizeTextRequest(peer=peer, id=req.msg_id, to_lang=req.lang, tone=req.tone)
    )
    piece = getattr(result, "result", None) or result
    return SummaryResult(
        msg_id=req.msg_id,
        text=str(getattr(piece, "text", "") or ""),
        entities=message_entities(piece),
        quota_left=getattr(result, "quota_left", None),
    )


SPEC_SUMMARIZE = OperationSpec(
    id="message.summarize",
    request=SummarizeReq,
    response=SummaryResult,
    impl=summarize,
    summary="Summarize a long message with AI",
    description="Shares its quota with `message compose`; Premium raises it.",
    timeout_s=180,
    columns=("msg_id", "text"),
    example={"msg_id": 12345, "text": "They are running late."},
    example_args="message summarize @alice 12345",
    covers=("messages-core.summarize-message",),
)


class ComposeReq(Request):
    text: Annotated[
        str, arg(0, metavar="TEXT", required=False, help="Text to rewrite; '-' reads stdin.")
    ] = ""
    stdin: Annotated[bool, opt("--stdin", help="Read from stdin.")] = False
    proofread: Annotated[bool, opt("--proofread", help="Fix grammar and spelling.")] = False
    translate: Annotated[
        str | None, opt("--translate", metavar="LANG", help="Translate into this language.")
    ] = None
    tone: Annotated[str | None, opt("--tone", metavar="SLUG", help="Rewrite in this tone.")] = None
    emojify: Annotated[bool, opt("--emojify", help="Add emoji.")] = False
    rich: Annotated[
        str | None, opt("--rich", metavar="PATH", kind="path", help="Compose a rich body.")
    ] = None
    diff: Annotated[
        str | None, choice("unified", "inline", "json", help="Shape of the proofreading diff.")
    ] = None
    send_to: Annotated[
        PeerRef | None,
        opt("--send-to", metavar="CHAT", kind="peer", help="Send the result instead of printing."),
    ] = None


async def compose(ctx: OpContext, req: ComposeReq) -> ComposeResult:
    """Rewrite text with Telegram's AI: proofread, translate, tone, emojify.

    The diff entities only mean something when proofreading is the sole mode,
    because they are expressed against the original text; mixing a
    translation in makes them unreadable, so that combination is warned about
    rather than silently reported.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if req.rich:
        _send.require_supported(
            "--rich", "messages.composeRichMessageWithAI is layer 229 and absent from Telethon 1.44"
        )
    text, entities = _send.body(req.text, parse="none", stdin=req.stdin)
    if not text:
        raise UsageError("give some text to rewrite", field="text")
    if req.proofread and (req.translate or req.tone or req.emojify):
        ctx.warn("diff entities are only produced when --proofread is the only mode")

    tone = types.InputAiComposeToneSlug(slug=req.tone) if req.tone else None
    result = await _client(ctx)(
        fn.ComposeMessageWithAIRequest(
            text=types.TextWithEntities(text=text, entities=_send.tl_entities(entities) or []),
            proofread=req.proofread or None,
            emojify=req.emojify or None,
            translate_to_lang=req.translate,
            tone=tone,
        )
    )
    piece = getattr(result, "result", None) or result
    diff = getattr(result, "diff", None)
    out = ComposeResult(
        text=str(getattr(piece, "text", "") or ""),
        entities=message_entities(piece),
        diff_text=str(getattr(diff, "text", "")) if diff is not None else None,
        diff_entities=message_entities(diff) if diff is not None else [],
        quota_left=getattr(result, "quota_left", None),
    )
    if req.send_to is not None:
        out.sent = await send(ctx, SendReq(chat=req.send_to, text=out.text, parse="none"))
    return out


SPEC_COMPOSE = OperationSpec(
    id="message.compose",
    request=ComposeReq,
    response=ComposeResult,
    impl=compose,
    summary="Rewrite text with Telegram's AI",
    tags=frozenset({"mutating-checked"}),
    timeout_s=180,
    columns=("text",),
    example={"text": "I am on my way.", "quota_left": 9},
    example_args='message compose --proofread "im on my way"',
    covers=(
        "ai.compose",
        "appearance.ai-compose-run",
        "messages-core.ai-compose",
        "messages-core.format-diff-entities",
    ),
    covers_partial=("richmsg.compose-ai",),
    coverage_note="Composing a rich body needs layer 229 and is refused with NOT_SUPPORTED.",
)


class PreviewReq(Request):
    url: Annotated[str, arg(0, metavar="URL", help="URL to preview.")]
    refresh: Annotated[bool, opt("--refresh", help="Bypass the cached webpage hash.")] = False


async def preview(ctx: OpContext, req: PreviewReq) -> WebPagePreview:
    """Fetch the link preview Telegram would attach to a URL.

    `hash=0` always: the cached-hash protocol saves a round trip only for a
    client keeping a webpage cache, and tlgr keeps none, so asking for the
    cached form would just answer `webPageNotModified` with nothing in it.
    """
    from telethon.tl.functions import messages as fn

    from tlgr.ops._serialize import media_summary, photo_summary

    result = await _client(ctx)(fn.GetWebPageRequest(url=req.url, hash=0))
    page = getattr(result, "webpage", None) or result
    name = type(page).__name__
    document = getattr(page, "document", None)
    photo = getattr(page, "photo", None)

    return WebPagePreview(
        url=str(getattr(page, "url", req.url) or req.url),
        type=getattr(page, "type", None),
        site_name=getattr(page, "site_name", None),
        title=getattr(page, "title", None),
        description=getattr(page, "description", None),
        photo=photo_summary(photo),
        document=media_summary(page) if document is not None else None,
        has_large_media=bool(getattr(page, "has_large_media", False)),
        cached_page=getattr(page, "cached_page", None) is not None,
        pending=name == "WebPagePending",
    )


SPEC_PREVIEW = OperationSpec(
    id="message.preview",
    request=PreviewReq,
    response=WebPagePreview,
    impl=preview,
    summary="Fetch the link preview Telegram would attach to a URL",
    description="Sending that preview as the media is `message send --preview-url`.",
    columns=("url", "site_name", "title"),
    example={"url": "https://telegram.org", "site_name": "Telegram", "title": "Telegram"},
    example_args="message preview https://telegram.org",
    covers=("webpage.preview-fetch",),
)


# ---------------------------------------------------------------------------
# catalogs: effects and dice
# ---------------------------------------------------------------------------


class EffectListReq(Request):
    premium_only: Annotated[
        bool, opt("--premium-only", help="Only effects that require Premium.")
    ] = False
    refresh: Annotated[bool, opt("--refresh", help="Ignore the cached hash.")] = False


async def effect_list(ctx: OpContext, req: EffectListReq) -> Page[Effect]:
    """The animated message effects `--effect` accepts."""
    from telethon.tl.functions import messages as fn

    limit, _ = _window(ctx, "message.effect.list", PageKind.LOCAL, default=100)
    result = await _client(ctx)(fn.GetAvailableEffectsRequest(hash=0))
    items = [
        Effect(
            id=int(getattr(effect, "id", 0) or 0),
            emoticon=str(getattr(effect, "emoticon", "") or ""),
            premium_required=bool(getattr(effect, "premium_required", False)),
            static_icon_id=getattr(effect, "static_icon_id", None),
            effect_animation_id=getattr(effect, "effect_animation_id", None),
            effect_sticker_id=getattr(effect, "effect_sticker_id", None),
        )
        for effect in (getattr(result, "effects", None) or [])
    ]
    if req.premium_only:
        items = [effect for effect in items if effect.premium_required]
    return Page(items=items[:limit], has_more=len(items) > limit, total=len(items))


SPEC_EFFECT_LIST = OperationSpec(
    id="message.effect.list",
    request=EffectListReq,
    response=Page[Effect],
    impl=effect_list,
    summary="Browse the animated message effects",
    description="Effects apply to private chats only, and some need Premium to send.",
    aliases=("effect.list",),
    paginated=PageKind.LOCAL,
    columns=("id", "emoticon", "premium_required"),
    example={"items": [{"id": 5104841245755180586, "emoticon": "🔥"}], "has_more": False},
    example_args="message effect list",
    covers=("messages-core.message-effects-catalog",),
)


class DiceListReq(Request):
    stake: Annotated[bool, opt("--stake", help="Also return the staked-dice game state.")] = False
    refresh: Annotated[bool, opt("--refresh", help="Re-read help.getAppConfig.")] = False


async def dice_list(ctx: OpContext, req: DiceListReq) -> DiceCatalog:
    """Which dice emoji exist and what counts as a win.

    Read from `help.getAppConfig` every time rather than hardcoded: a client
    with a frozen list reports a newly added dice emoji as unsupported media.
    """
    from telethon.tl.functions import help as help_fn

    config = await _client(ctx)(help_fn.GetAppConfigRequest(hash=0))
    values = _app_config(config)
    emojis = [str(item) for item in (values.get("emojies_send_dice") or [])]
    raw_success = values.get("emojies_send_dice_success") or {}
    success = {
        str(emoji): int((info or {}).get("value", 0))
        for emoji, info in raw_success.items()
        if isinstance(info, dict)
    }
    catalog = DiceCatalog(emojis=emojis, success_values=success)
    if req.stake:
        from telethon.tl.functions import messages as fn

        state = await _client(ctx)(fn.GetEmojiGameInfoRequest())
        catalog.stake = {"tl_type": type(state).__name__, "read_only": True}
        ctx.warn("staked dice is read-only in tlgr: placing a TON stake is refused")
    return catalog


def _app_config(config: Any) -> dict[str, Any]:
    """`help.appConfig` as plain Python, whatever JSON node shape it uses."""

    def unwrap(node: Any) -> Any:
        name = type(node).__name__
        if name == "JsonObject":
            return {str(v.key): unwrap(v.value) for v in (getattr(node, "value", None) or [])}
        if name == "JsonArray":
            return [unwrap(v) for v in (getattr(node, "value", None) or [])]
        if name == "JsonNull":
            return None
        return getattr(node, "value", node)

    return unwrap(getattr(config, "config", config)) or {}


SPEC_DICE_LIST = OperationSpec(
    id="message.dice.list",
    request=DiceListReq,
    response=DiceCatalog,
    impl=dice_list,
    summary="Which dice emoji exist and what counts as a win",
    aliases=("dice.list",),
    columns=("emojis",),
    example={"emojis": ["🎲", "🎯", "🏀"], "success_values": {"🎯": 6}},
    example_args="message dice list",
    covers=("dice.animation-assets", "dice.list-emojis", "dice.stake-info"),
)


# ---------------------------------------------------------------------------
# report / fact-check / paid
# ---------------------------------------------------------------------------


class ReportReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[
        tuple[str, ...],
        arg(1, metavar="MSG_ID", required=False, variadic=True, help="Message ids."),
    ] = ()
    option: Annotated[
        list[str], opt("--option", metavar="KEY", help="Report-option key; repeatable.")
    ] = []
    comment: Annotated[str | None, opt("--comment", help="Free-text comment.")] = None
    list_options: Annotated[
        bool, opt("--list-options", help="Return the report menu instead of reporting.")
    ] = False
    from_user: Annotated[
        PeerRef | None,
        opt("--from", metavar="USER", kind="user", help="Report a member as spam."),
    ] = None
    not_spam: Annotated[bool, opt("--not-spam", help="Report an anti-spam false positive.")] = False


async def report(ctx: OpContext, req: ReportReq) -> ReportResult:
    """Report messages, a member's spam, or an anti-spam false positive.

    `messages.report` is a menu, not a single call: the first request answers
    with the options the server currently offers, and each `--option` walks
    one level deeper. Reporting blind would submit whatever the first option
    happened to be.
    """
    from telethon.tl.functions import channels as ch
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    client = _client(ctx)
    ids = _ids(req.msg_id)

    if req.from_user is not None:
        channel = await client.get_input_entity(peer)
        member = await _send.resolve(ctx, req.from_user)
        await client(ch.ReportSpamRequest(channel=channel, participant=member, id=ids))
        return ReportResult(ok=True, title="reported as spam")

    if req.not_spam:
        channel = await client.get_input_entity(peer)
        if len(ids) != 1:
            raise UsageError("--not-spam takes exactly one message id", field="msg_id")
        await client(ch.ReportAntiSpamFalsePositiveRequest(channel=channel, msg_id=ids[0]))
        return ReportResult(ok=True, title="reported as a false positive")

    if not ids:
        raise UsageError("give at least one message id to report", field="msg_id")

    option = req.option[-1].encode() if req.option else b""
    result = await client(
        fn.ReportRequest(peer=peer, id=ids, option=option, message=req.comment or "")
    )
    name = type(result).__name__
    if name == "ReportResultReported":
        return ReportResult(ok=True, title="reported")
    if name == "ReportResultAddComment":
        return ReportResult(
            ok=False, title="a comment is required", comment_required=True, options=[]
        )
    options = [
        {"key": _decode_key(getattr(entry, "option", b"")), "text": getattr(entry, "text", "")}
        for entry in (getattr(result, "options", None) or [])
    ]
    return ReportResult(ok=False, title=getattr(result, "title", None), options=options)


def _decode_key(raw: bytes) -> str:
    try:
        return raw.decode()
    except UnicodeDecodeError:
        import base64

        return base64.b64encode(raw).decode()


SPEC_REPORT = OperationSpec(
    id="message.report",
    request=ReportReq,
    response=ReportResult,
    impl=report,
    summary="Report messages for abuse",
    description=(
        "The first call returns the option menu; pass --option to walk it. "
        "Reporting a chat or a user (rather than messages) belongs to the "
        "`chat`/`user` groups."
    ),
    mutating=True,
    columns=("ok", "title"),
    example={"ok": False, "title": "What is wrong with this message?", "options": []},
    example_args="message report @channel 42 --list-options",
    covers=(
        "messages-core.report-antispam-false-positive",
        "messages-core.report-message",
        "messages-core.report-spam-participant",
    ),
)


class FactCheckReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Message id.")]
    set: Annotated[str | None, opt("--set", metavar="TEXT", help="Fact-check text.")] = None
    parse: Annotated[str | None, choice("md", "html", "none", help="Formatting of --set.")] = None
    clear: Annotated[bool, opt("--clear", help="Remove the fact-check.")] = False


async def fact_check_set(ctx: OpContext, req: FactCheckReq) -> FactCheck:
    """Read, set or remove a message's fact-check.

    Reading is free for everyone; writing needs an account Telegram flagged
    as an independent fact-checker for the message's country, so the write
    path usually answers PERMISSION_DENIED — which is information, not a bug.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    client = _client(ctx)

    if req.clear:
        await client(fn.DeleteFactCheckRequest(peer=peer, msg_id=req.msg_id))
        return FactCheck(msg_id=req.msg_id)
    if req.set is not None:
        text, entities = _send.body(req.set, parse=req.parse)
        result = await client(
            fn.EditFactCheckRequest(
                peer=peer,
                msg_id=req.msg_id,
                text=types.TextWithEntities(text=text, entities=_send.tl_entities(entities) or []),
            )
        )
        _send.messages_from_updates(result)
        return FactCheck(msg_id=req.msg_id, text=text, entities=entities)

    found = await client(fn.GetFactCheckRequest(peer=peer, msg_id=[req.msg_id]))
    for check in found or []:
        piece = getattr(check, "text", None)
        return FactCheck(
            msg_id=req.msg_id,
            country=getattr(check, "country", None),
            text=str(getattr(piece, "text", "") or ""),
            entities=message_entities(piece) if piece is not None else [],
            hash=getattr(check, "hash", None),
            need_check=bool(getattr(check, "need_check", False)),
        )
    return FactCheck(msg_id=req.msg_id)


SPEC_FACT_CHECK_SET = OperationSpec(
    id="message.fact-check.set",
    request=FactCheckReq,
    response=FactCheck,
    impl=fact_check_set,
    summary="Read, set or remove a message's fact-check",
    mutating=True,
    tags=frozenset({"mutating-checked"}),
    columns=("msg_id", "country", "text"),
    example={"msg_id": 42, "country": "US", "text": "Context: …"},
    example_args="message fact-check set @channel 42",
    covers=("messages-core.factcheck-edit", "messages-core.factcheck-view"),
)


class PaidReq(Request):
    user: Annotated[
        PeerRef | None, arg(0, metavar="USER", required=False, kind="user", help="User.")
    ] = None
    exempt: Annotated[bool, opt("--exempt", help="Let this user message me for free.")] = False
    charge: Annotated[bool, opt("--charge", help="Re-enable the per-message fee.")] = False
    refund: Annotated[bool, opt("--refund", help="With --exempt: refund the Stars paid.")] = False
    revenue: Annotated[bool, opt("--revenue", help="Report Stars earned from paid messages.")] = (
        False
    )
    channel: Annotated[
        PeerRef | None,
        opt("--channel", metavar="CHAT", kind="peer", help="Scope --revenue to a channel."),
    ] = None


async def paid_set(ctx: OpContext, req: PaidReq) -> PaidMessageSettings:
    """Per-user paid-message settings, and the Stars they earned."""
    from telethon.tl import types
    from telethon.tl.functions import account as acc

    client = _client(ctx)
    if req.revenue:
        peer = (
            await _send.resolve(ctx, req.channel)
            if req.channel is not None
            else types.InputPeerSelf()
        )
        revenue = await client(acc.GetPaidMessagesRevenueRequest(user_id=peer))
        return PaidMessageSettings(revenue_stars=getattr(revenue, "stars_amount", None))

    if req.user is None:
        raise UsageError("give a user, or --revenue", field="user")
    if req.exempt == req.charge:
        raise UsageError("pass exactly one of --exempt or --charge", field="exempt")

    peer = await _send.resolve(ctx, req.user)
    await client(
        acc.ToggleNoPaidMessagesExceptionRequest(
            user_id=peer, refund_charged=req.refund or None, require_payment=req.charge or None
        )
    )
    return PaidMessageSettings(
        user_id=_send.peer_id_of(peer),
        exempt=req.exempt,
        refunded_stars=0 if req.refund else None,
    )


SPEC_PAID_SET = OperationSpec(
    id="message.paid.set",
    request=PaidReq,
    response=PaidMessageSettings,
    impl=paid_set,
    summary="Per-user paid-message settings and the Stars they earned",
    description=(
        "--refund sends Stars back to the sender, so it is gated behind --yes. "
        "The global 'charge non-contacts' switch is a privacy setting and the "
        "per-group price is a chat setting; both belong to their own groups."
    ),
    mutating=True,
    columns=("user_id", "exempt", "revenue_stars"),
    example={"user_id": 777, "exempt": True},
    example_args="message paid set @alice --exempt",
    covers=("messages-core.paid-messages-exempt-user", "messages-core.paid-messages-revenue"),
)


# ---------------------------------------------------------------------------
# sponsored messages
# ---------------------------------------------------------------------------


class SponsoredListReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel or bot chat.")]
    mark_viewed: Annotated[bool, opt("--mark-viewed", help="Also report the impressions.")] = False


async def sponsored_list(ctx: OpContext, req: SponsoredListReq) -> Page[SponsoredMessage]:
    """The sponsored messages a channel would show.

    Opt-in: tlgr never mixes them into `message list`. Registering the
    impression is a separate flag because doing it silently would report
    views nobody saw.
    """
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    client = _client(ctx)
    result = await client(fn.GetSponsoredMessagesRequest(peer=peer))
    items: list[SponsoredMessage] = []
    for entry in getattr(result, "messages", None) or []:
        random_id = getattr(entry, "random_id", b"") or b""
        items.append(
            SponsoredMessage(
                random_id=_decode_key(random_id),
                title=getattr(entry, "title", None),
                message=str(getattr(entry, "message", "") or ""),
                entities=message_entities(entry),
                url=getattr(entry, "url", None),
                button_text=getattr(entry, "button_text", None),
                sponsor_info=getattr(entry, "sponsor_info", None),
                additional_info=getattr(entry, "additional_info", None),
                recommended=bool(getattr(entry, "recommended", False)),
                can_report=bool(getattr(entry, "can_report", False)),
            )
        )
        if req.mark_viewed:
            await client(fn.ViewSponsoredMessageRequest(random_id=random_id))
            items[-1].viewed = True
    return Page(items=items, has_more=False, total=len(items))


SPEC_SPONSORED_LIST = OperationSpec(
    id="message.sponsored.list",
    request=SponsoredListReq,
    response=Page[SponsoredMessage],
    impl=sponsored_list,
    summary="List the sponsored messages a chat would show",
    aliases=("ads.list",),
    tags=frozenset({"mutating-checked"}),
    columns=("random_id", "title", "message"),
    example={"items": [{"random_id": "abc", "message": "An ad"}], "has_more": False},
    example_args="message sponsored list @durov",
    covers=("messages-core.sponsored-list",),
)


class SponsoredHideReq(Request):
    state: Annotated[str, arg(0, metavar="STATE", required=False, help="on = hide ads.")] = "on"
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Disable ads in this channel instead."),
    ] = None


async def sponsored_hide(ctx: OpContext, req: SponsoredHideReq) -> SponsoredHidden:
    """Hide ads for my account (Premium), or disable them in my own channel."""
    from telethon.tl.functions import account as acc
    from telethon.tl.functions import channels as ch

    hide = req.state.strip().lower() in ("on", "true", "yes", "1")
    client = _client(ctx)
    if req.chat is not None:
        peer = await _send.resolve(ctx, req.chat)
        channel = await client.get_input_entity(peer)
        await client(ch.RestrictSponsoredMessagesRequest(channel=channel, restricted=hide))
        return SponsoredHidden(hidden=hide, chat_id=_send.peer_id_of(peer))
    await client(acc.ToggleSponsoredMessagesRequest(enabled=not hide))
    return SponsoredHidden(hidden=hide)


SPEC_SPONSORED_HIDE = OperationSpec(
    id="message.sponsored.hide",
    request=SponsoredHideReq,
    response=SponsoredHidden,
    impl=sponsored_hide,
    summary="Hide sponsored messages for my account or my channel",
    aliases=("ads.hide",),
    mutating=True,
    idempotent=True,
    columns=("hidden", "chat_id"),
    example={"hidden": True},
    example_args="message sponsored hide on",
    covers=("messages-core.sponsored-hide",),
)


class SponsoredReportReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat the ad appeared in.")]
    random_id: Annotated[str, arg(1, metavar="RANDOM_ID", help="Ad id from `sponsored list`.")]
    option: Annotated[
        str | None, opt("--option", metavar="KEY", help="Report-option key from the menu.")
    ] = None
    click: Annotated[bool, opt("--click", help="Report a click instead of a report.")] = False
    media: Annotated[bool, opt("--media", help="With --click: the click was on the media.")] = False


async def sponsored_report(ctx: OpContext, req: SponsoredReportReq) -> ReportResult:
    """Report a sponsored message, or register a click-through."""
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    await _send.resolve(ctx, req.chat)
    random_id = req.random_id.encode()
    if req.click:
        await client(fn.ClickSponsoredMessageRequest(random_id=random_id, media=req.media or None))
        return ReportResult(ok=True, title="click reported")
    result = await client(
        fn.ReportSponsoredMessageRequest(random_id=random_id, option=(req.option or "").encode())
    )
    name = type(result).__name__
    if name in (
        "ChannelsSponsoredMessageReportResultReported",
        "SponsoredMessageReportResultReported",
    ):
        return ReportResult(ok=True, title="reported")
    options = [
        {"key": _decode_key(getattr(entry, "option", b"")), "text": getattr(entry, "text", "")}
        for entry in (getattr(result, "options", None) or [])
    ]
    return ReportResult(ok=False, title=getattr(result, "title", None), options=options)


SPEC_SPONSORED_REPORT = OperationSpec(
    id="message.sponsored.report",
    request=SponsoredReportReq,
    response=ReportResult,
    impl=sponsored_report,
    summary="Report or click through a sponsored message",
    aliases=("ads.report",),
    mutating=True,
    columns=("ok", "title"),
    example={"ok": True, "title": "reported"},
    example_args="message sponsored report @durov abc123",
    covers=("messages-core.sponsored-interact",),
)


# ---------------------------------------------------------------------------
# suggested posts
# ---------------------------------------------------------------------------


class SuggestedApproveReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Suggested post id.")]
    publish_at: Annotated[
        str | None,
        opt("--publish-at", metavar="TS", kind="datetime", help="Publish then, not now."),
    ] = None
    price_stars: Annotated[
        int | None, opt("--price-stars", metavar="N", help="Counter-offer in Stars.")
    ] = None
    price_ton: Annotated[
        int | None, opt("--price-ton", metavar="NANO", help="Counter-offer in TON.")
    ] = None


async def suggested_approve(ctx: OpContext, req: SuggestedApproveReq) -> SuggestedPostState:
    """Approve a suggested post, optionally at another time or price."""
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    await _client(ctx)(
        fn.ToggleSuggestedPostApprovalRequest(
            peer=peer, msg_id=req.msg_id, schedule_date=_send.schedule_at(req.publish_at)
        )
    )
    price = None
    if req.price_stars is not None or req.price_ton is not None:
        price = {"stars": req.price_stars, "ton": req.price_ton}
    return SuggestedPostState(
        chat_id=_send.peer_id_of(peer),
        msg_id=req.msg_id,
        state="approved",
        publish_at=req.publish_at,
        price=price,
    )


SPEC_SUGGESTED_APPROVE = OperationSpec(
    id="message.suggested.approve",
    request=SuggestedApproveReq,
    response=SuggestedPostState,
    impl=suggested_approve,
    summary="Approve a suggested post",
    description="Approving a paid suggestion spends the channel's Stars, so it needs --yes.",
    mutating=True,
    destructive=True,
    columns=("chat_id", "msg_id", "state"),
    example={"chat_id": -1001234, "msg_id": 42, "state": "approved"},
    example_args="message suggested approve @channel 42",
    covers=("messages-core.suggested-post-approve-decline",),
)


class SuggestedDenyReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Suggested post id.")]
    comment: Annotated[str | None, opt("--comment", help="Reason sent back to the author.")] = None


async def suggested_deny(ctx: OpContext, req: SuggestedDenyReq) -> SuggestedPostState:
    """Decline a suggested post."""
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    await _client(ctx)(
        fn.ToggleSuggestedPostApprovalRequest(
            peer=peer, msg_id=req.msg_id, reject=True, reject_comment=req.comment
        )
    )
    return SuggestedPostState(chat_id=_send.peer_id_of(peer), msg_id=req.msg_id, state="declined")


SPEC_SUGGESTED_DENY = OperationSpec(
    id="message.suggested.deny",
    request=SuggestedDenyReq,
    response=SuggestedPostState,
    impl=suggested_deny,
    summary="Decline a suggested post",
    aliases=("message.suggested.decline",),
    mutating=True,
    columns=("chat_id", "msg_id", "state"),
    example={"chat_id": -1001234, "msg_id": 42, "state": "declined"},
    example_args="message suggested deny @channel 42",
    covers_partial=("messages-core.suggested-post-approve-decline",),
    coverage_note="The decline half; `message suggested approve` covers the approve half.",
)


class SuggestedEditReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Channel.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="My suggested post.")]
    text: Annotated[str | None, opt("--text", help="New text.")] = None
    price_stars: Annotated[
        int | None, opt("--price-stars", metavar="N", help="New asking price in Stars.")
    ] = None
    price_ton: Annotated[
        int | None, opt("--price-ton", metavar="NANO", help="New asking price in TON.")
    ] = None
    publish_at: Annotated[
        str | None,
        opt("--publish-at", metavar="TS", kind="datetime", help="New requested publication time."),
    ] = None
    add_offer: Annotated[
        bool, opt("--add-offer", help="Attach a new offer to an already-sent post.")
    ] = False


async def suggested_edit(ctx: OpContext, req: SuggestedEditReq) -> SuggestedPostState:
    """Change my own pending suggested post: text, price or time."""
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    if req.text is not None:
        text, entities = _send.body(req.text)
        await _client(ctx)(
            fn.EditMessageRequest(
                peer=peer, id=req.msg_id, message=text, entities=_send.tl_entities(entities)
            )
        )
    if req.price_stars is not None or req.price_ton is not None or req.publish_at:
        await _client(ctx)(
            fn.ToggleSuggestedPostApprovalRequest(
                peer=peer, msg_id=req.msg_id, schedule_date=_send.schedule_at(req.publish_at)
            )
        )
    return SuggestedPostState(
        chat_id=_send.peer_id_of(peer),
        msg_id=req.msg_id,
        state="offer-updated",
        publish_at=req.publish_at,
        price={"stars": req.price_stars, "ton": req.price_ton},
    )


SPEC_SUGGESTED_EDIT = OperationSpec(
    id="message.suggested.edit",
    request=SuggestedEditReq,
    response=SuggestedPostState,
    impl=suggested_edit,
    summary="Change your own pending suggested post",
    description="Price bounds come from appConfig's stars_suggested_post_amount_min/max.",
    mutating=True,
    columns=("chat_id", "msg_id", "state"),
    example={"chat_id": -1001234, "msg_id": 42, "state": "offer-updated"},
    example_args='message suggested edit @channel 42 --text "new copy"',
    covers=(
        "groups-channels-admin.suggested-post-counter-offer",
        "groups-channels-admin.suggested-post-send",
        "messages-core.suggested-post-edit-own-offer",
    ),
)


# ---------------------------------------------------------------------------
# AI composition tones
# ---------------------------------------------------------------------------


def _tone(entry: Any) -> Tone:
    return Tone(
        slug=str(getattr(entry, "slug", "") or ""),
        title=str(getattr(entry, "title", "") or ""),
        prompt=getattr(entry, "prompt", None),
        emoji_id=getattr(entry, "emoji_id", None) or getattr(entry, "document_id", None),
        installed=bool(getattr(entry, "installed", False)),
        author=getattr(entry, "author", None),
    )


class ToneListReq(Request):
    installed: Annotated[bool, opt("--installed", help="Only tones I installed.")] = False
    mine: Annotated[bool, opt("--mine", help="Only tones I authored.")] = False
    refresh: Annotated[bool, opt("--refresh", help="Ignore the cached hash.")] = False


async def tone_list(ctx: OpContext, req: ToneListReq) -> Page[Tone]:
    """List the AI composition tones `--tone` accepts."""
    from telethon.tl.functions import aicompose

    limit, _ = _window(ctx, "message.tone.list", PageKind.LOCAL, default=100)
    result = await _client(ctx)(aicompose.GetTonesRequest(hash=0))
    items = [_tone(entry) for entry in (getattr(result, "tones", None) or [])]
    if req.installed:
        items = [tone for tone in items if tone.installed]
    return Page(items=items[:limit], has_more=len(items) > limit, total=len(items))


SPEC_TONE_LIST = OperationSpec(
    id="message.tone.list",
    request=ToneListReq,
    response=Page[Tone],
    impl=tone_list,
    summary="List AI composition tones",
    aliases=("ai.tone.list",),
    paginated=PageKind.LOCAL,
    columns=("slug", "title", "installed"),
    example={"items": [{"slug": "formal", "title": "Formal"}], "has_more": False},
    example_args="message tone list",
    covers=("ai.tones-list", "appearance.ai-compose-tones", "messages-core.ai-compose-tones"),
)


class ToneGetReq(Request):
    slug: Annotated[str, arg(0, metavar="SLUG", help="Tone slug.")]
    example: Annotated[
        int, opt("--example", metavar="N", help="Generate N example rewrites.", ge=0)
    ] = 1


async def tone_get(ctx: OpContext, req: ToneGetReq) -> Tone:
    """Show one tone, and preview what it does."""
    from telethon.tl.functions import aicompose

    client = _client(ctx)
    found = await client(aicompose.GetToneRequest(slug=req.slug))
    tone = _tone(getattr(found, "tone", None) or found)
    for _ in range(req.example):
        sample = await client(aicompose.GetToneExampleRequest(slug=req.slug))
        piece = getattr(sample, "text", None) or sample
        tone.examples.append(str(getattr(piece, "text", piece) or ""))
    return tone


SPEC_TONE_GET = OperationSpec(
    id="message.tone.get",
    request=ToneGetReq,
    response=Tone,
    impl=tone_get,
    summary="Show one composition tone and preview it",
    description="Examples are generated server-side and spend the AI quota.",
    aliases=("ai.tone.get",),
    tags=frozenset({"mutating-checked"}),
    columns=("slug", "title", "prompt"),
    example={"slug": "formal", "title": "Formal", "prompt": "Rewrite formally"},
    example_args="message tone get formal",
    covers=("ai.tone-example",),
)


class ToneSetReq(Request):
    slug: Annotated[
        str, arg(0, metavar="SLUG", required=False, help="Existing tone; omit with --new.")
    ] = ""
    new: Annotated[bool, opt("--new", help="Create a new tone.")] = False
    title: Annotated[str | None, opt("--title", help="Tone title.")] = None
    prompt: Annotated[str | None, opt("--prompt", help="Tone prompt.")] = None
    emoji_id: Annotated[
        int | None, opt("--emoji-id", metavar="ID", help="Custom emoji icon (Premium).")
    ] = None
    credit_me: Annotated[bool, opt("--credit-me", help="Show me as the author.")] = False
    install: Annotated[bool, opt("--install", help="Install a shared tone.")] = False
    uninstall: Annotated[bool, opt("--uninstall", help="Uninstall it.")] = False


async def tone_set(ctx: OpContext, req: ToneSetReq) -> Tone:
    """Create, edit, install or uninstall an AI composition tone."""
    from telethon.tl.functions import aicompose

    client = _client(ctx)
    if req.install or req.uninstall:
        if not req.slug:
            raise UsageError("--install/--uninstall need a tone slug", field="slug")
        await client(aicompose.SaveToneRequest(slug=req.slug, unsave=req.uninstall or None))
        return Tone(slug=req.slug, installed=req.install)

    if req.new:
        created = await client(
            aicompose.CreateToneRequest(
                title=req.title or "", prompt=req.prompt or "", emoji_id=req.emoji_id
            )
        )
        return _tone(getattr(created, "tone", None) or created)

    if not req.slug:
        raise UsageError("give a tone slug, or --new", field="slug")
    updated = await client(
        aicompose.UpdateToneRequest(
            slug=req.slug, title=req.title, prompt=req.prompt, emoji_id=req.emoji_id
        )
    )
    return _tone(getattr(updated, "tone", None) or updated)


SPEC_TONE_SET = OperationSpec(
    id="message.tone.set",
    request=ToneSetReq,
    response=Tone,
    impl=tone_set,
    summary="Create, edit or install a composition tone",
    aliases=("ai.tone.set",),
    mutating=True,
    columns=("slug", "title", "installed"),
    example={"slug": "formal", "title": "Formal"},
    example_args='message tone set --new --title Formal --prompt "Rewrite formally"',
    covers=("ai.tone-create", "ai.tone-edit", "ai.tone-install"),
)


class ToneDeleteReq(Request):
    slug: Annotated[str, arg(0, metavar="SLUG", help="Tone I authored.")]


async def tone_delete(ctx: OpContext, req: ToneDeleteReq) -> Tone:
    """Delete a tone I authored. It disappears for everyone who installed it."""
    from telethon.tl.functions import aicompose

    await _client(ctx)(aicompose.DeleteToneRequest(slug=req.slug))
    return Tone(slug=req.slug, installed=False)


SPEC_TONE_DELETE = OperationSpec(
    id="message.tone.delete",
    request=ToneDeleteReq,
    response=Tone,
    impl=tone_delete,
    summary="Delete a composition tone you authored",
    aliases=("ai.tone.delete",),
    mutating=True,
    destructive=True,
    columns=("slug",),
    example={"slug": "formal"},
    example_args="message tone delete formal",
    covers_partial=("ai.tone-edit",),
    coverage_note="The delete half of `ai.tone-edit`; `message tone set` covers the edit half.",
)


# ---------------------------------------------------------------------------
# games
# ---------------------------------------------------------------------------


class GameGetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Game message id.")]
    user: Annotated[
        PeerRef | None, opt("--user", metavar="USER", kind="user", help="Only this player.")
    ] = None
    url: Annotated[bool, opt("--url", help="Print the HTML5 game URL.")] = False


async def game_get(ctx: OpContext, req: GameGetReq) -> GameInfo:
    """A game's high-score table.

    Control-only: a CLI can report the scoreboard but cannot render an HTML5
    game, and `messages.getGameUrl` is a bot method the pinned Telethon does
    not expose, so --url is refused rather than faked.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if req.url:
        _send.require_supported(
            "--url",
            "messages.getGameUrl is a bot-only method and is not in Telethon 1.44; "
            "a user account cannot mint a game URL",
        )
    peer = await _send.resolve(ctx, req.chat)
    client = _client(ctx)
    player = await _send.resolve(ctx, req.user) if req.user is not None else types.InputUserSelf()
    result = await client(fn.GetGameHighScoresRequest(peer=peer, id=req.msg_id, user_id=player))
    scores = [
        GameScore(
            position=getattr(entry, "pos", None),
            user_id=getattr(entry, "user_id", None),
            score=int(getattr(entry, "score", 0) or 0),
        )
        for entry in (getattr(result, "scores", None) or [])
    ]
    info = GameInfo(scores=scores)
    found = await _fetch(ctx, peer, chat_id=_send.peer_id_of(peer), limit=1, ids=[req.msg_id])
    if found and found[0].media is not None:
        info.title = found[0].media.title or ""
    return info


SPEC_GAME_GET = OperationSpec(
    id="message.game.get",
    request=GameGetReq,
    response=GameInfo,
    impl=game_get,
    summary="A game's high-score table",
    columns=("title", "short_name"),
    example={"title": "Corsairs", "short_name": "corsairs", "scores": []},
    example_args="message game get @gamebot 42",
    covers=("game.high-scores",),
    covers_partial=("game.play",),
    coverage_note="A CLI cannot render an HTML5 game; --url is refused with NOT_SUPPORTED.",
)


class GameSendReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    bot: Annotated[
        PeerRef | None, opt("--bot", metavar="USER", kind="user", help="Bot that owns the game.")
    ] = None
    short_name: Annotated[str | None, opt("--short-name", help="Game short name.")] = None
    via_inline: Annotated[
        bool, opt(help="Relay the game through an inline query (the user-account path).")
    ] = True
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Reply to this id.")
    ] = None
    silent: Annotated[bool, opt("--silent", help="No notification.")] = False


async def game_send(ctx: OpContext, req: GameSendReq) -> Message:
    """Send a bot game into a chat.

    `inputMediaGame` with a short name is a bot path; a user account relays
    an inline result instead, which is what `--via-inline` does and why it is
    the default.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if req.bot is None or not req.short_name:
        raise UsageError("--bot and --short-name are both required", field="bot")

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    bot = await _send.resolve(ctx, req.bot)
    client = _client(ctx)

    if req.via_inline:
        results = await client(
            fn.GetInlineBotResultsRequest(bot=bot, peer=peer, query=req.short_name, offset="")
        )
        found = list(getattr(results, "results", None) or [])
        if not found:
            raise NotFoundError(f"the bot returned no inline result for {req.short_name!r}")
        sent = await client(
            fn.SendInlineBotResultRequest(
                peer=peer,
                random_id=_random_id(),
                query_id=getattr(results, "query_id", 0),
                id=str(getattr(found[0], "id", "")),
                silent=req.silent or None,
            )
        )
        return _send.message_from_updates(sent, chat_id=chat_id)

    sent = await client(
        fn.SendMediaRequest(
            peer=peer,
            media=types.InputMediaGame(
                id=types.InputGameShortName(bot_id=bot, short_name=req.short_name)
            ),
            message="",
            random_id=_random_id(),
            silent=req.silent or None,
        )
    )
    return _send.message_from_updates(sent, chat_id=chat_id)


SPEC_GAME_SEND = OperationSpec(
    id="message.game.send",
    request=GameSendReq,
    response=Message,
    impl=game_send,
    summary="Send a bot game into a chat",
    mutating=True,
    rate_class="send",
    columns=("id", "chat_id"),
    example=_EXAMPLE_MESSAGE,
    example_args="message game send @group --bot @gamebot --short-name corsairs",
    covers=("game.send",),
)


__all__ = sorted(name for name in dir() if name.startswith("SPEC_"))
