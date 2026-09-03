"""The `todo` group: checklists.

The whole group turns on one invariant: **a task's id is never reused and
never renumbered.** `messages.editMessage` rewrites the entire `todoList`, and
completions are keyed by task id — so removing task 2 by renumbering 3 and 4
down to 2 and 3 would silently move every tick onto the wrong row. Deleting
keeps the survivors' ids, and appending always uses `max(existing) + 1`
because reusing an id is `TODO_ITEM_DUPLICATE`.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, PermissionError_, UsageError
from tlgr.core.timefmt import fmt_dt, to_unix
from tlgr.models.base import Request
from tlgr.models.peer import PeerRef
from tlgr.models.todo import Todo, TodoTask
from tlgr.ops import _send
from tlgr.ops._common import already, client, only, random_id
from tlgr.ops._params import arg, opt
from tlgr.ops._serialize import peer_id_of
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

_EXAMPLE_TODO: dict[str, Any] = {
    "chat_id": 777123,
    "msg_id": 12345,
    "title": "Release checklist",
    "tasks": [
        {"id": 1, "title": "tag the commit", "done": True, "completed_by": 777},
        {"id": 2, "title": "publish the wheel"},
    ],
    "done_count": 1,
}


# ---------------------------------------------------------------------------
# Reading a checklist off a message
# ---------------------------------------------------------------------------


def _text_with_entities(value: Any) -> tuple[str, list[Any]]:
    from tlgr.ops._serialize import message_entities

    if value is None:
        return "", []

    class _Holder:
        entities = list(getattr(value, "entities", None) or [])

    return str(getattr(value, "text", value) or ""), message_entities(_Holder())


def todo_model(media: Any, *, chat_id: int = 0, msg_id: int = 0) -> Todo:
    """`MessageMediaToDo` → the one `Todo` shape this group returns."""
    raw = getattr(media, "todo", None)
    completions = {
        int(getattr(item, "id", 0)): item for item in (getattr(media, "completions", None) or [])
    }
    title, entities = _text_with_entities(getattr(raw, "title", None))
    tasks: list[TodoTask] = []
    for item in getattr(raw, "list", None) or []:
        task_id = int(getattr(item, "id", 0))
        text, task_entities = _text_with_entities(getattr(item, "title", None))
        done = completions.get(task_id)
        tasks.append(
            TodoTask(
                id=task_id,
                title=text,
                entities=task_entities,
                done=done is not None,
                completed_by=peer_id_of(getattr(done, "completed_by", None)) if done else None,
                completed_date=fmt_dt(getattr(done, "date", None)) if done else None,
                completed_date_unix=to_unix(getattr(done, "date", None)) if done else None,
            )
        )
    return Todo(
        chat_id=chat_id,
        msg_id=msg_id,
        title=title,
        entities=entities,
        others_can_add=bool(getattr(raw, "others_can_append", False)),
        others_can_complete=bool(getattr(raw, "others_can_complete", False)),
        tasks=tasks,
        done_count=sum(1 for task in tasks if task.done),
    )


async def _fetch(ctx: OpContext, peer: Any, chat_id: int, msg_id: int) -> tuple[Any, Todo]:
    message = await client(ctx).get_messages(peer, ids=msg_id)
    media = getattr(message, "media", None) if message is not None else None
    if getattr(media, "todo", None) is None:
        raise NotFoundError(f"message {msg_id} in {chat_id} is not a checklist")
    return message, todo_model(media, chat_id=chat_id, msg_id=msg_id)


async def _reread(ctx: OpContext, peer: Any, chat_id: int, msg_id: int, updates: Any) -> Todo:
    for update in getattr(updates, "updates", None) or []:
        media = getattr(getattr(update, "message", None), "media", None)
        if getattr(media, "todo", None) is not None:
            return todo_model(media, chat_id=chat_id, msg_id=msg_id)
    return (await _fetch(ctx, peer, chat_id, msg_id))[1]


def _tl_items(entries: list[tuple[int, str]], parse: str | None) -> list[Any]:
    from telethon.tl import types

    out: list[Any] = []
    for task_id, text in entries:
        plain, entities = _send.body(text, parse=parse)
        out.append(
            types.TodoItem(
                id=task_id,
                title=types.TextWithEntities(
                    text=plain, entities=_send.tl_entities(entities) or []
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# todo create
# ---------------------------------------------------------------------------


class CreateReq(_send.SendOptions, kw_only=True):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Where to post it.")]
    title: Annotated[str, arg(1, metavar="TITLE", help="The list's title.")]
    tasks: Annotated[
        list[str], arg(2, metavar="TASK", variadic=True, help="The tasks, in order.")
    ] = []
    others_can_add: Annotated[bool, opt("--others-can-add", help="Anyone may append tasks.")] = (
        False
    )
    others_can_complete: Annotated[
        bool, opt("--others-can-complete", help="Anyone may tick tasks.")
    ] = False
    parse: Annotated[
        str | None, opt("--parse", metavar="MODE", help="md|html|none for title and tasks.")
    ] = None
    reply_to: Annotated[
        int | None, opt("--reply-to", metavar="ID", kind="msg_id", help="Reply to this message.")
    ] = None


async def create(ctx: OpContext, req: CreateReq) -> Todo:
    """Send a checklist.

    Ids are assigned 1..n here, once, and never touched again: every later
    command in this group addresses a task by the id it was born with.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    if not req.tasks:
        raise UsageError("a checklist needs at least one task", field="tasks")

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    title, entities = _send.body(req.title, parse=req.parse)
    todo = types.TodoList(
        title=types.TextWithEntities(text=title, entities=_send.tl_entities(entities) or []),
        list=_tl_items(list(enumerate(req.tasks, start=1)), req.parse),
        others_can_append=req.others_can_add or None,
        others_can_complete=req.others_can_complete or None,
    )
    values = {
        "peer": peer,
        "media": types.InputMediaTodo(todo=todo),
        "message": "",
        "random_id": random_id(),
        "silent": req.silent or None,
        "noforwards": req.protect or None,
        "reply_to": await _send.reply_target(ctx, reply_to=req.reply_to, topic=req.topic),
        "schedule_date": _send.schedule_at(req.schedule),
        "send_as": await _send.resolve(ctx, req.send_as) if req.send_as is not None else None,
        "effect": _send.effect_id(req.effect),
        "allow_paid_stars": req.paid_stars,
    }
    result = await client(ctx)(fn.SendMediaRequest(**only(values, fn.SendMediaRequest)))
    sent = _send.message_from_updates(result, chat_id=chat_id)
    checklist = await _reread(ctx, peer, chat_id, sent.id, result)
    checklist.msg_id = sent.id
    ctx.emit("todo_created", {"chat_id": chat_id, "msg_id": sent.id, "title": checklist.title})
    return checklist


SPEC_CREATE = OperationSpec(
    id="todo.create",
    request=CreateReq,
    response=Todo,
    impl=create,
    summary="Send a checklist / to-do list",
    description=(
        "Creating a checklist needs Telegram Premium. Task ids are assigned "
        "1..n and are the handle every other `todo` command uses."
    ),
    aliases=("message.checklist",),
    mutating=True,
    rate_class="send",
    columns=("chat_id", "msg_id", "title", "done_count"),
    example=_EXAMPLE_TODO,
    example_args="todo create @team 'Release checklist' 'tag the commit' 'publish the wheel'",
    covers=("messages-core.checklist-create", "todo.create"),
)


# ---------------------------------------------------------------------------
# todo get
# ---------------------------------------------------------------------------


class GetReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Checklist message id.")]


async def get(ctx: OpContext, req: GetReq) -> Todo:
    """Read a checklist: the tasks, and who ticked what and when."""
    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    return (await _fetch(ctx, peer, chat_id, req.msg_id))[1]


SPEC_GET = OperationSpec(
    id="todo.get",
    request=GetReq,
    response=Todo,
    impl=get,
    summary="Read a checklist: tasks, who ticked what and when",
    columns=("msg_id", "title", "done_count"),
    example=_EXAMPLE_TODO,
    example_args="todo get @team 12345",
    covers=("todo.view-completions",),
)


# ---------------------------------------------------------------------------
# todo toggle
# ---------------------------------------------------------------------------


class ToggleReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Checklist message id.")]
    done: Annotated[list[str], opt("--done", metavar="ID", help="Task ids to tick.")] = []
    undone: Annotated[list[str], opt("--undone", metavar="ID", help="Task ids to untick.")] = []
    send_as: Annotated[
        PeerRef | None,
        opt("--send-as", metavar="PEER", kind="peer", help="Tick as a channel or anonymously."),
    ] = None


def _task_ids(values: list[str], known: set[int], field: str) -> list[int]:
    from tlgr.ops._common import ids as expand

    out = expand(tuple(values))
    unknown = [task for task in out if task not in known]
    if unknown:
        raise UsageError(f"this checklist has no task {unknown[0]}", field=field)
    return out


async def toggle(ctx: OpContext, req: ToggleReq) -> Todo:
    """Tick and untick tasks in one call.

    `toggleTodoCompleted` takes both vectors at once, so "move task 2 to done
    and task 3 back" is one request and one service message rather than two
    that could interleave with somebody else's edit.
    """
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    _, checklist = await _fetch(ctx, peer, chat_id, req.msg_id)
    known = {task.id for task in checklist.tasks}
    completed = _task_ids(req.done, known, "done")
    incompleted = _task_ids(req.undone, known, "undone")
    if not completed and not incompleted:
        raise UsageError("name at least one task with --done or --undone", field="done")

    state = {task.id: task.done for task in checklist.tasks}
    if all(state.get(task) for task in completed) and not any(
        state.get(task) for task in incompleted
    ):
        already(ctx)
        checklist.already = True
        return checklist

    if req.send_as is not None:
        # Ticking honours the chat's default send-as identity, and there is no
        # per-request field for it; the default is what the server reads.
        await client(ctx)(
            fn.SaveDefaultSendAsRequest(peer=peer, send_as=await _send.resolve(ctx, req.send_as))
        )

    result = await client(ctx)(
        fn.ToggleTodoCompletedRequest(
            peer=peer, msg_id=req.msg_id, completed=completed, incompleted=incompleted
        )
    )
    updated = await _reread(ctx, peer, chat_id, req.msg_id, result)
    ctx.emit(
        "todo_toggled",
        {"chat_id": chat_id, "msg_id": req.msg_id, "done": completed, "undone": incompleted},
    )
    return updated


SPEC_TOGGLE = OperationSpec(
    id="todo.toggle",
    request=ToggleReq,
    response=Todo,
    impl=toggle,
    summary="Tick and untick checklist tasks in one call",
    description=(
        "Needs to be the author unless the list was created with "
        "`--others-can-complete`. A no-op is `already: true`."
    ),
    aliases=("todo.done", "todo.undone"),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("msg_id", "done_count"),
    example=_EXAMPLE_TODO,
    example_args="todo toggle @team 12345 --done 1",
    covers=(
        "messages-core.checklist-toggle-task",
        "todo.send-as",
        "todo.toggle-completed",
    ),
)


# ---------------------------------------------------------------------------
# todo add
# ---------------------------------------------------------------------------


class AddReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Checklist message id.")]
    tasks: Annotated[list[str], arg(2, metavar="TASK", variadic=True, help="Tasks to append.")] = []
    parse: Annotated[str | None, opt("--parse", metavar="MODE", help="md|html|none.")] = None


async def add(ctx: OpContext, req: AddReq) -> Todo:
    """Append tasks to an existing checklist.

    New ids continue from `max(existing) + 1`. Reusing an id the list has
    already seen is `TODO_ITEM_DUPLICATE`, and reusing one it has *forgotten*
    would re-attach an old completion to a new task.
    """
    from telethon.tl.functions import messages as fn

    if not req.tasks:
        raise UsageError("name at least one task to append", field="tasks")

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    _, checklist = await _fetch(ctx, peer, chat_id, req.msg_id)
    if not checklist.others_can_add:
        # Non-authors are refused by the server; saying so here costs nothing
        # and turns an RPC error into an answer.
        ctx.warn("this checklist is not open for appending; only its author may add tasks")

    start = max((task.id for task in checklist.tasks), default=0) + 1
    entries = list(enumerate(req.tasks, start=start))
    result = await client(ctx)(
        fn.AppendTodoListRequest(peer=peer, msg_id=req.msg_id, list=_tl_items(entries, req.parse))
    )
    return await _reread(ctx, peer, chat_id, req.msg_id, result)


SPEC_ADD = OperationSpec(
    id="todo.add",
    request=AddReq,
    response=Todo,
    impl=add,
    summary="Append tasks to an existing checklist",
    description=(
        "Ids continue from the highest one the list already has; they are "
        "never reused, because a completion is keyed by task id."
    ),
    mutating=True,
    rate_class="send",
    columns=("msg_id", "done_count"),
    example=_EXAMPLE_TODO,
    example_args="todo add @team 12345 'sign the release'",
    covers=("messages-core.checklist-append-tasks",),
)


# ---------------------------------------------------------------------------
# todo edit
# ---------------------------------------------------------------------------


class EditReq(Request):
    chat: Annotated[PeerRef, arg(0, metavar="CHAT", kind="peer", help="Chat.")]
    msg_id: Annotated[int, arg(1, metavar="MSG_ID", kind="msg_id", help="Checklist message id.")]
    title: Annotated[str | None, opt("--title", metavar="TEXT", help="New title.")] = None
    add_task: Annotated[
        list[str], opt("--add-task", metavar="TEXT", help="Add a task; repeatable.")
    ] = []
    remove_task: Annotated[
        list[str], opt("--remove-task", metavar="ID", help="Remove a task id; repeatable.")
    ] = []
    rename_task: Annotated[
        list[str], opt("--rename-task", metavar="ID=TEXT", help="Retitle a task; repeatable.")
    ] = []
    others_can_add: Annotated[
        str | None, opt("--others-can-add", metavar="on|off", help="Who may append.")
    ] = None
    others_can_complete: Annotated[
        str | None, opt("--others-can-complete", metavar="on|off", help="Who may tick.")
    ] = None
    parse: Annotated[str | None, opt("--parse", metavar="MODE", help="md|html|none.")] = None


def _switch(value: str | None, current: bool, field: str) -> bool:
    if value is None:
        return current
    if value not in ("on", "off"):
        raise UsageError(f"--{field} is on or off", field=field)
    return value == "on"


async def edit(ctx: OpContext, req: EditReq) -> Todo:
    """Rename a list, add or remove tasks, change who may add or tick.

    `editMessage` replaces the whole `todoList`, so this is a read-modify-write
    that *keeps every surviving task's id*. Renumbering would be the natural
    thing to do and would move every completion onto the wrong row.
    """
    from telethon.tl import types
    from telethon.tl.functions import messages as fn

    peer = await _send.resolve(ctx, req.chat)
    chat_id = _send.peer_id_of(peer)
    message, checklist = await _fetch(ctx, peer, chat_id, req.msg_id)

    known = {task.id for task in checklist.tasks}
    doomed = set(_task_ids(req.remove_task, known, "remove-task"))
    renames: dict[int, str] = {}
    for pair in req.rename_task:
        raw, sep, text = pair.partition("=")
        if not sep:
            raise UsageError("--rename-task wants ID=TEXT", field="rename-task")
        try:
            task_id = int(raw)
        except ValueError as exc:
            raise UsageError(f"{raw!r} is not a task id", field="rename-task") from exc
        if task_id not in known:
            raise UsageError(f"this checklist has no task {task_id}", field="rename-task")
        renames[task_id] = text

    entries = [
        (task.id, renames.get(task.id, task.title))
        for task in checklist.tasks
        if task.id not in doomed
    ]
    start = max(known, default=0) + 1
    entries += list(enumerate(req.add_task, start=start))
    if not entries:
        raise UsageError(
            "a checklist cannot be emptied; delete the message instead", field="remove-task"
        )

    title, entities = _send.body(
        req.title if req.title is not None else checklist.title, parse=req.parse
    )
    todo = types.TodoList(
        title=types.TextWithEntities(text=title, entities=_send.tl_entities(entities) or []),
        list=_tl_items(entries, req.parse),
        others_can_append=_switch(req.others_can_add, checklist.others_can_add, "others-can-add")
        or None,
        others_can_complete=_switch(
            req.others_can_complete, checklist.others_can_complete, "others-can-complete"
        )
        or None,
    )
    if not getattr(message, "out", False):
        raise PermissionError_("only the author of a checklist may edit it")

    result = await client(ctx)(
        fn.EditMessageRequest(peer=peer, id=req.msg_id, media=types.InputMediaTodo(todo=todo))
    )
    return await _reread(ctx, peer, chat_id, req.msg_id, result)


SPEC_EDIT = OperationSpec(
    id="todo.edit",
    request=EditReq,
    response=Todo,
    impl=edit,
    summary="Rename a checklist, remove or add tasks, change who may add or tick",
    description=(
        "Surviving tasks keep their ids: completions are keyed by id, and "
        "renumbering would move every tick onto a different task."
    ),
    mutating=True,
    rate_class="send",
    columns=("msg_id", "title", "done_count"),
    example=_EXAMPLE_TODO,
    example_args="todo edit @team 12345 --title 'Release 2.0'",
    covers=("messages-core.checklist-edit", "todo.edit-list", "todo.permissions"),
)
