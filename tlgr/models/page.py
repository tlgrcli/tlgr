"""The one paginated shape: `Page[T]`."""

from __future__ import annotations

from typing import Generic, TypeVar

from tlgr.models.base import Model

__all__ = ["Page", "PageInfo"]

T = TypeVar("T")


class Page(Model, Generic[T]):
    """A slice of a longer list.

    `total` is the server's own count where it gives one and `null` otherwise;
    it is never estimated, because a wrong total is worse than no total.
    """

    items: list[T] = []
    has_more: bool = False
    next_cursor: str | None = None
    total: int | None = None


class PageInfo(Model):
    """The pagination half of a response envelope, without the items."""

    has_more: bool = False
    next_cursor: str | None = None
    total: int | None = None
