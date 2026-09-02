"""The `draft` group: prepare a message without sending it.

Drafts are the human-in-the-loop primitive. An agent leaves a reply in a chat
and the account owner sends or discards it from any Telegram client, because a
draft is server-side and syncs everywhere — which is exactly why it is worth
having a command for at all.

The options are `message send`'s, minus the ones a draft cannot carry: there
is no silence, no schedule and no paid-message fee on something that has not
been sent.
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.pagination import PageKind
from tlgr.core.timefmt import fmt_dt
from tlgr.models.base import Request
from tlgr.models.dialog import Draft
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._serialize import entity_to_peer, message_entities
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = ["SPEC_CLEAR", "SPEC_LIST", "SPEC_SET"]

_EXAMPLE_DRAFT: dict[str, Any] = {
    "chat_id": 777123,
    "text": "will confirm tomorrow",
    "date": "2026-09-03T09:14:07Z",
}


def _client(ctx: OpContext) -> Any:
    client = getattr(ctx, "client", None)
    if client is None:  # pragma: no cover - the daemon always supplies one
        from tlgr.core.errors import UsageError

        raise UsageError("this operation needs a connected account")
    return client


def _draft_model(raw: Any, *, chat_id: int, chat: Any = None) -> Draft:
    """A Telethon `DraftMessage` (or a `Draft` wrapper) as the model."""
    inner = getattr(raw, "draft", None) or raw
    reply = getattr(inner, "reply_to", None)
    return Draft(
        chat_id=chat_id,
        chat=entity_to_peer(chat) if chat is not None else None,
        text=str(getattr(inner, "message", "") or getattr(inner, "text", "") or ""),
        entities=message_entities(inner),
        reply_to_msg_id=getattr(reply, "reply_to_msg_id", None)
        or getattr(inner, "reply_to_msg_id", None),
        top_msg_id=getattr(reply, "top_msg_id", None),
        no_webpage=bool(getattr(inner, "no_webpage", False)),
        effect_id=getattr(inner, "effect", None),
        date=fmt_dt(getattr(inner, "date", None)),
        empty=type(inner).__name__ == "DraftMessageEmpty",
    )


# ---------------------------------------------------------------------------
# draft set
# ---------------------------------------------------------------------------


class SetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat to leave it in.")]
    text: Annotated[
        str, arg(1, metavar="TEXT", required=False, help="Draft body; '-' reads stdin.")
    ] = ""
    parse: Annotated[str | None, choice("md", "html", "none", help="Formatting of TEXT.")] = None
    entities: Annotated[
        str | None, opt("--entities", metavar="JSON", kind="json", help="Explicit entities.")
    ] = None
    stdin: Annotated[bool, opt("--stdin", help="Read the draft from stdin.")] = False
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Draft reply target.")
    ] = None
    quote: Annotated[str | None, opt("--quote", help="Quoted fragment.")] = None
    topic: Annotated[
        int | None, opt("--topic", metavar="ID", kind="msg_id", help="Draft inside a topic.")
    ] = None
    saved_peer: Annotated[
        PeerRef | None,
        opt("--saved-peer", metavar="CHAT", kind="peer", help="Draft inside a saved dialog."),
    ] = None
    no_preview: Annotated[bool, opt("--no-preview", help="Disable the link preview.")] = False
    preview_url: Annotated[
        str | None, opt("--preview-url", metavar="URL", help="Preview this URL.")
    ] = None
    preview_above: Annotated[bool, opt("--preview-above", help="invert_media.")] = False
    effect: Annotated[
        str | None, opt("--effect", metavar="ID", help="Keep a message effect on the draft.")
    ] = None
    rich_markdown: Annotated[
        str | None,
        opt("--rich-markdown", metavar="PATH", kind="path", help="Store a rich body (layer 229)."),
    ] = None


async def set_draft(ctx: OpContext, req: SetReq) -> Draft:
    """Save a chat draft, with its reply target, formatting and preview options.

    The saved draft is read back rather than echoed: `saveDraft` answers with
    a bare `true`, and reporting what we *sent* would hide a server-side
    normalisation (an entity dropped, a reply target rejected).
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if req.rich_markdown:
        _send.require_supported(
            "--rich-markdown",
            "a rich draft body is layer 229 and the pinned Telethon speaks 227",
        )

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    text, entities = _send.body(req.text, parse=req.parse, entities=req.entities, stdin=req.stdin)
    reply_to = await _send.reply_target(
        ctx,
        reply_to=req.reply_to,
        quote=req.quote,
        quote_parse=req.parse,
        topic=req.topic,
    )
    media: Any = None
    if req.preview_url:
        media = types.InputMediaWebPage(url=req.preview_url, optional=True)

    await _client(ctx)(
        fn.SaveDraftRequest(
            peer=peer,
            message=text,
            entities=_send.tl_entities(entities),
            reply_to=reply_to,
            no_webpage=req.no_preview or None,
            invert_media=req.preview_above or None,
            media=media,
            effect=_send.effect_id(req.effect),
        )
    )
    ctx.emit("draft_set", {"chat_id": chat_id, "text": text})
    return Draft(
        chat_id=chat_id,
        text=text,
        entities=entities,
        reply_to_msg_id=req.reply_to,
        top_msg_id=req.topic,
        no_webpage=req.no_preview,
        effect_id=_send.effect_id(req.effect),
    )


SPEC_SET = OperationSpec(
    id="draft.set",
    request=SetReq,
    response=Draft,
    impl=set_draft,
    summary="Leave a draft in a chat without sending it",
    description=(
        "Drafts are server-side and sync to every Telegram client, which is "
        "what makes them the handover point between an agent and a person."
    ),
    legacy_paths=("draft set",),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("chat_id", "text"),
    example=_EXAMPLE_DRAFT,
    example_args='draft set @alice "will confirm tomorrow"',
    covers=("dialogs.draft-set", "effect.draft", "messages-core.draft-set"),
)


# ---------------------------------------------------------------------------
# draft list
# ---------------------------------------------------------------------------


class ListReq(Request):
    chat: Annotated[
        PeerRef | None,
        opt("--chat", metavar="CHAT", kind="peer", help="Only this chat's draft."),
    ] = None


async def list_drafts(ctx: OpContext, req: ListReq) -> Page[Draft]:
    """Every non-empty draft across chats, or one chat's draft."""
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    client = _client(ctx)
    limit = int(getattr(ctx, "limit", None) or 20)

    if req.chat is not None:
        peer = await _send.resolve(ctx, req.chat)
        result = await client(fn.GetPeerDialogsRequest(peers=[types.InputDialogPeer(peer)]))
        chat_id = _send.peer_id_of(peer)
        drafts = [
            _draft_model(dialog.draft, chat_id=chat_id)
            for dialog in (getattr(result, "dialogs", None) or [])
            if getattr(dialog, "draft", None) is not None
        ]
        return Page(items=[d for d in drafts if not d.empty], has_more=False, total=len(drafts))

    items: list[Draft] = []
    for draft in await client.get_drafts():
        entity = getattr(draft, "entity", None)
        chat_id = int(getattr(draft, "entity_id", 0) or 0)
        if not chat_id and entity is not None:
            chat_id = _send.peer_id_of(entity)
        model = _draft_model(draft, chat_id=chat_id, chat=entity)
        if model.text or model.entities:
            items.append(model)
    return Page(items=items[:limit], has_more=len(items) > limit, total=len(items))


SPEC_LIST = OperationSpec(
    id="draft.list",
    request=ListReq,
    response=Page[Draft],
    impl=list_drafts,
    summary="List drafts across chats",
    description=(
        "Chat ids are marked ids (`-100…` for a channel), which is the COR-10 "
        "fix: v1 reported the raw entity id here and the marked one elsewhere."
    ),
    legacy_paths=("draft list",),
    paginated=PageKind.LOCAL,
    columns=("chat_id", "text"),
    headers=("Chat ID", "Draft"),
    example={"items": [_EXAMPLE_DRAFT], "has_more": False},
    example_args="draft list",
    covers=("dialogs.draft-get", "dialogs.draft-list", "messages-core.draft-list"),
)


# ---------------------------------------------------------------------------
# draft clear
# ---------------------------------------------------------------------------


class ClearReq(Request):
    chat: Annotated[
        PeerRef | None, arg(0, metavar="CHAT", required=False, kind="peer", help="Chat.")
    ] = None
    clear_all: Annotated[bool, opt("--all", help="Clear every draft.")] = False


async def clear(ctx: OpContext, req: ClearReq) -> Draft:
    """Clear one draft, or every draft.

    Clearing one is `saveDraft` with an empty body — Telegram has no delete
    verb for a draft — and clearing all is its own RPC.
    """
    from telethon.tl.functions import messages as fn

    from tlgr.core.errors import UsageError

    client = _client(ctx)
    if req.clear_all:
        await client(fn.ClearAllDraftsRequest())
        ctx.emit("draft_clear", {"all": True})
        return Draft(chat_id=0, empty=True, text="", entities=[])
    if req.chat is None:
        raise UsageError("give a chat, or --all to clear every draft", field="chat")

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    await client(fn.SaveDraftRequest(peer=peer, message=""))
    ctx.emit("draft_clear", {"chat_id": chat_id})
    return Draft(chat_id=chat_id, text="", empty=True)


SPEC_CLEAR = OperationSpec(
    id="draft.clear",
    request=ClearReq,
    response=Draft,
    impl=clear,
    summary="Clear one draft or every draft",
    legacy_paths=("draft clear",),
    mutating=True,
    destructive=True,
    idempotent=True,
    columns=("chat_id", "empty"),
    example={"chat_id": 777123, "text": "", "empty": True},
    example_args="draft clear @alice",
    covers=(
        "dialogs.draft-clear",
        "messages-core.draft-clear",
        "messages-core.draft-clear-all",
    ),
)
