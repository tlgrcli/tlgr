"""Checklists.

A task's `id` is assigned by the client and is the only thing that ties a
completion to a task: `messages.editMessage` rewrites the whole `todoList`, so
renumbering the survivors while deleting one would silently move every tick to
the wrong row. Nothing in this group ever renumbers.
"""

from __future__ import annotations

from tlgr.models.base import Model
from tlgr.models.message import MessageEntity

__all__ = ["Todo", "TodoTask"]


class TodoTask(Model):
    id: int
    title: str = ""
    entities: list[MessageEntity] = []
    done: bool = False
    completed_by: int | None = None
    completed_date: str | None = None
    completed_date_unix: int | None = None


class Todo(Model):
    chat_id: int = 0
    msg_id: int = 0
    title: str = ""
    entities: list[MessageEntity] = []
    others_can_add: bool = False
    others_can_complete: bool = False
    tasks: list[TodoTask] = []
    done_count: int = 0
    already: bool = False
