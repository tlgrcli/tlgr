"""The `contact` group: the address book, the blocklist and the phonebook.

Four things about Telegram's contact API shape this module.

* **Adding a contact is two different methods.** `contacts.addContact` takes
  a user you can already address; `contacts.importContacts` takes a raw
  phone number. They fail differently and they answer differently, and an
  empty `imported` is *ambiguous* — the number may have no account, or its
  owner may refuse phone lookups — so both lists come back rather than a
  boolean.
* **Several "edit" calls are replacements.** `contacts.setBlocked` and
  `contacts.editCloseFriends` overwrite the whole list, so everything here
  reads the current state, prints the diff and writes the union — never a
  bare append, which is how a client silently unblocks everyone.
* **A contact's name is only ever *our* view of it.** `contacts.addContact`
  on someone who is already a contact rewrites the local name and touches
  nothing on their profile. v1 leaned on that for tagging and so does this.
* **Phone numbers are privacy-bearing.** They appear only where the server
  chose to send one, and `--redact` blanks them like any other secret.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page, decode_cursor
from tlgr.core.paths import write_private
from tlgr.core.timefmt import fmt_dt, parse_dt, to_unix
from tlgr.models.base import Request
from tlgr.models.contact import (
    BlockedPeer,
    BlockedSet,
    CloseFriends,
    Contact,
    ContactAdded,
    ContactImport,
    ContactNote,
    ContactRemoved,
    ContactRenamed,
    ContactShared,
    ContactSync,
    FoundPeer,
    ImportedPhone,
    PhoneShared,
    SavedPhoneContact,
    SignUp,
    TopPeer,
    TopPeerState,
    UserStatus,
)
from tlgr.models.page import Page
from tlgr.models.peer import Peer, PeerRef
from tlgr.ops import _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._serialize import entity_to_peer, peer_id_of
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: `contacts.importContacts` is one of the most flood-limited methods there
#: is; official clients send a few hundred per call and pause between them.
IMPORT_BATCH = 200

#: The rating categories `contacts.getTopPeers` splits its answer into, in
#: the CLI's spelling. The key is the request flag Telethon expects.
TOP_CATEGORIES: dict[str, str] = {
    "correspondents": "correspondents",
    "bots-pm": "bots_pm",
    "bots-inline": "bots_inline",
    "bots-app": "bots_app",
    "bots-guestchat": "bots_guestchat",
    "calls": "phone_calls",
    "forward-users": "forward_users",
    "forward-chats": "forward_chats",
    "groups": "groups",
    "channels": "channels",
}

#: category name → the `TopPeerCategory*` constructor `resetTopPeerRating`
#: and the reply both use.
_TOP_TYPES: dict[str, str] = {
    "correspondents": "TopPeerCategoryCorrespondents",
    "bots-pm": "TopPeerCategoryBotsPM",
    "bots-inline": "TopPeerCategoryBotsInline",
    "bots-app": "TopPeerCategoryBotsApp",
    "bots-guestchat": "TopPeerCategoryBotsGuestChat",
    "calls": "TopPeerCategoryPhoneCalls",
    "forward-users": "TopPeerCategoryForwardUsers",
    "forward-chats": "TopPeerCategoryForwardChats",
    "groups": "TopPeerCategoryGroups",
    "channels": "TopPeerCategoryChannels",
}

_EXAMPLE_CONTACT: dict[str, Any] = {
    "id": 777123,
    "raw_id": 777123,
    "name": "Alice",
    "username": "alice",
    "phone": "+15550001111",
    "mutual": True,
}

_PHONE_CHARS = re.compile(r"[^0-9+]")


# ---------------------------------------------------------------------------
# Shared helpers — `user.py` and `resolve.py` import from here
# ---------------------------------------------------------------------------


def client_of(ctx: OpContext) -> Any:
    client = getattr(ctx, "client", None)
    if client is None:  # pragma: no cover - the daemon always supplies one
        raise UsageError("this operation needs a connected account")
    return client


def mark_already(ctx: OpContext) -> None:
    mark = getattr(ctx, "mark_already", None)
    if callable(mark):
        mark()


def e164(phone: str) -> str:
    """`(555) 000-1111` → `+15550001111`. Format only; nothing is looked up."""
    cleaned = _PHONE_CHARS.sub("", (phone or "").strip())
    digits = cleaned.lstrip("+")
    return f"+{digits}" if digits else ""


async def input_user(
    ctx: OpContext,
    ref: PeerRef | str,
    *,
    from_chat: PeerRef | None = None,
    from_message: int | None = None,
) -> Any:
    """The `InputUser` for *ref*, including the `min` form Telethon never builds.

    A user seen only inside a channel message carries no usable access hash.
    `--from-chat/--from-message` is what turns that into
    `inputUserFromMessage`, and it is the difference between `chat posters`
    producing ids and producing something you can act on.
    """
    from telethon import utils
    from telethon.tl import types

    if from_chat is not None and from_message is not None:
        container = await _send.resolve(ctx, from_chat)
        raw = str(getattr(ref, "value", ref)).lstrip("@")
        if not raw.lstrip("-").isdigit():
            raise UsageError(
                "--from-chat/--from-message address a user by id; pass the numeric id",
                field="user",
            )
        return types.InputUserFromMessage(
            peer=container, msg_id=int(from_message), user_id=abs(int(raw))
        )

    peer = await _send.resolve(ctx, ref)
    try:
        return utils.get_input_user(peer)
    except (TypeError, ValueError) as exc:
        raise UsageError(
            f"{getattr(ref, 'raw', ref)!r} is a chat, not a user", field="user"
        ) from exc


async def fetch_user(ctx: OpContext, target: Any) -> Any:
    """The full `User` object behind an `InputUser`.

    Used wherever a command has to read the *current* state before writing —
    the existing name for a rename, `stories_hidden` for the idempotent
    hide — because writing a value the server already holds is an RPC and a
    flood-budget entry for nothing.
    """
    from telethon.tl.functions import users as ufn

    found = await client_of(ctx)(ufn.GetUsersRequest(id=[target]))
    for user in list(found or []):
        if type(user).__name__ == "User":
            return user
    raise NotFoundError("that user could not be read back from the server")


def display_name(user: Any) -> str:
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    return f"{first} {last}".strip()


def status_model(user_id: int, status: Any) -> UserStatus:
    """`userStatus*` as a model, keeping `by_me` intact.

    `userStatusRecently` and friends are coarse *because of a privacy
    setting*, and `by_me` says the setting is ours. Dropping it is how a
    client concludes "they hid from you" about someone who did nothing.
    """
    name = type(status).__name__
    kind = {
        "UserStatusOnline": "online",
        "UserStatusOffline": "offline",
        "UserStatusRecently": "recently",
        "UserStatusLastWeek": "last_week",
        "UserStatusLastMonth": "last_month",
    }.get(name, "empty")
    expires = getattr(status, "expires", None)
    was = getattr(status, "was_online", None)
    return UserStatus(
        user_id=user_id,
        kind=kind,  # type: ignore[arg-type]
        expires=fmt_dt(expires),
        expires_unix=to_unix(expires),
        was_online=fmt_dt(was),
        was_online_unix=to_unix(was),
        by_me=bool(getattr(status, "by_me", False)),
    )


def status_word(status: Any) -> str:
    """v1's short lowercase status string (`online`, `offline`, `recently`)."""
    return type(status).__name__.replace("UserStatus", "").lower() if status else ""


def birthday_text(birthday: Any) -> str | None:
    """`birthday` as `YYYY-MM-DD`, or `MM-DD` when the year is withheld."""
    if birthday is None:
        return None
    day = int(getattr(birthday, "day", 0) or 0)
    month = int(getattr(birthday, "month", 0) or 0)
    if not day or not month:
        return None
    year = getattr(birthday, "year", None)
    return f"{year:04d}-{month:02d}-{day:02d}" if year else f"{month:02d}-{day:02d}"


def birthday_age(birthday: Any, *, today: datetime | None = None) -> int | None:
    year = getattr(birthday, "year", None) if birthday is not None else None
    if not year:
        return None
    now = today or datetime.now(timezone.utc)
    month = int(getattr(birthday, "month", 1) or 1)
    day = int(getattr(birthday, "day", 1) or 1)
    age = now.year - int(year) - ((now.month, now.day) < (month, day))
    return age if age >= 0 else None


def contact_model(user: Any, *, mutual: bool | None = None) -> Contact:
    """A Telethon `User` as a contact row."""
    raw_id = int(getattr(user, "id", 0) or 0)
    status = getattr(user, "status", None)
    return Contact(
        id=raw_id,
        raw_id=raw_id,
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        name=display_name(user),
        username=getattr(user, "username", None),
        usernames=[
            u.username
            for u in (getattr(user, "usernames", None) or [])
            if getattr(u, "username", None)
        ],
        # Telegram sends a bare number; tlgr emits E.164 everywhere.
        phone=e164(getattr(user, "phone", "") or "") or None,
        mutual=bool(getattr(user, "mutual_contact", False)) if mutual is None else bool(mutual),
        close_friend=bool(getattr(user, "close_friend", False)),
        premium=bool(getattr(user, "premium", False)),
        bot=bool(getattr(user, "bot", False)),
        deleted=bool(getattr(user, "deleted", False)),
        verified=bool(getattr(user, "verified", False)),
        scam=bool(getattr(user, "scam", False)),
        fake=bool(getattr(user, "fake", False)),
        stories_hidden=bool(getattr(user, "stories_hidden", False)),
        status=status_model(raw_id, status) if status is not None else None,
    )


def peers_by_id(*collections: Any) -> dict[int, Any]:
    """`{marked id: entity}` for the users/chats a reply carried with it."""
    from telethon import utils

    out: dict[int, Any] = {}
    for collection in collections:
        for entity in collection or []:
            with contextlib.suppress(TypeError, ValueError):
                out[int(utils.get_peer_id(entity))] = entity
    return out


def peer_model(peer: Any, known: dict[int, Any]) -> Peer:
    """A bare `Peer` resolved against the entities the same reply carried."""
    marked = peer_id_of(peer)
    entity = known.get(marked or 0)
    if entity is not None:
        return entity_to_peer(entity)
    return Peer(id=marked or 0, raw_id=abs(marked or 0), kind="unknown")


async def load_contacts(ctx: OpContext) -> tuple[list[Any], list[Any], int]:
    """`(contacts, users, saved_count)` from `contacts.getContacts`.

    `hash=0` because Telethon has no store to diff against; the cheap drift
    check is `contact list --ids-only`, which is `getContactIDs`.
    """
    from telethon.tl.functions import contacts as fn

    result = await client_of(ctx)(fn.GetContactsRequest(hash=0))
    if type(result).__name__ == "ContactsNotModified":  # pragma: no cover - hash is always 0
        return [], [], 0
    return (
        list(getattr(result, "contacts", None) or []),
        list(getattr(result, "users", None) or []),
        int(getattr(result, "saved_count", 0) or 0),
    )


def _window(ctx: OpContext, op: str, kind: PageKind, default: int = 50) -> tuple[int, Any]:
    """`(limit, cursor state)` — `--limit`/`--cursor` are transport-level (L5)."""
    limit = int(getattr(ctx, "limit", None) or default)
    if limit < 1:
        raise UsageError("--limit must be at least 1", field="limit")
    token = getattr(ctx, "cursor", None)
    state: dict[str, Any] = {}
    if token:
        state = decode_cursor(token, op=op, kind=kind, account=ctx.account)
    return min(limit, 1000), state


def _slice(items: list[Any], ctx: OpContext, op: str, offset: int, limit: int) -> Page[Any]:
    """One page out of a list we already hold in full."""
    window = items[offset : offset + limit]
    return build_page(
        window,
        op=op,
        kind=PageKind.LOCAL,
        state={"offset": offset + len(window)},
        account=ctx.account,
        has_more=offset + len(window) < len(items),
        total=len(items),
    )


# ---------------------------------------------------------------------------
# Phonebook files
# ---------------------------------------------------------------------------


def _read_file(source: str, field: str) -> str:
    """Read a phonebook the daemon can reach.

    `-` is refused rather than silently read: the implementation runs inside
    the daemon, so "stdin" there is the daemon's stdin, not the caller's.
    """
    if source.strip() == "-":
        raise UsageError(
            "'-' reads the caller's stdin, and this operation runs in the daemon; "
            "write the phonebook to a file and pass its path",
            field=field,
        )
    path = Path(source).expanduser()
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise UsageError(f"{source}: {exc.strerror or exc}", field=field) from exc


def parse_phonebook(text: str) -> list[ImportedPhone]:
    """vCard, CSV or one-number-per-line, into the same list.

    Deliberately forgiving about the input and strict about the output: every
    entry ends up with an E.164 number and a first name, because
    `importContacts` rejects an empty name with `CONTACT_NAME_EMPTY` and a
    whole batch fails for one bad row.
    """
    body = text.strip()
    if not body:
        return []
    if "BEGIN:VCARD" in body.upper():
        return _parse_vcard(body)
    return _parse_csv(body)


def _parse_vcard(text: str) -> list[ImportedPhone]:
    out: list[ImportedPhone] = []
    first = last = ""
    phone = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        upper = line.upper()
        if upper.startswith("BEGIN:VCARD"):
            first = last = phone = ""
        elif upper.startswith("N:"):
            parts = line.split(":", 1)[1].split(";")
            last = parts[0].strip() if parts else ""
            first = parts[1].strip() if len(parts) > 1 else ""
        elif upper.startswith("FN:") and not first:
            words = line.split(":", 1)[1].strip().split(maxsplit=1)
            first = words[0] if words else ""
            last = words[1] if len(words) > 1 else last
        elif upper.startswith("TEL") and ":" in line:
            phone = e164(line.split(":", 1)[1])
        elif upper.startswith("END:VCARD") and phone:
            out.append(ImportedPhone(phone=phone, first_name=first or phone, last_name=last))
    return out


def _parse_csv(text: str) -> list[ImportedPhone]:
    import csv
    import io

    out: list[ImportedPhone] = []
    for row in csv.reader(io.StringIO(text)):
        cells = [cell.strip() for cell in row if cell.strip()]
        if not cells:
            continue
        phone = e164(cells[0])
        if not phone or not phone.lstrip("+").isdigit():
            # A header row, or a comment. Skipping beats importing "phone".
            continue
        out.append(
            ImportedPhone(
                phone=phone,
                first_name=cells[1] if len(cells) > 1 else phone,
                last_name=cells[2] if len(cells) > 2 else "",
            )
        )
    return out


def render_export(contacts: list[Contact], fmt: str) -> str:
    """The contact list as vCard, CSV or JSON — all local, no RPC."""
    if fmt == "json":
        import msgspec

        return msgspec.json.format(msgspec.json.encode(contacts).decode(), indent=2)
    if fmt == "csv":
        lines = ["id,first_name,last_name,username,phone"]
        for row in contacts:
            lines.append(
                ",".join(
                    str(value or "")
                    for value in (
                        row.id,
                        row.first_name,
                        row.last_name,
                        row.username,
                        row.phone,
                    )
                )
            )
        return "\n".join(lines) + "\n"
    cards: list[str] = []
    for row in contacts:
        card = [
            "BEGIN:VCARD",
            "VERSION:3.0",
            f"N:{row.last_name or ''};{row.first_name or ''};;;",
            f"FN:{row.name or row.first_name or ''}",
        ]
        if row.phone:
            card.append(f"TEL;TYPE=CELL:{row.phone}")
        if row.username:
            card.append(f"X-TELEGRAM:{row.username}")
        card.append("END:VCARD")
        cards.append("\n".join(card))
    return "\n".join(cards) + "\n"


# ---------------------------------------------------------------------------
# contact list
# ---------------------------------------------------------------------------


class ListReq(Request):
    sort: Annotated[
        str,
        choice("name", "first-name", "last-name", "last-seen", "added", help="Ordering."),
    ] = "name"
    with_status: Annotated[
        bool, opt("--with-status", help="Merge contacts.getStatuses into every row.")
    ] = False
    with_stories: Annotated[
        bool, opt("--with-stories", help="Add has_unseen_stories per contact.")
    ] = False
    mutual_only: Annotated[bool, opt("--mutual-only", help="Only mutual contacts.")] = False
    close_friends_only: Annotated[bool, opt("--close-friends-only", help="Only close friends.")] = (
        False
    )
    unregistered: Annotated[
        bool, opt("--unregistered", help="Saved numbers with no Telegram account (takeout).")
    ] = False
    ids_only: Annotated[
        bool, opt("--ids-only", help="Cheap drift check: contacts.getContactIDs only.")
    ] = False
    export: Annotated[
        str | None, choice("vcard", "csv", "json", help="Write the list out instead.")
    ] = None
    out: Annotated[
        str | None, opt("--out", metavar="PATH", kind="path", help="Destination file for --export.")
    ] = None


async def _read_stories(ctx: OpContext, users: list[Any]) -> dict[int, bool]:
    """`{user id: has unseen stories}` from the read marks the server holds."""
    from telethon.tl.functions import stories as sfn

    read: dict[int, int] = {}
    with contextlib.suppress(Exception):
        result = await client_of(ctx)(sfn.GetAllReadPeerStoriesRequest())
        for update in getattr(result, "updates", None) or []:
            peer = getattr(update, "peer", None)
            marked = peer_id_of(peer)
            if marked is not None:
                read[abs(marked)] = int(getattr(update, "max_id", 0) or 0)
    out: dict[int, bool] = {}
    for user in users:
        recent = getattr(user, "stories_max_id", None)
        max_id = int(getattr(recent, "max_id", 0) or 0)
        out[int(user.id)] = bool(max_id and max_id > read.get(int(user.id), 0))
    return out


def _sorted(rows: list[Contact], how: str) -> list[Contact]:
    if how == "first-name":
        return sorted(rows, key=lambda c: ((c.first_name or "").casefold(), c.id))
    if how == "last-name":
        return sorted(rows, key=lambda c: ((c.last_name or "").casefold(), c.id))
    if how == "last-seen":
        # Newest first: an unknown last-seen sorts last rather than as 1970.
        return sorted(rows, key=lambda c: -((c.status.was_online_unix or 0) if c.status else 0))
    if how == "added":
        # Telegram sends the contact list in the order it was built up.
        return rows
    return sorted(rows, key=lambda c: ((c.name or "").casefold(), c.id))


async def list_contacts(ctx: OpContext, req: ListReq) -> Page[Contact]:
    """The contact list, sorted, decorated and optionally written to a file.

    Sorting and the vCard/CSV rendering are entirely local: the server sends
    one list and has no opinion about its order, so asking it per sort would
    be a second full download for nothing.
    """
    from telethon.tl.functions import contacts as fn

    limit, state = _window(ctx, "contact.list", PageKind.LOCAL, default=200)
    offset = int(state.get("offset", 0) or 0)

    if req.ids_only:
        ids = list(await client_of(ctx)(fn.GetContactIDsRequest(hash=0)))
        rows = [Contact(id=int(i), raw_id=int(i)) for i in ids]
        return _slice(rows, ctx, "contact.list", offset, limit)

    if req.unregistered:
        saved = await _saved_contacts(ctx)
        _, users, _ = await load_contacts(ctx)
        known = {e164(getattr(u, "phone", "") or "") for u in users}
        rows = [
            Contact(
                id=0,
                first_name=entry.first_name,
                last_name=entry.last_name,
                name=f"{entry.first_name} {entry.last_name}".strip(),
                phone=entry.phone,
            )
            for entry in saved
            if entry.phone not in known
        ]
        return _slice(rows, ctx, "contact.list", offset, limit)

    contacts, users, saved_count = await load_contacts(ctx)
    mutual = {int(c.user_id): bool(getattr(c, "mutual", False)) for c in contacts}
    rows = [contact_model(u, mutual=mutual.get(int(u.id))) for u in users]

    if req.with_status:
        statuses = {
            int(item.user_id): status_model(int(item.user_id), item.status)
            for item in (await client_of(ctx)(fn.GetStatusesRequest()) or [])
        }
        for row in rows:
            row.status = statuses.get(row.id, row.status)
    if req.with_stories:
        unseen = await _read_stories(ctx, users)
        for row in rows:
            row.has_unseen_stories = unseen.get(row.id, False)

    if req.mutual_only:
        rows = [row for row in rows if row.mutual]
    if req.close_friends_only:
        rows = [row for row in rows if row.close_friend]
    rows = _sorted(rows, req.sort)
    if rows:
        rows[0].saved_count = saved_count

    if req.export:
        if not req.out:
            raise UsageError(
                "--export writes a file and this operation runs in the daemon, so it "
                "needs --out PATH; for machine-readable output on stdout use --json",
                field="out",
            )
        text = render_export(rows, req.export)
        # 0600: a phonebook is exactly the kind of file that should not be
        # world-readable because a shell redirect was convenient.
        write_private(Path(req.out).expanduser(), text)
        ctx.warn(f"wrote {len(rows)} contacts as {req.export} to {req.out}")

    return _slice(rows, ctx, "contact.list", offset, limit)


SPEC_LIST = OperationSpec(
    id="contact.list",
    request=ListReq,
    response=Page[Contact],
    impl=list_contacts,
    summary="The contact list, with sorting, status, story state and export formats",
    description=(
        "`contacts.getContacts` sends the whole list in one call, so sorting "
        "and the vCard/CSV rendering happen locally. `--ids-only` is the "
        "cheap drift check (`contacts.getContactIDs`); `--with-status` and "
        "`--with-stories` each cost one extra call for the whole list, never "
        "one per contact. A phone number appears only where privacy allows."
    ),
    aliases=("contacts",),
    legacy_paths=("contact list", "contacts"),
    paginated=PageKind.LOCAL,
    rate_class="read",
    columns=("id", "name", "username", "phone"),
    headers=("Id", "Name", "Username", "Phone"),
    example={"items": [_EXAMPLE_CONTACT], "has_more": False, "total": 1},
    example_args="contact list --with-status",
    covers=(
        "contacts-users.close-friends-list",
        "contacts-users.contacts-export-vcard",
        "contacts-users.contacts-ids",
        "contacts-users.contacts-list",
        "contacts-users.contacts-sort",
        "contacts-users.contacts-story-state",
    ),
)


# ---------------------------------------------------------------------------
# contact add / rename / remove
# ---------------------------------------------------------------------------


class AddReq(Request):
    user: Annotated[
        PeerRef | None,
        arg(0, metavar="USER", required=False, kind="user", help="@username, id or +phone."),
    ] = None
    name: Annotated[
        str | None,
        arg(1, metavar="NAME", required=False, help="v1 spelling of --first-name [--last-name]."),
    ] = None
    first_name: Annotated[
        str | None, opt("--first-name", metavar="TEXT", help="Mandatory for a new contact.")
    ] = None
    last_name: Annotated[str | None, opt("--last-name", metavar="TEXT")] = None
    phone: Annotated[
        str | None, opt("--phone", metavar="NUMBER", help="Attach a phone number.")
    ] = None
    share_phone: Annotated[
        bool, opt("--share-phone", help="Grant them a phone-number privacy exception.")
    ] = False
    note: Annotated[
        str | None, opt("--note", metavar="TEXT", help="Private annotation on the contact.")
    ] = None
    from_message: Annotated[
        str | None,
        opt("--from-message", metavar="CHAT:ID", help="Take the contact card of that message."),
    ] = None


def _split_name(name: str | None) -> tuple[str, str]:
    parts = (name or "").split(maxsplit=1)
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def _note_of(text: str | None) -> Any:
    from telethon.tl import types

    if text is None:
        return None
    return types.TextWithEntities(text=text, entities=[])


async def _card_from_message(ctx: OpContext, ref: str) -> tuple[str, str, str, int]:
    """`(phone, first, last, user_id)` out of a `messageMediaContact`."""
    chat_ref, _, raw_id = ref.rpartition(":")
    if not chat_ref or not raw_id.strip().isdigit():
        raise UsageError("--from-message takes <chat>:<msg-id>", field="from-message")
    peer = await _send.resolve(ctx, chat_ref)
    # `get_messages` picks channels.getMessages or messages.getMessages by
    # peer kind; building the request by hand here would get that wrong for
    # exactly the case (a channel) where a contact card is most often seen.
    found = await client_of(ctx).get_messages(peer, ids=[int(raw_id)])
    for message in found or []:
        media = getattr(message, "media", None) if message is not None else None
        if type(media).__name__ == "MessageMediaContact":
            return (
                e164(getattr(media, "phone_number", "") or ""),
                getattr(media, "first_name", "") or "",
                getattr(media, "last_name", "") or "",
                int(getattr(media, "user_id", 0) or 0),
            )
    raise NotFoundError(f"message {raw_id} in {chat_ref} carries no contact card")


async def _import_phone(
    ctx: OpContext, phone: str, first: str, last: str, note: str | None
) -> ContactAdded:
    """`contacts.importContacts` for one number, ambiguity intact."""
    from telethon.tl import types
    from telethon.tl.functions import contacts as fn

    result = await client_of(ctx)(
        fn.ImportContactsRequest(
            [
                types.InputPhoneContact(
                    client_id=int(time.time() * 1000) & 0x7FFFFFFF,
                    phone=phone,
                    first_name=first or phone,
                    last_name=last,
                    note=_note_of(note),
                )
            ]
        )
    )
    imported = [int(i.user_id) for i in getattr(result, "imported", None) or []]
    popular = [int(getattr(p, "importers", 0) or 0) for p in getattr(result, "popular_invites", [])]
    reason = None
    if not imported:
        reason = (
            "the server imported nothing: the number has no Telegram account, OR its "
            "owner refuses lookups by phone (inputPrivacyKeyAddedByPhone). These two "
            "are not distinguishable from here."
        )
    return ContactAdded(
        added=bool(imported),
        user_id=imported[0] if imported else None,
        first_name=first or phone,
        last_name=last,
        imported=imported,
        retry=[int(i) for i in getattr(result, "retry_contacts", None) or []],
        popular_importers=max(popular) if popular else None,
        note=note,
        reason=reason,
    )


async def add(ctx: OpContext, req: AddReq) -> ContactAdded:
    """Add a contact — by user, by phone, or from a contact card in a message.

    Which method runs is decided by what we can address: a user we can build
    an `InputUser` for goes through `contacts.addContact` (no phone needed);
    a bare number goes through `contacts.importContacts`, whose empty answer
    is ambiguous and is reported as ambiguous rather than as "no such user".
    """
    from telethon.tl.functions import contacts as fn

    first, last = _split_name(req.name)
    first = req.first_name if req.first_name is not None else first
    last = req.last_name if req.last_name is not None else last
    phone = e164(req.phone or "")

    if req.from_message:
        card_phone, card_first, card_last, user_id = await _card_from_message(ctx, req.from_message)
        first = first or card_first
        last = last or card_last
        phone = phone or card_phone
        if not user_id:
            # A card with user_id 0 belongs to somebody with no account we can
            # see; only importContacts can do anything with it.
            if not phone:
                raise NotFoundError("that contact card carries neither a user nor a phone number")
            return await _import_phone(ctx, phone, first, last, req.note)
        target = await input_user(ctx, str(user_id))
    elif req.user is not None and req.user.kind == "phone":
        return await _import_phone(ctx, str(req.user.value), first, last, req.note)
    elif req.user is not None:
        target = await input_user(ctx, req.user)
    elif phone:
        return await _import_phone(ctx, phone, first, last, req.note)
    else:
        raise UsageError("give a user, a +phone, or --from-message", field="user")

    known = await fetch_user(ctx, target)
    first = first or (getattr(known, "first_name", "") or "")
    last = last or (getattr(known, "last_name", "") or "")
    if not first:
        # The server rejects an empty first name outright; v1 sent "." and
        # everything downstream (including the user's own tagging scheme)
        # depends on that still working.
        first = "."
    if req.share_phone and not getattr(ctx, "dry_run", False):
        ctx.warn("--share-phone discloses your own number to them; it cannot be undone")
    await client_of(ctx)(
        fn.AddContactRequest(
            id=target,
            first_name=first,
            last_name=last,
            phone=phone or (getattr(known, "phone", None) or ""),
            add_phone_privacy_exception=req.share_phone or None,
            note=_note_of(req.note),
        )
    )
    user_id = int(getattr(known, "id", 0) or 0)
    ctx.emit("contact_add", {"user_id": user_id})
    return ContactAdded(
        added=True,
        user_id=user_id,
        first_name=first,
        last_name=last,
        imported=[user_id],
        shared_phone=req.share_phone,
        note=req.note,
    )


SPEC_ADD = OperationSpec(
    id="contact.add",
    request=AddReq,
    response=ContactAdded,
    impl=add,
    summary="Add a contact — by user, by phone, or from a contact card in a message",
    description=(
        "An empty `imported` with an empty `retry` is ambiguous: the number "
        "has no account, or its owner hides it from phone lookups. `reason` "
        "says so rather than the reply claiming 'no such user'. `--retry` "
        "entries must be sent again later; they are not failures."
    ),
    legacy_paths=("contact add",),
    mutating=True,
    rate_class="bulk",
    columns=("added", "user_id", "first_name"),
    example={"added": True, "user_id": 777123, "first_name": "Alice", "imported": [777123]},
    example_args="contact add @alice --first-name Alice",
    covers=(
        "contact.receive-card",
        "contacts-users.contact-add-by-phone",
        "contacts-users.contact-add-by-user",
        "contacts-users.contact-card-open",
        "contacts-users.contact-phone-privacy-exception",
        "dialogs.actionbar-add-contact",
    ),
    tags=frozenset({"visible-to-others"}),
)


class RenameReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Who to rename.")]
    first_name: Annotated[str | None, opt("--first-name", metavar="TEXT")] = None
    last_name: Annotated[str | None, opt("--last-name", metavar="TEXT")] = None


async def rename(ctx: OpContext, req: RenameReq) -> ContactRenamed:
    """Change the locally visible name of a contact.

    Re-issuing `addContact` for someone who is already a contact rewrites
    only *our* view of their name; their profile is untouched, and it works
    on non-contacts too (it saves them). Omitted parts keep the current
    profile name, and an empty first name becomes `"."` because the server
    rejects an empty one — v1 did this and the user's tagging depends on it.
    """
    from telethon.tl.functions import contacts as fn

    target = await input_user(ctx, req.user)
    known = await fetch_user(ctx, target)
    first = req.first_name if req.first_name is not None else (known.first_name or "")
    last = req.last_name if req.last_name is not None else (known.last_name or "")
    if not first:
        first = "."
    await client_of(ctx)(
        fn.AddContactRequest(
            id=target,
            first_name=first,
            last_name=last,
            phone=getattr(known, "phone", None) or "",
            add_phone_privacy_exception=None,
        )
    )
    user_id = int(getattr(known, "id", 0) or 0)
    ctx.emit("contact_rename", {"user_id": user_id})
    return ContactRenamed(saved=True, user_id=user_id, first_name=first, last_name=last)


SPEC_RENAME = OperationSpec(
    id="contact.rename",
    request=RenameReq,
    response=ContactRenamed,
    impl=rename,
    summary="Change the locally visible name of a contact",
    description=(
        "Works on non-contacts too — it saves them — which is what makes it "
        "usable for tagging users with a state marker in the last name."
    ),
    legacy_paths=("contact rename",),
    mutating=True,
    idempotent=True,
    columns=("saved", "user_id", "first_name", "last_name"),
    example={"saved": True, "user_id": 777123, "first_name": "Alice", "last_name": "· lead"},
    example_args="contact rename @alice --last-name '· lead'",
    covers=("contacts-users.contact-edit-name",),
)


class RemoveReq(Request):
    user: Annotated[
        list[PeerRef],
        arg(0, metavar="USER", variadic=True, kind="user", help="Contacts to delete."),
    ] = []
    phone: Annotated[
        list[str],
        opt("--phone", metavar="NUMBER", help="Delete a phonebook entry by number."),
    ] = []


async def remove(ctx: OpContext, req: RemoveReq) -> ContactRemoved:
    """Delete contacts, by user or by phone number.

    Deleting by phone reaches entries with no Telegram account at all, which
    is the only way to clear them — and it is irreversible server-side, which
    is why the whole op is destructive and needs `--yes`.
    """
    from telethon.tl.functions import contacts as fn

    if not req.user and not req.phone:
        raise UsageError("give at least one user or --phone", field="user")

    user_ids: list[int] = []
    if req.user:
        targets = [await input_user(ctx, ref) for ref in req.user]
        result = await client_of(ctx)(fn.DeleteContactsRequest(id=targets))
        user_ids = [
            int(u.id) for u in (getattr(result, "users", None) or []) if hasattr(u, "id")
        ] or [int(getattr(t, "user_id", 0) or 0) for t in targets]

    phones = [e164(p) for p in req.phone if e164(p)]
    if phones:
        await client_of(ctx)(fn.DeleteByPhonesRequest(phones=phones))

    ctx.emit("contact_remove", {"user_ids": user_ids, "phones": phones})
    return ContactRemoved(removed=True, user_ids=user_ids, phones=phones)


SPEC_REMOVE = OperationSpec(
    id="contact.remove",
    request=RemoveReq,
    response=ContactRemoved,
    impl=remove,
    summary="Delete contacts, by user or by phone number",
    legacy_paths=("contact remove",),
    mutating=True,
    destructive=True,
    rate_class="bulk",
    columns=("removed", "user_ids", "phones"),
    example={"removed": True, "user_ids": [777123], "phones": []},
    example_args="contact remove @alice",
    covers=("contacts-users.contact-delete", "contacts-users.contact-delete-by-phone"),
)


# ---------------------------------------------------------------------------
# contact note set
# ---------------------------------------------------------------------------


class NoteSetReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Which contact.")]
    text: Annotated[str | None, arg(1, metavar="TEXT", required=False, help="The note.")] = None
    clear: Annotated[bool, opt("--clear", help="Delete the note.")] = False
    parse: Annotated[str | None, choice("md", "html", "none", help="Markup of the note.")] = None


async def note_set(ctx: OpContext, req: NoteSetReq) -> ContactNote:
    """Set or clear the private note attached to a contact.

    The note is ours alone; read it back with `user get --full`. Only a
    contact can carry one, so a non-contact fails with CONTACT_MISSING rather
    than silently doing nothing.
    """
    from telethon.tl import types
    from telethon.tl.functions import contacts as fn

    if req.clear and req.text:
        raise UsageError("--clear and a note text contradict each other", field="clear")
    text, entities = _send.body(req.text or "", parse=req.parse)
    if req.clear:
        text, entities = "", []

    target = await input_user(ctx, req.user)
    await client_of(ctx)(
        fn.UpdateContactNoteRequest(
            id=target,
            note=types.TextWithEntities(text=text, entities=_send.tl_entities(entities) or []),
        )
    )
    user_id = int(getattr(target, "user_id", 0) or 0)
    ctx.emit("contact_note", {"user_id": user_id})
    return ContactNote(user_id=user_id, note=text or None, cleared=not text)


SPEC_NOTE_SET = OperationSpec(
    id="contact.note.set",
    request=NoteSetReq,
    response=ContactNote,
    impl=note_set,
    summary="Set or clear the private note attached to a contact",
    description="An empty `TextWithEntities` is how Telegram spells 'no note'.",
    mutating=True,
    idempotent=True,
    columns=("user_id", "note"),
    example={"user_id": 777123, "note": "met at the conference"},
    example_args='contact note set @alice "met at the conference"',
    covers=(
        "contact.note",
        "contacts-users.contact-note-delete",
        "contacts-users.contact-note-set",
    ),
)


# ---------------------------------------------------------------------------
# contact search
# ---------------------------------------------------------------------------


class SearchReq(Request):
    query: Annotated[str, arg(0, metavar="QUERY", help="What to look for.")] = ""
    mine_only: Annotated[bool, opt("--mine-only", help="Only contacts and known peers.")] = False
    global_only: Annotated[bool, opt("--global-only", help="Only public username matches.")] = False
    broadcasts: Annotated[bool, opt("--broadcasts", help="Restrict to channels.")] = False
    bots: Annotated[bool, opt("--bots", help="Restrict to bots.")] = False
    type: Annotated[str | None, choice("user", "bot", "group", "channel", help="Kind filter.")] = (
        None
    )
    with_sponsored: Annotated[
        bool, opt("--with-sponsored", help="Also request sponsored peers (off by default).")
    ] = False
    recent: Annotated[bool, opt("--recent", help="List the recently searched peers instead.")] = (
        False
    )
    forget: Annotated[
        PeerRef | None,
        opt("--forget", metavar="PEER", kind="peer", help="Drop one entry and reset its rating."),
    ] = None
    clear_recent: Annotated[bool, opt("--clear-recent", help="Forget the whole history.")] = False
    with_tme_urls: Annotated[
        bool, opt("--with-tme-urls", help="With --recent: include help.getRecentMeUrls.")
    ] = False


def _recent_path(ctx: OpContext) -> Path | None:
    paths = getattr(ctx, "paths", None)
    if paths is None or not ctx.account:  # pragma: no cover - the daemon supplies both
        return None
    return Path(paths.account_dir(ctx.account)) / "recent_peers.json"


def _recent_load(ctx: OpContext) -> list[dict[str, Any]]:
    path = _recent_path(ctx)
    if path is None or not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover - a corrupt file is empty
        return []
    return list(raw.get("peers", [])) if isinstance(raw, dict) else []


def _recent_save(ctx: OpContext, rows: list[dict[str, Any]]) -> None:
    path = _recent_path(ctx)
    if path is None:  # pragma: no cover
        return
    with contextlib.suppress(OSError):
        write_private(path, json.dumps({"peers": rows[:50]}))


async def search(ctx: OpContext, req: SearchReq) -> Page[FoundPeer]:
    """Search contacts, known peers and global public usernames.

    `contacts.search` splits its answer in two and the split is the useful
    part: `my_results` is people this account already knows, `results` is
    everyone else's public username. They arrive labelled (`source`) rather
    than merged, and sponsored rows stay off unless asked for — a CLI has no
    reason to render an advert next to a contact.

    The recent-search list is tlgr's own state: TDLib keeps it client-side
    and MTProto has no call for it. `--forget` is the one half that *is*
    server-side, because dropping a peer also resets its top-peer rating so
    the server stops suggesting it.
    """
    from telethon.tl.functions import contacts as fn
    from telethon.tl.functions import help as hfn

    limit, state = _window(ctx, "contact.search", PageKind.LOCAL, default=50)
    offset = int(state.get("offset", 0) or 0)

    # Searching is a read, so the op stays dry-runnable; the two branches
    # that *do* write guard themselves rather than making the whole command
    # print a stub under --dry-run (the `folder list --tags` pattern).
    if req.clear_recent:
        if getattr(ctx, "dry_run", False):
            ctx.warn("--dry-run: the recent-search history would be cleared")
        else:
            _recent_save(ctx, [])
            ctx.emit("contact_search_clear", {})
    if req.forget is not None:
        from telethon.tl import types as tl

        peer = await _send.resolve(ctx, req.forget)
        marked = _send.peer_id_of(peer)
        if getattr(ctx, "dry_run", False):
            ctx.warn(f"--dry-run: {marked} would be forgotten and its rating reset")
        else:
            _recent_save(ctx, [row for row in _recent_load(ctx) if int(row.get("id", 0)) != marked])
            await client_of(ctx)(
                fn.ResetTopPeerRatingRequest(category=tl.TopPeerCategoryCorrespondents(), peer=peer)
            )
            ctx.emit("contact_search_forget", {"peer_id": marked})

    if req.recent:
        recent: list[FoundPeer] = [
            FoundPeer(
                peer=Peer(
                    id=int(row.get("id", 0)),
                    raw_id=abs(int(row.get("id", 0))),
                    kind=row.get("kind", "unknown"),
                    title=row.get("title", ""),
                    username=row.get("username"),
                ),
                source="recent",
            )
            for row in _recent_load(ctx)
        ]
        if req.with_tme_urls:
            result = await client_of(ctx)(hfn.GetRecentMeUrlsRequest(referer=""))
            known = peers_by_id(getattr(result, "users", None), getattr(result, "chats", None))
            for url in getattr(result, "urls", None) or []:
                peer_ref = getattr(url, "peer", None) or getattr(url, "chat", None)
                if peer_ref is None:
                    continue
                recent.append(
                    FoundPeer(
                        peer=peer_model(peer_ref, known),
                        source="tme",
                        url=str(getattr(url, "url", "") or ""),
                    )
                )
        return _slice(recent, ctx, "contact.search", offset, limit)

    if not req.query.strip():
        raise UsageError("a search query is required", field="query")

    found = await client_of(ctx)(
        fn.SearchRequest(
            q=req.query,
            limit=min(limit + offset, 200),
            broadcasts=req.broadcasts or None,
            bots=req.bots or None,
        )
    )
    known = peers_by_id(getattr(found, "users", None), getattr(found, "chats", None))
    rows: list[FoundPeer] = []
    if not req.global_only:
        rows += [
            FoundPeer(peer=peer_model(p, known), source="mine")
            for p in getattr(found, "my_results", None) or []
        ]
    if not req.mine_only:
        seen = {row.peer.id for row in rows}
        rows += [
            FoundPeer(peer=peer_model(p, known), source="global")
            for p in getattr(found, "results", None) or []
            if peer_id_of(p) not in seen
        ]

    if req.with_sponsored:
        sponsored = await client_of(ctx)(fn.GetSponsoredPeersRequest(q=req.query))
        ads = peers_by_id(getattr(sponsored, "users", None), getattr(sponsored, "chats", None))
        for item in getattr(sponsored, "peers", None) or []:
            random_id = getattr(item, "random_id", None)
            rows.append(
                FoundPeer(
                    peer=peer_model(getattr(item, "peer", None), ads),
                    source="sponsored",
                    sponsored=True,
                    random_id=random_id.hex() if isinstance(random_id, bytes) else None,
                )
            )

    if req.type:
        wanted = {"user": {"user"}, "bot": {"bot"}, "group": {"group", "supergroup"}}.get(
            req.type, {"channel"}
        )
        rows = [row for row in rows if row.peer.kind in wanted]

    # Remember what was found so `--recent` has something to show; this is
    # local state, and the only reason it exists is that MTProto has no call
    # for the recently-searched list every GUI client keeps.
    history = _recent_load(ctx)
    for row in rows[:10]:
        entry = {
            "id": row.peer.id,
            "kind": row.peer.kind,
            "title": row.peer.title,
            "username": row.peer.username,
        }
        history = [item for item in history if int(item.get("id", 0)) != row.peer.id]
        history.insert(0, entry)
    _recent_save(ctx, history)

    return _slice(rows, ctx, "contact.search", offset, limit)


SPEC_SEARCH = OperationSpec(
    id="contact.search",
    request=SearchReq,
    response=Page[FoundPeer],
    impl=search,
    summary="Search contacts, known peers and global public usernames",
    description=(
        "`source` labels every row: `mine` is a contact or an already-known "
        "peer, `global` is a public username match, `recent` is tlgr's own "
        "search history and `sponsored` is an advert (off unless "
        "--with-sponsored). Local title matching over the dialog list is "
        "`chat list --search`."
    ),
    aliases=("chat.search",),
    legacy_paths=("contact search",),
    paginated=PageKind.LOCAL,
    rate_class="resolve",
    tags=frozenset({"mutating-checked"}),
    columns=("peer.id", "peer.title", "peer.username", "source"),
    headers=("Id", "Title", "Username", "Source"),
    example={
        "items": [
            {
                "peer": {"id": 777123, "raw_id": 777123, "kind": "user", "title": "Alice"},
                "source": "mine",
            }
        ],
        "has_more": False,
    },
    example_args="contact search alice",
    covers=(
        "contacts-users.contacts-search",
        "contacts-users.search-recent",
        "contacts-users.search-sponsored-peers",
        "dialogs.recent-searches",
        "dialogs.search-peers",
        "dialogs.sponsored-search-peers",
    ),
)


# ---------------------------------------------------------------------------
# contact status list / birthday list / joined list
# ---------------------------------------------------------------------------


class StatusListReq(Request):
    online_only: Annotated[bool, opt("--online-only", help="Only contacts online right now.")] = (
        False
    )
    since: Annotated[
        str | None,
        opt("--since", metavar="TS", kind="datetime", help="Only statuses newer than this."),
    ] = None


async def status_list(ctx: OpContext, req: StatusListReq) -> Page[UserStatus]:
    """Online / last-seen for every contact, in one call.

    This is the cold-start snapshot; live changes arrive as `updateUserStatus`
    on the event bus. `by_me` on a coarse bucket means *our* last-seen privacy
    caused the coarseness — never report that as the peer hiding from us.
    """
    from telethon.tl.functions import contacts as fn

    rows = [
        status_model(int(item.user_id), item.status)
        for item in (await client_of(ctx)(fn.GetStatusesRequest()) or [])
    ]
    if req.online_only:
        rows = [row for row in rows if row.kind == "online"]
    if req.since:
        floor = parse_dt(req.since)
        cutoff = int(floor.timestamp()) if floor else 0
        rows = [
            row for row in rows if max(row.was_online_unix or 0, row.expires_unix or 0) >= cutoff
        ]
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_STATUS_LIST = OperationSpec(
    id="contact.status.list",
    request=StatusListReq,
    response=Page[UserStatus],
    impl=status_list,
    summary="Online / last-seen status of every contact in one call",
    description=(
        "`userStatusRecently`/`LastWeek`/`LastMonth` carry `by_me`: the "
        "coarse bucket is caused by OUR OWN last-seen privacy, not by "
        "theirs. Never report it as the peer hiding from you."
    ),
    aliases=("contact.statuses",),
    paginated=PageKind.LOCAL,
    columns=("user_id", "kind", "was_online"),
    headers=("User", "State", "Last seen"),
    example={"items": [{"user_id": 777123, "kind": "online"}], "has_more": False},
    example_args="contact status list --online-only",
    covers=("contacts-users.contacts-statuses", "dialogs.presence-watch"),
)


class BirthdayListReq(Request):
    window: Annotated[
        int, opt("--window", metavar="DAYS", help="Days around today to include.", ge=0)
    ] = 1


async def birthday_list(ctx: OpContext, req: BirthdayListReq) -> Page[Contact]:
    """Contacts whose birthday is today or within a day.

    Only the ones whose birthday privacy lets us see it. Official clients
    poll this every six to eight hours, which makes it a good `job` and a bad
    thing to call in a loop.
    """
    from telethon.tl.functions import contacts as fn

    result = await client_of(ctx)(fn.GetBirthdaysRequest())
    users = {int(u.id): u for u in getattr(result, "users", None) or []}
    today = datetime.now(timezone.utc)
    rows: list[Contact] = []
    for entry in getattr(result, "contacts", None) or []:
        user = users.get(int(entry.contact_id))
        row = contact_model(user) if user is not None else Contact(id=int(entry.contact_id))
        row.birthday = birthday_text(entry.birthday)
        row.age = birthday_age(entry.birthday, today=today)
        if req.window and not _within(entry.birthday, today, req.window):
            continue
        rows.append(row)
    limit, state = _window(ctx, "contact.birthday.list", PageKind.LOCAL, default=100)
    return _slice(rows, ctx, "contact.birthday.list", int(state.get("offset", 0) or 0), limit)


def _within(birthday: Any, today: datetime, window: int) -> bool:
    """Is this birthday within `window` days of today, wrapping the year?"""
    month = int(getattr(birthday, "month", 0) or 0)
    day = int(getattr(birthday, "day", 0) or 0)
    if not month or not day:
        return False
    for offset in range(-window, window + 1):
        moment = today.fromordinal(today.toordinal() + offset)
        if (moment.month, moment.day) == (month, day):
            return True
    return False


SPEC_BIRTHDAY_LIST = OperationSpec(
    id="contact.birthday.list",
    request=BirthdayListReq,
    response=Page[Contact],
    impl=birthday_list,
    summary="Contacts whose birthday is today or within a day",
    description=(
        "Visible only per each contact's birthday privacy. Dismissing the "
        "chat-list bar is `chat promo list --dismiss BIRTHDAY_CONTACTS_TODAY`."
    ),
    aliases=("contact.birthdays",),
    paginated=PageKind.LOCAL,
    columns=("id", "name", "birthday", "age"),
    headers=("Id", "Name", "Birthday", "Age"),
    example={
        "items": [{"id": 777123, "name": "Alice", "birthday": "1990-04-01", "age": 36}],
        "has_more": False,
    },
    example_args="contact birthday list",
    covers=("contact.birthdays", "contacts-users.contacts-birthdays"),
)


class JoinedListReq(Request):
    since: Annotated[
        str | None,
        opt("--since", metavar="TS", kind="datetime", help="Only sign-ups after this date."),
    ] = None
    notify: Annotated[
        str | None,
        choice("on", "off", help="Turn the 'contact joined' notification on or off."),
    ] = None
    max_chats: Annotated[
        int, opt("--max-chats", metavar="N", help="Cap the dialog scan.", ge=1)
    ] = 200


async def joined_list(ctx: OpContext, req: JoinedListReq) -> Page[SignUp]:
    """Contacts who joined Telegram, and the "X joined" notification switch.

    There is no method that lists sign-ups: Telegram delivers each one as a
    `messageActionContactSignUp` service message in that person's chat, so
    this scans recent dialogs for them. The scan is capped and says so rather
    than pretending an empty answer is authoritative.
    """
    from telethon.tl.functions import account as afn

    notify: bool | None = None
    if req.notify is not None:
        if getattr(ctx, "dry_run", False):
            ctx.warn(f"--dry-run: the contact-joined notification would be turned {req.notify}")
        else:
            await client_of(ctx)(
                afn.SetContactSignUpNotificationRequest(silent=req.notify == "off")
            )
        notify = req.notify == "on"
    else:
        silent = await client_of(ctx)(afn.GetContactSignUpNotificationRequest())
        notify = not bool(silent)

    floor = parse_dt(req.since) if req.since else None
    rows: list[SignUp] = []
    scanned = 0
    client = client_of(ctx)
    async for dialog in client.iter_dialogs(limit=req.max_chats):
        scanned += 1
        entity = getattr(dialog, "entity", None)
        if type(entity).__name__ != "User":
            continue
        async for message in client.iter_messages(entity, limit=20):
            if message is None:
                continue
            action = getattr(message, "action", None)
            if type(action).__name__ != "MessageActionContactSignUp":
                continue
            when = getattr(message, "date", None)
            if floor is not None and when is not None and when < floor:
                continue
            rows.append(
                SignUp(
                    user_id=int(getattr(entity, "id", 0) or 0),
                    name=display_name(entity),
                    username=getattr(entity, "username", None),
                    chat_id=int(getattr(entity, "id", 0) or 0),
                    msg_id=int(getattr(message, "id", 0) or 0),
                    date=fmt_dt(when),
                    date_unix=to_unix(when),
                    notify=notify,
                )
            )
    if scanned >= req.max_chats:
        ctx.warn(
            f"the scan stopped at {req.max_chats} chats; raise --max-chats to look further. "
            "An empty list here is not proof that nobody joined."
        )
    limit, state = _window(ctx, "contact.joined.list", PageKind.LOCAL, default=50)
    return _slice(rows, ctx, "contact.joined.list", int(state.get("offset", 0) or 0), limit)


SPEC_JOINED_LIST = OperationSpec(
    id="contact.joined.list",
    request=JoinedListReq,
    response=Page[SignUp],
    impl=joined_list,
    summary="Contacts who joined Telegram, and the 'X joined' notification switch",
    description=(
        "Telegram has no sign-up list: each one is a "
        "`messageActionContactSignUp` service message, so this scans recent "
        "chats for them and warns when the scan was capped."
    ),
    paginated=PageKind.LOCAL,
    rate_class="bulk",
    timeout_s=300,
    tags=frozenset({"mutating-checked"}),
    columns=("user_id", "name", "date"),
    headers=("User", "Name", "Joined"),
    example={"items": [{"user_id": 777123, "name": "Alice", "notify": True}], "has_more": False},
    example_args="contact joined list",
    covers=("contacts-users.contacts-joined-notification", "dialogs.contact-signup-notify"),
)


# ---------------------------------------------------------------------------
# contact blocked list / set
# ---------------------------------------------------------------------------


class BlockedListReq(Request):
    stories: Annotated[
        bool, opt("--stories", help="The story blocklist instead (my_stories_from).")
    ] = False


async def _blocked_page(ctx: OpContext, *, stories: bool, offset: int, limit: int) -> Any:
    from telethon.tl.functions import contacts as fn

    return await client_of(ctx)(
        fn.GetBlockedRequest(offset=offset, limit=limit, my_stories_from=stories or None)
    )


async def blocked_list(ctx: OpContext, req: BlockedListReq) -> Page[BlockedPeer]:
    """The blocklist, or the separate story blocklist.

    The two lists are independent: someone on the story blocklist can still
    message you, and someone blocked outright is not automatically on it.
    """
    limit, state = _window(ctx, "contact.blocked.list", PageKind.PARTICIPANTS, default=100)
    offset = int(state.get("offset", 0) or 0)
    kind = "stories" if req.stories else "main"

    rows: list[BlockedPeer] = []
    total: int | None = None
    fetch_all = bool(getattr(ctx, "fetch_all", False))
    while True:
        result = await _blocked_page(
            ctx, stories=req.stories, offset=offset + len(rows), limit=limit
        )
        known = peers_by_id(getattr(result, "users", None), getattr(result, "chats", None))
        batch = list(getattr(result, "blocked", None) or [])
        total = getattr(result, "count", None)
        for item in batch:
            date = getattr(item, "date", None)
            rows.append(
                BlockedPeer(
                    peer=peer_model(getattr(item, "peer_id", None), known),
                    date=fmt_dt(date),
                    date_unix=to_unix(date),
                    kind=kind,  # type: ignore[arg-type]
                )
            )
        if not fetch_all or not batch or (total is not None and offset + len(rows) >= total):
            break

    has_more = total is not None and offset + len(rows) < int(total)
    return build_page(
        rows,
        op="contact.blocked.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": offset + len(rows)},
        account=ctx.account,
        has_more=has_more and not fetch_all,
        total=int(total) if total is not None else None,
    )


SPEC_BLOCKED_LIST = OperationSpec(
    id="contact.blocked.list",
    request=BlockedListReq,
    response=Page[BlockedPeer],
    impl=blocked_list,
    summary="The blocklist, or the separate story blocklist",
    aliases=("user.blocked",),
    paginated=PageKind.PARTICIPANTS,
    columns=("peer.id", "peer.title", "date", "kind"),
    headers=("Id", "Peer", "Blocked", "List"),
    example={
        "items": [{"peer": {"id": 777123, "raw_id": 777123, "kind": "user"}, "kind": "main"}],
        "has_more": False,
    },
    example_args="contact blocked list",
    covers=("contacts-users.block-list", "contacts-users.block-stories-list"),
)


class BlockedSetReq(Request):
    user: Annotated[
        list[PeerRef],
        arg(0, metavar="PEER", variadic=True, kind="peer", help="The complete new list."),
    ] = []
    stories: Annotated[bool, opt("--stories", help="Operate on the story blocklist.")] = False
    from_file: Annotated[
        str | None,
        opt("--from-file", metavar="PATH", kind="path", help="Read the peer list from a file."),
    ] = None


async def blocked_set(ctx: OpContext, req: BlockedSetReq) -> BlockedSet:
    """Replace the whole blocklist atomically.

    DESTRUCTIVE in a way the method name hides: `contacts.setBlocked`
    *replaces* the list, so everyone not named is unblocked. The current list
    is read first and the diff is part of the answer, because "I unblocked
    forty people" should not be something you discover later.
    """
    from telethon.tl.functions import contacts as fn

    refs = list(req.user)
    if req.from_file:
        for line in _read_file(req.from_file, "from-file").splitlines():
            text = line.strip()
            if text and not text.startswith("#"):
                from tlgr.models.peer import parse_peer_ref

                refs.append(parse_peer_ref(text))
    if not refs:
        raise UsageError(
            "give the complete new blocklist; setBlocked replaces it, so an empty "
            "list would unblock everyone",
            field="user",
        )

    peers = [await _send.resolve(ctx, ref) for ref in refs]
    wanted = {_send.peer_id_of(peer) for peer in peers}

    current = await _blocked_page(ctx, stories=req.stories, offset=0, limit=1000)
    known = peers_by_id(getattr(current, "users", None), getattr(current, "chats", None))
    before = {
        peer_model(getattr(item, "peer_id", None), known).id
        for item in getattr(current, "blocked", None) or []
    }

    await client_of(ctx)(
        fn.SetBlockedRequest(id=peers, limit=len(peers), my_stories_from=req.stories or None)
    )
    ctx.emit("blocked_set", {"count": len(peers)})
    return BlockedSet(
        count=len(peers),
        blocked=sorted(wanted - before),
        unblocked=sorted(before - wanted),
        kind="stories" if req.stories else "main",
        applied=True,
    )


SPEC_BLOCKED_SET = OperationSpec(
    id="contact.blocked.set",
    request=BlockedSetReq,
    response=BlockedSet,
    impl=blocked_set,
    summary="Replace the whole blocklist atomically",
    description=(
        "`contacts.setBlocked` REPLACES the list: everyone not passed is "
        "unblocked. The reply is the diff against what was there before."
    ),
    mutating=True,
    destructive=True,
    rate_class="bulk",
    columns=("count", "blocked", "unblocked"),
    example={"count": 1, "blocked": [777123], "unblocked": [], "applied": True},
    example_args="contact blocked set @spammer",
    covers=("dialogs.blocked-set-bulk",),
)


# ---------------------------------------------------------------------------
# contact close-friends list / set
# ---------------------------------------------------------------------------


class CloseFriendsListReq(Request):
    pass


async def close_friends_list(ctx: OpContext, req: CloseFriendsListReq) -> Page[Contact]:
    """List close friends.

    There is no getter: a close friend is a contact carrying `close_friend`,
    so the contact list is fetched and filtered.
    """
    _, users, _ = await load_contacts(ctx)
    rows = [contact_model(u) for u in users if getattr(u, "close_friend", False)]
    limit, state = _window(ctx, "contact.close-friends.list", PageKind.LOCAL, default=30)
    return _slice(rows, ctx, "contact.close-friends.list", int(state.get("offset", 0) or 0), limit)


SPEC_CLOSE_FRIENDS_LIST = OperationSpec(
    id="contact.close-friends.list",
    request=CloseFriendsListReq,
    response=Page[Contact],
    impl=close_friends_list,
    summary="List your close friends",
    description="No dedicated getter exists; `user.close_friend` on the contact list is it.",
    aliases=("story.close-friends.list",),
    paginated=PageKind.LOCAL,
    columns=("id", "name", "username"),
    headers=("Id", "Name", "Username"),
    example={"items": [dict(_EXAMPLE_CONTACT, close_friend=True)], "has_more": False},
    example_args="contact close-friends list",
    covers=("stories.close-friends-list",),
)


class CloseFriendsSetReq(Request):
    user: Annotated[
        list[PeerRef],
        arg(0, metavar="USER", variadic=True, kind="user", help="The complete new list."),
    ] = []
    add: Annotated[
        list[PeerRef], opt("--add", metavar="USER", kind="user", help="Read-modify-write add.")
    ] = []
    remove: Annotated[
        list[PeerRef],
        opt("--remove", metavar="USER", kind="user", help="Read-modify-write remove."),
    ] = []


async def close_friends_set(ctx: OpContext, req: CloseFriendsSetReq) -> CloseFriends:
    """Read or edit the close-friends list.

    `contacts.editCloseFriends` replaces the whole list, so `--add`/`--remove`
    read the current contact list first and send the union. Only contacts may
    be close friends; the server refuses anyone else.
    """
    from telethon.tl.functions import contacts as fn

    _, users, _ = await load_contacts(ctx)
    by_id = {int(u.id): u for u in users}
    current = [int(u.id) for u in users if getattr(u, "close_friend", False)]

    if req.user and (req.add or req.remove):
        raise UsageError("give either a complete list or --add/--remove, not both", field="user")

    if req.user:
        wanted = [int(getattr(await input_user(ctx, ref), "user_id", 0) or 0) for ref in req.user]
    else:
        wanted = list(current)
        for ref in req.add:
            uid = int(getattr(await input_user(ctx, ref), "user_id", 0) or 0)
            if uid and uid not in wanted:
                wanted.append(uid)
        for ref in req.remove:
            uid = int(getattr(await input_user(ctx, ref), "user_id", 0) or 0)
            wanted = [i for i in wanted if i != uid]

    strangers = [uid for uid in wanted if uid not in by_id]
    if strangers:
        raise UsageError(
            f"only contacts can be close friends; {strangers} are not in the contact list",
            field="user",
        )
    if sorted(wanted) == sorted(current):
        mark_already(ctx)
        return CloseFriends(
            user_ids=current,
            count=len(current),
            contacts=[contact_model(by_id[i]) for i in current if i in by_id],
        )

    await client_of(ctx)(fn.EditCloseFriendsRequest(id=wanted))
    ctx.emit("close_friends_set", {"count": len(wanted)})
    return CloseFriends(
        user_ids=wanted,
        count=len(wanted),
        contacts=[contact_model(by_id[i]) for i in wanted if i in by_id],
    )


SPEC_CLOSE_FRIENDS_SET = OperationSpec(
    id="contact.close-friends.set",
    request=CloseFriendsSetReq,
    response=CloseFriends,
    impl=close_friends_set,
    summary="Read or edit the close-friends list",
    description=(
        "`contacts.editCloseFriends` replaces the list, so --add/--remove are "
        "a read-modify-write over the current contact list."
    ),
    aliases=("privacy.close-friends.set", "story.close-friends.set"),
    mutating=True,
    idempotent=True,
    rate_class="bulk",
    columns=("count", "user_ids"),
    example={"user_ids": [777123], "count": 1},
    example_args="contact close-friends set @alice",
    covers=("contacts-users.close-friends-set", "stories.close-friends-set"),
)


# ---------------------------------------------------------------------------
# contact top list / set
# ---------------------------------------------------------------------------


class TopListReq(Request):
    category: Annotated[
        list[str],
        opt(
            "--category",
            metavar="NAME",
            help=("Rating category; repeatable. " + ", ".join(TOP_CATEGORIES)),
        ),
    ] = []


async def top_list(ctx: OpContext, req: TopListReq) -> Page[TopPeer]:
    """Frequent contacts / top peers by category.

    `topPeersDisabled` is a real answer, not an empty one: the user turned
    the feature off, and the ratings are gone server-side. Reporting it as
    "no frequent contacts" would suggest there is something to look at.
    """
    from telethon.tl.functions import contacts as fn

    wanted = list(req.category or ["correspondents"])
    unknown = [name for name in wanted if name not in TOP_CATEGORIES]
    if unknown:
        raise UsageError(
            f"unknown --category {unknown}; pick from {', '.join(TOP_CATEGORIES)}",
            field="category",
        )
    limit, state = _window(ctx, "contact.top.list", PageKind.PARTICIPANTS, default=50)
    offset = int(state.get("offset", 0) or 0)

    flags = {TOP_CATEGORIES[name]: True for name in wanted}
    result = await client_of(ctx)(
        fn.GetTopPeersRequest(offset=offset, limit=limit, hash=0, **flags)
    )
    if type(result).__name__ == "TopPeersDisabled":
        reason = (
            "frequent-contact collection is turned off for this account, so there are "
            "no ratings to report; turn it back on with `tlgr contact top set on`"
        )
        ctx.warn(reason)
        mark = getattr(ctx, "mark_indeterminate", None)
        if callable(mark):
            mark(reason)
        return Page(items=[], has_more=False, total=0)
    known = peers_by_id(getattr(result, "users", None), getattr(result, "chats", None))
    names = {value: key for key, value in _TOP_TYPES.items()}
    rows: list[TopPeer] = []
    for group in getattr(result, "categories", None) or []:
        label = names.get(type(getattr(group, "category", None)).__name__, "correspondents")
        for entry in getattr(group, "peers", None) or []:
            rows.append(
                TopPeer(
                    peer=peer_model(getattr(entry, "peer", None), known),
                    category=label,
                    rating=float(getattr(entry, "rating", 0.0) or 0.0),
                )
            )
    return build_page(
        rows,
        op="contact.top.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": offset + len(rows)},
        account=ctx.account,
        limit=limit,
    )


SPEC_TOP_LIST = OperationSpec(
    id="contact.top.list",
    request=TopListReq,
    response=Page[TopPeer],
    impl=top_list,
    summary="Frequent contacts / top peers by category",
    description=(
        "Ratings decay with the server's `rating_e_decay`. A disabled "
        "feature answers exit 13, not an empty list: nothing was measured."
    ),
    paginated=PageKind.PARTICIPANTS,
    columns=("category", "peer.title", "rating"),
    headers=("Category", "Peer", "Rating"),
    example={
        "items": [
            {
                "peer": {"id": 777123, "raw_id": 777123, "kind": "user", "title": "Alice"},
                "category": "correspondents",
                "rating": 12.5,
            }
        ],
        "has_more": False,
    },
    example_args="contact top list --category correspondents",
    covers=("calls.top-callers", "contacts-users.top-peers-get"),
)


class TopSetReq(Request):
    state: Annotated[str | None, arg(0, metavar="STATE", required=False, help="on | off")] = None
    reset: Annotated[
        PeerRef | None,
        opt("--reset", metavar="PEER", kind="peer", help="Zero this peer's rating instead."),
    ] = None
    category: Annotated[str, opt("--category", metavar="NAME", help="Category for --reset.")] = (
        "correspondents"
    )


async def top_set(ctx: OpContext, req: TopSetReq) -> TopPeerState:
    """Enable/disable frequent-contact collection, or reset one peer's rating.

    Turning it off wipes the ratings server-side, so it is destructive even
    though it looks like a switch.
    """
    from telethon.tl import types
    from telethon.tl.functions import contacts as fn

    if req.reset is not None:
        constructor = _TOP_TYPES.get(req.category)
        if constructor is None:
            raise UsageError(
                f"unknown --category {req.category!r}; pick from {', '.join(_TOP_TYPES)}",
                field="category",
            )
        peer = await _send.resolve(ctx, req.reset)
        await client_of(ctx)(
            fn.ResetTopPeerRatingRequest(category=getattr(types, constructor)(), peer=peer)
        )
        marked = _send.peer_id_of(peer)
        ctx.emit("top_peer_reset", {"peer_id": marked})
        return TopPeerState(reset_peer=marked, category=req.category)

    if req.state not in ("on", "off"):
        raise UsageError("say `on` or `off`, or pass --reset <peer>", field="state")
    enabled = req.state == "on"
    await client_of(ctx)(fn.ToggleTopPeersRequest(enabled=enabled))
    ctx.emit("top_peers_toggle", {"enabled": enabled})
    return TopPeerState(enabled=enabled, disabled_by_user=not enabled)


SPEC_TOP_SET = OperationSpec(
    id="contact.top.set",
    request=TopSetReq,
    response=TopPeerState,
    impl=top_set,
    summary="Enable/disable frequent-contact collection, or reset one peer's rating",
    description="Turning it off also wipes the ratings server-side, which is why it needs --yes.",
    mutating=True,
    destructive=True,
    columns=("enabled", "reset_peer", "category"),
    example={"enabled": True},
    example_args="contact top set on",
    covers=(
        "calls.reset-top-caller",
        "contacts-users.top-peers-reset",
        "contacts-users.top-peers-toggle",
        "dialogs.top-peers-toggle",
    ),
)


# ---------------------------------------------------------------------------
# contact share / share-phone
# ---------------------------------------------------------------------------


class ShareReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Whose card to send.")]
    to: Annotated[
        PeerRef | None, opt("--to", metavar="CHAT", kind="peer", help="Destination chat.")
    ] = None


async def share(ctx: OpContext, req: ShareReq) -> ContactShared:
    """Send someone's contact card into a chat.

    The phone may be empty when their privacy hides it, which yields a card
    without a number rather than an error — that is what the GUI sends too.
    """
    from telethon.helpers import generate_random_long
    from telethon.tl import types
    from telethon.tl.functions import messages as mfn

    if req.to is None:
        raise UsageError("--to names the chat to send the card into", field="to")
    target = await input_user(ctx, req.user)
    known = await fetch_user(ctx, target)
    destination = await _send.resolve(ctx, req.to)

    updates = await client_of(ctx)(
        mfn.SendMediaRequest(
            peer=destination,
            media=types.InputMediaContact(
                phone_number=getattr(known, "phone", None) or "",
                first_name=getattr(known, "first_name", None) or "",
                last_name=getattr(known, "last_name", None) or "",
                vcard="",
            ),
            message="",
            random_id=generate_random_long(),
        )
    )
    chat_id = _send.peer_id_of(destination)
    message = _send.message_from_updates(updates, chat_id=chat_id)
    ctx.emit("contact_share", {"chat_id": chat_id, "user_id": int(known.id)})
    return ContactShared(chat_id=chat_id, msg_id=message.id, contact=contact_model(known))


SPEC_SHARE = OperationSpec(
    id="contact.share",
    request=ShareReq,
    response=ContactShared,
    impl=share,
    summary="Send someone's contact card into a chat",
    mutating=True,
    rate_class="send",
    columns=("chat_id", "msg_id"),
    example={"chat_id": 777123, "msg_id": 4211},
    example_args="contact share @alice --to @bobby",
    covers=("contacts-users.user-share-contact-card",),
    tags=frozenset({"visible-to-others"}),
)


class SharePhoneReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Who to share it with.")]


async def share_phone(ctx: OpContext, req: SharePhoneReq) -> PhoneShared:
    """Share my phone number with someone who added me as a contact.

    Only valid while `peerSettings.share_contact` is set — check
    `chat action-bar` first — and irreversible: a number cannot be un-shared.
    """
    from telethon.tl.functions import contacts as fn

    target = await input_user(ctx, req.user)
    await client_of(ctx)(fn.AcceptContactRequest(id=target))
    user_id = int(getattr(target, "user_id", 0) or 0)
    ctx.emit("contact_share_phone", {"user_id": user_id})
    return PhoneShared(user_id=user_id, shared=True)


SPEC_SHARE_PHONE = OperationSpec(
    id="contact.share-phone",
    request=SharePhoneReq,
    response=PhoneShared,
    impl=share_phone,
    summary="Share my phone number with someone who added me as a contact",
    description="Irreversible: your number cannot be un-shared once they have it.",
    mutating=True,
    destructive=True,
    columns=("user_id", "shared"),
    example={"user_id": 777123, "shared": True},
    example_args="contact share-phone @alice",
    covers=("contacts-users.contact-accept-share-phone", "dialogs.actionbar-share-phone"),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# contact saved list / import / sync
# ---------------------------------------------------------------------------


async def _saved_contacts(ctx: OpContext) -> list[SavedPhoneContact]:
    """`contacts.getSaved`, inside a takeout session when the server insists.

    TAKEOUT_REQUIRED is not a failure: it is Telegram saying "this is a data
    export, open one". `TAKEOUT_INIT_DELAY_X` is a wait, and it surfaces as
    a rate limit rather than an error.
    """
    from telethon.tl.functions import InvokeWithTakeoutRequest
    from telethon.tl.functions import account as afn
    from telethon.tl.functions import contacts as fn

    client = client_of(ctx)
    query = fn.GetSavedRequest()
    try:
        rows = await client(query)
    except Exception as exc:
        if "TAKEOUT" not in type(exc).__name__.upper() and "TAKEOUT" not in str(exc).upper():
            raise
        session = await client(afn.InitTakeoutSessionRequest(contacts=True))
        rows = await client(
            InvokeWithTakeoutRequest(takeout_id=int(getattr(session, "id", 0) or 0), query=query)
        )
    out: list[SavedPhoneContact] = []
    for entry in list(rows or []):
        date = getattr(entry, "date", None)
        out.append(
            SavedPhoneContact(
                phone=e164(getattr(entry, "phone", "") or ""),
                first_name=getattr(entry, "first_name", "") or "",
                last_name=getattr(entry, "last_name", "") or "",
                date=fmt_dt(date),
                date_unix=to_unix(date),
            )
        )
    return out


class SavedListReq(Request):
    invite_text: Annotated[
        bool, opt("--invite-text", help="Also print the localized invite copy.")
    ] = False


async def saved_list(ctx: OpContext, req: SavedListReq) -> Page[SavedPhoneContact]:
    """Every phone number this account ever uploaded, Telegram account or not.

    A CLI cannot send an SMS, so `--invite-text` prints the copy Telegram
    would have used and leaves the sending to a human.
    """
    from telethon.tl.functions import help as hfn

    rows = await _saved_contacts(ctx)
    _, users, _ = await load_contacts(ctx)
    known = {e164(getattr(u, "phone", "") or "") for u in users if getattr(u, "phone", None)}
    for row in rows:
        row.has_account = row.phone in known if known else None
    if req.invite_text and rows:
        invite = await client_of(ctx)(hfn.GetInviteTextRequest())
        rows[0].invite_text = str(getattr(invite, "message", "") or "")

    limit, state = _window(ctx, "contact.saved.list", PageKind.LOCAL, default=100)
    return _slice(rows, ctx, "contact.saved.list", int(state.get("offset", 0) or 0), limit)


SPEC_SAVED_LIST = OperationSpec(
    id="contact.saved.list",
    request=SavedListReq,
    response=Page[SavedPhoneContact],
    impl=saved_list,
    summary="Every phone number this account ever uploaded, including non-Telegram ones",
    description=(
        "Needs a takeout session, which this opens automatically. "
        "`has_account` is computed against the contact list, so it is null "
        "when the contact list could not be read."
    ),
    paginated=PageKind.LOCAL,
    rate_class="bulk",
    timeout_s=300,
    columns=("phone", "first_name", "last_name", "has_account"),
    headers=("Phone", "First", "Last", "On Telegram"),
    example={
        "items": [{"phone": "+15550001111", "first_name": "Alice", "has_account": True}],
        "has_more": False,
    },
    example_args="contact saved list",
    covers=("contacts-users.contacts-saved-phonebook", "contacts-users.user-invite-friends"),
)


class ImportReq(Request):
    file: Annotated[str, arg(0, metavar="FILE", kind="path", help="file.vcf | file.csv")]
    batch_size: Annotated[
        int, opt("--batch-size", metavar="N", help="Contacts per call.", ge=1, le=500)
    ] = IMPORT_BATCH


async def contact_import(ctx: OpContext, req: ImportReq) -> ContactImport:
    """Bulk-import a phonebook from vCard or CSV.

    `retry_contacts` is not an error list: the server is asking for those
    numbers again later, and a caller that drops them loses contacts
    silently. They come back in `retry` so a second pass can send them.
    """
    from telethon.tl import types
    from telethon.tl.functions import contacts as fn

    entries = parse_phonebook(_read_file(req.file, "file"))
    if not entries:
        raise UsageError(f"{req.file} has no usable phone numbers in it", field="file")

    imported: list[ImportedPhone] = []
    retry: list[ImportedPhone] = []
    popular: list[ImportedPhone] = []
    batches = 0
    client = client_of(ctx)
    for start in range(0, len(entries), req.batch_size):
        chunk = entries[start : start + req.batch_size]
        batches += 1
        result = await client(
            fn.ImportContactsRequest(
                [
                    types.InputPhoneContact(
                        client_id=start + index,
                        phone=entry.phone,
                        first_name=entry.first_name or entry.phone,
                        last_name=entry.last_name,
                    )
                    for index, entry in enumerate(chunk)
                ]
            )
        )
        by_client = {start + index: entry for index, entry in enumerate(chunk)}
        for item in getattr(result, "imported", None) or []:
            entry = by_client.get(int(item.client_id))
            if entry is not None:
                imported.append(
                    ImportedPhone(
                        phone=entry.phone,
                        first_name=entry.first_name,
                        last_name=entry.last_name,
                        user_id=int(item.user_id),
                    )
                )
        for item in getattr(result, "popular_invites", None) or []:
            entry = by_client.get(int(item.client_id))
            if entry is not None:
                popular.append(
                    ImportedPhone(
                        phone=entry.phone,
                        first_name=entry.first_name,
                        last_name=entry.last_name,
                        importers=int(getattr(item, "importers", 0) or 0),
                    )
                )
        for client_id in getattr(result, "retry_contacts", None) or []:
            entry = by_client.get(int(client_id))
            if entry is not None:
                retry.append(
                    ImportedPhone(
                        phone=entry.phone,
                        first_name=entry.first_name,
                        last_name=entry.last_name,
                        retry=True,
                    )
                )

    if retry:
        ctx.warn(
            f"{len(retry)} numbers came back in retry_contacts; the server wants them "
            "sent again later. Re-run with a file containing just those."
        )
    ctx.emit("contact_import", {"imported": len(imported), "retry": len(retry)})
    return ContactImport(
        parsed=len(entries),
        imported=imported,
        retry=retry,
        popular_invites=popular,
        batches=batches,
        flood_waits=int(getattr(ctx, "flood_wait_slept", 0) or 0),
    )


SPEC_IMPORT = OperationSpec(
    id="contact.import",
    request=ImportReq,
    response=ContactImport,
    impl=contact_import,
    summary="Bulk-import a phonebook from vCard or CSV",
    description=(
        "Heavily flood-limited, so imports are chunked (`--batch-size`, 200 "
        "by default) and paced by the session limiter. `popular_invites` "
        "says how many other people already imported that number."
    ),
    mutating=True,
    rate_class="bulk",
    timeout_s=600,
    columns=("parsed", "batches", "imported", "retry"),
    example={"parsed": 2, "batches": 1, "imported": [{"phone": "+15550001111"}], "retry": []},
    example_args="contact import phonebook.vcf",
    covers=("contacts-users.contacts-import-bulk",),
)


class SyncReq(Request):
    file: Annotated[str, arg(0, metavar="FILE", kind="path", help="The phonebook to sync from.")]
    delete_missing: Annotated[
        bool, opt("--delete-missing", help="Delete server contacts absent from the file.")
    ] = False
    apply: Annotated[
        bool, opt("--apply", help="Actually apply the diff (the default is to print it).")
    ] = False


async def sync(ctx: OpContext, req: SyncReq) -> ContactSync:
    """Two-way sync of a local phonebook file with the server contact list.

    A headless CLI has no OS address book, so the "device phonebook" is the
    file you point at. Printing the diff is the default because deleting by
    phone is irreversible server-side; `--apply` is what actually writes.
    """
    from telethon.tl import types
    from telethon.tl.functions import contacts as fn

    entries = parse_phonebook(_read_file(req.file, "file"))
    _, users, _ = await load_contacts(ctx)
    server = {e164(getattr(u, "phone", "") or ""): u for u in users if getattr(u, "phone", None)}
    local = {entry.phone: entry for entry in entries}

    to_import = [entry for phone, entry in local.items() if phone not in server]
    to_delete = [phone for phone in server if phone not in local] if req.delete_missing else []

    if not req.apply:
        return ContactSync(to_import=to_import, to_delete=to_delete, applied=False)

    imported = 0
    if to_import:
        result = await client_of(ctx)(
            fn.ImportContactsRequest(
                [
                    types.InputPhoneContact(
                        client_id=index,
                        phone=entry.phone,
                        first_name=entry.first_name or entry.phone,
                        last_name=entry.last_name,
                    )
                    for index, entry in enumerate(to_import)
                ]
            )
        )
        imported = len(getattr(result, "imported", None) or [])
    if to_delete:
        await client_of(ctx)(fn.DeleteByPhonesRequest(phones=to_delete))
    ctx.emit("contact_sync", {"imported": imported, "deleted": len(to_delete)})
    return ContactSync(
        to_import=to_import,
        to_delete=to_delete,
        applied=True,
        imported=imported,
        deleted=len(to_delete),
    )


SPEC_SYNC = OperationSpec(
    id="contact.sync",
    request=SyncReq,
    response=ContactSync,
    impl=sync,
    summary="Two-way sync of a local phonebook file with the server contact list",
    description=(
        "Prints the diff and changes nothing unless `--apply` is given, "
        "because `contacts.deleteByPhones` is irreversible server-side."
    ),
    mutating=True,
    destructive=True,
    rate_class="bulk",
    timeout_s=600,
    columns=("applied", "imported", "deleted"),
    example={"to_import": [{"phone": "+15550001111"}], "to_delete": [], "applied": False},
    example_args="contact sync phonebook.vcf",
    covers=("contacts-users.contacts-sync", "privacy.sync-contacts-delete"),
)
