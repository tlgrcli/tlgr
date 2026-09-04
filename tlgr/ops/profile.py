"""The `profile` group: Settings ▸ Edit Profile, and everything on it.

Seven sub-nouns, one subject — *how I appear to other people*: the name and
bio, the usernames (including the Fragment collectibles), the avatar history,
the accent colours, the emoji status, the presence switch and the public link.

Three things here are corrections rather than features.

* **`profile get` fetches `users.getFullUser`.** v1 called `get_me()` and
  hard-coded `bio` to `""`, so every agent that read a bio read a lie. The
  full user is where the bio, the birthday, the personal channel, the gift
  counters and the Star rating live, and one command answers with all of it.
* **`profile photo set` uploads the file itself.** v1 called
  `client.upload_profile_photo()`, which does not exist in Telethon 1.44, so
  the command could never have worked at all. The real path is `upload_file`
  followed by raw `photos.uploadProfilePhoto`.
* **`profile update` is one command over three RPCs.** The GUI shows one Edit
  Profile screen; `account.updateProfile`, `account.updateBirthday` and
  `account.updatePersonalChannel` are the server's decomposition, not a
  vocabulary an agent should have to learn.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from tlgr.core.errors import NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.core.timefmt import fmt_dt, parse_dt, to_unix
from tlgr.models.base import Request
from tlgr.models.contact import MusicTrack, ProfilePhoto
from tlgr.models.media import WallpaperInstalled
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.models.profile import (
    AdminedChannel,
    ColorPalette,
    ColorSet,
    EmojiStatusItem,
    EmojiStatusSet,
    PhotosDeleted,
    PresenceSet,
    ProfileFull,
    ProfileLink,
    ProfilePhotoSet,
    ProfileUpdated,
    ProfileUsername,
    UsernameSet,
)
from tlgr.ops import _settings
from tlgr.ops._common import client, window
from tlgr.ops._params import arg, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: Palette ids 0-6 carry no colours of their own: every official client draws
#: red, orange, violet, green, cyan, blue and pink for them. Saying so is the
#: difference between "no colours" and "the built-in seven".
_BUILTIN_PALETTES = ("red", "orange", "violet", "green", "cyan", "blue", "pink")


# ---------------------------------------------------------------------------
# profile get
# ---------------------------------------------------------------------------


def _usernames(user: Any) -> list[ProfileUsername]:
    """`user.usernames`, or the single `username` when the vector is absent.

    The main handle is the first *active* entry, which is the rule links
    resolve by; `editable` marks the basic (non-collectible) name, and the
    two are different facts that a flattened string loses.
    """
    rows: list[ProfileUsername] = []
    for entry in getattr(user, "usernames", None) or []:
        rows.append(
            ProfileUsername(
                username=str(getattr(entry, "username", "") or ""),
                active=bool(getattr(entry, "active", False)),
                editable=bool(getattr(entry, "editable", False)),
            )
        )
    if not rows and getattr(user, "username", None):
        rows.append(ProfileUsername(username=str(user.username), active=True, editable=True))
    for row in rows:
        if row.active:
            row.main = True
            break
    return rows


def _birthday_text(raw: Any) -> str | None:
    """`birthday` as `DD-MM` or `DD-MM-YYYY`, the same spelling `--birthday` takes."""
    if raw is None:
        return None
    day = int(getattr(raw, "day", 0) or 0)
    month = int(getattr(raw, "month", 0) or 0)
    year = getattr(raw, "year", None)
    return f"{day:02d}-{month:02d}" + (f"-{int(year)}" if year else "")


def _birthday_tl(text: str | None) -> Any:
    """The inverse. `none` clears it, which the API spells "send no birthday"."""
    from telethon.tl import types

    if text is None or text.strip().lower() in ("none", "clear", ""):
        return None
    parts = [part for part in text.replace("/", "-").replace(".", "-").split("-") if part]
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        raise UsageError("--birthday takes DD-MM, DD-MM-YYYY or 'none'", field="birthday")
    day, month = int(parts[0]), int(parts[1])
    if not (1 <= day <= 31 and 1 <= month <= 12):
        raise UsageError(f"{text!r} is not a date", field="birthday")
    return types.Birthday(day=day, month=month, year=int(parts[2]) if len(parts) == 3 else None)


def _disallowed_gifts(raw: Any) -> list[str]:
    """`disallowedGiftsSettings` as the keyword list `privacy global set` takes."""
    names = {
        "disallow_unlimited_stargifts": "unlimited",
        "disallow_limited_stargifts": "limited",
        "disallow_unique_stargifts": "unique",
        "disallow_premium_gifts": "premium",
        "disallow_stargifts_from_channels": "from-channels",
    }
    if raw is None:
        return []
    return sorted(word for field, word in names.items() if getattr(raw, field, False))


def _emoji_status(raw: Any, into: ProfileFull) -> None:
    name = type(raw).__name__
    if name == "EmojiStatus":
        into.emoji_status = int(getattr(raw, "document_id", 0) or 0)
    elif name == "EmojiStatusCollectible":
        into.emoji_status = int(getattr(raw, "document_id", 0) or 0)
        into.emoji_status_collectible_id = int(getattr(raw, "collectible_id", 0) or 0)
    else:
        return
    into.emoji_status_until = fmt_dt(getattr(raw, "until", None))


def _peer_color(raw: Any) -> tuple[int | None, int | None, int | None]:
    """`(palette id, collectible id, background emoji id)` from a `PeerColor`."""
    if raw is None:
        return None, None, None
    if type(raw).__name__ == "PeerColorCollectible":
        return (
            None,
            int(getattr(raw, "collectible_id", 0) or 0),
            getattr(raw, "background_emoji_id", None),
        )
    return getattr(raw, "color", None), None, getattr(raw, "background_emoji_id", None)


class GetReq(Request):
    full: Annotated[
        bool,
        opt("--full/--no-full", help="Also fetch users.getFullUser (bio, birthday, counters)."),
    ] = True
    refresh: Annotated[
        bool, opt("--refresh", help="Force the userFull fetch even with --no-full.")
    ] = False


async def get(ctx: OpContext, req: GetReq) -> ProfileFull:
    """My own profile, including the fields only `userFull` carries.

    `--no-full` exists for the hot path — a script that only needs the id and
    the name should not pay for a second round trip — but the default is
    `--full`, because v1's default was a `bio` that was always `""`.
    """
    from telethon.tl import types
    from telethon.tl.functions import users as fn

    handle = client(ctx)
    me = await handle.get_me()
    profile = ProfileFull(
        id=int(getattr(me, "id", 0) or 0),
        first_name=str(getattr(me, "first_name", "") or ""),
        last_name=str(getattr(me, "last_name", "") or ""),
        username=getattr(me, "username", None),
        usernames=_usernames(me),
        phone=getattr(me, "phone", None),
        premium=bool(getattr(me, "premium", False)),
        bot=bool(getattr(me, "bot", False)),
        photo_id=getattr(getattr(me, "photo", None), "photo_id", None),
    )
    _emoji_status(getattr(me, "emoji_status", None), profile)
    profile.color, profile.color_collectible_id, profile.background_emoji_id = _peer_color(
        getattr(me, "color", None)
    )
    profile.profile_color, _, profile.profile_background_emoji_id = _peer_color(
        getattr(me, "profile_color", None)
    )
    if not req.full and not req.refresh:
        return profile

    # tlgr holds no `userFull` cache of its own — the daemon caches peers,
    # not profiles — so the fetch below *is* the refresh. `--refresh` exists
    # so a caller that assumed a cache gets the fresh answer it wanted rather
    # than a flag that silently means nothing.
    answer = await handle(fn.GetFullUserRequest(id=types.InputUserSelf()))
    full = getattr(answer, "full_user", None)
    if full is None:  # pragma: no cover - the server always sends one
        return profile
    profile.bio = str(getattr(full, "about", "") or "")
    profile.birthday = _birthday_text(getattr(full, "birthday", None))
    profile.personal_channel_id = getattr(full, "personal_channel_id", None)
    if profile.personal_channel_id is not None:
        found = _settings.entity_map(answer).get(int(profile.personal_channel_id))
        profile.personal_channel = _settings.peer_model(found)
    profile.ttl_period = getattr(full, "ttl_period", None)
    profile.sponsored_enabled = getattr(full, "sponsored_enabled", None)
    profile.stargifts_count = getattr(full, "stargifts_count", None)
    rating = getattr(full, "stars_rating", None)
    profile.stars_rating = getattr(rating, "level", None) if rating is not None else None
    profile.disallowed_gifts = _disallowed_gifts(getattr(full, "disallowed_gifts", None))
    tab = getattr(full, "main_tab", None)
    if tab is not None:
        profile.main_tab = type(tab).__name__.removeprefix("ProfileTab").lower() or None
    fallback = getattr(full, "fallback_photo", None)
    profile.fallback_photo_id = getattr(fallback, "id", None)
    profile.common_chats_count = getattr(full, "common_chats_count", None)
    return profile


SPEC_GET = OperationSpec(
    id="profile.get",
    request=GetReq,
    response=ProfileFull,
    impl=get,
    summary="Show my own profile (bio, birthday, business, gifts and colours included)",
    description=(
        'v1 answered from `get_me()` alone and reported `bio: ""` for every '
        "account, whether or not one was set. This fetches `users.getFullUser` "
        "as well, so an absent bio and an empty bio are different answers."
    ),
    legacy_paths=("profile get",),
    idempotent=True,
    columns=("id", "first_name", "last_name", "username", "phone", "premium"),
    headers=("ID", "First", "Last", "Username", "Phone", "Premium"),
    example={
        "id": 4242,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "username": "ada",
        "phone": "+989123456789",
        "premium": True,
        "bio": "counting on it",
        "birthday": "10-12",
    },
    example_args="profile get",
    covers=("profile.main-tab", "profile.set-bio", "profile.view-own"),
    covers_partial=("profile.usernames-list",),
    coverage_note="The per-username detail (active, collectible) is `profile username list`.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# profile update
# ---------------------------------------------------------------------------


class UpdateReq(Request):
    first_name: Annotated[str | None, opt("--first-name", metavar="TEXT", help="First name.")] = (
        None
    )
    last_name: Annotated[
        str | None, opt("--last-name", metavar="TEXT", help="Last name ('' clears it).")
    ] = None
    bio: Annotated[str | None, opt("--bio", metavar="TEXT", help="About text.")] = None
    birthday: Annotated[
        str | None, opt("--birthday", metavar="DATE", help="DD-MM, DD-MM-YYYY, or 'none'.")
    ] = None
    channel: Annotated[
        str | None,
        opt("--channel", metavar="CHAT", help="Personal channel to show; 'none' unlinks."),
    ] = None
    photo: Annotated[
        str | None, opt("--photo", metavar="PATH", kind="path", help="Shortcut for photo set.")
    ] = None


async def update(ctx: OpContext, req: UpdateReq) -> ProfileUpdated:
    """Edit my profile: names, bio, birthday, personal channel, photo.

    One command, up to four RPCs, and only the ones the caller's flags need.
    An empty string is a real value — `--last-name ""` clears the surname,
    which is why the fields are `str | None` and not truthiness tests.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    result = ProfileUpdated()

    if req.first_name is not None or req.last_name is not None or req.bio is not None:
        await handle(
            fn.UpdateProfileRequest(
                first_name=req.first_name, last_name=req.last_name, about=req.bio
            )
        )
        for name, value in (
            ("first_name", req.first_name),
            ("last_name", req.last_name),
            ("bio", req.bio),
        ):
            if value is not None:
                setattr(result, name, value)
                result.changed.append(name)

    if req.birthday is not None:
        await handle(fn.UpdateBirthdayRequest(birthday=_birthday_tl(req.birthday)))
        result.birthday = _birthday_text(_birthday_tl(req.birthday))
        result.changed.append("birthday")

    if req.channel is not None:
        if req.channel.strip().lower() in ("none", "clear", ""):
            channel: Any = types.InputChannelEmpty()
            result.personal_channel_id = None
        else:
            from tlgr.ops._common import input_channel

            peer = await _settings.resolve(ctx, req.channel)
            channel = input_channel(peer)
            result.personal_channel_id = _settings.peer_of(peer)
        await handle(fn.UpdatePersonalChannelRequest(channel=channel))
        result.changed.append("personal_channel")

    if req.photo is not None:
        photo = await photo_set(ctx, PhotoSetReq(file=req.photo))
        result.photo_id = photo.photo_id
        result.changed.append("photo")

    if not result.changed:
        raise UsageError(
            "nothing to change: give --first-name, --last-name, --bio, "
            "--birthday, --channel or --photo",
            field="first_name",
        )
    ctx.emit("profile_updated", {"changed": result.changed})
    return result


SPEC_UPDATE = OperationSpec(
    id="profile.update",
    request=UpdateReq,
    response=ProfileUpdated,
    impl=update,
    summary="Edit my profile: names, bio, birthday, personal channel, photo",
    description=(
        "`changed` names exactly the fields that were written, because the "
        "command spans four RPCs and a report that lists what you did not ask "
        "for is one nobody can act on."
    ),
    aliases=("profile.set",),
    legacy_paths=("profile update",),
    mutating=True,
    columns=("first_name", "last_name", "bio", "changed"),
    headers=("First", "Last", "Bio", "Changed"),
    example={"first_name": "Ada", "bio": "counting on it", "changed": ["first_name", "bio"]},
    example_args='profile update --bio "counting on it"',
    covers=("profile.birthday-set", "profile.set-name"),
    covers_partial=("profile.personal-channel", "profile.photo-upload", "profile.set-bio"),
    coverage_note=(
        "Reading these back is `profile get`; the avatar's own flags live on "
        "`profile photo set`, and the eligible channels on `profile channel list`."
    ),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# profile username list / set
# ---------------------------------------------------------------------------


class UsernameListReq(Request):
    pass


async def username_list(ctx: OpContext, req: UsernameListReq) -> Page[ProfileUsername]:
    """Every username on this account, collectibles included."""
    rows = _usernames(await client(ctx).get_me())
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_USERNAME_LIST = OperationSpec(
    id="profile.username.list",
    request=UsernameListReq,
    response=Page[ProfileUsername],
    impl=username_list,
    summary="List my usernames, including Fragment collectibles",
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("username", "active", "editable", "main"),
    headers=("Username", "Active", "Editable", "Main"),
    example={
        "items": [
            {"username": "ada", "active": True, "editable": True, "main": True},
            {"username": "lovelace", "active": False},
        ]
    },
    example_args="profile username list",
    covers=("profile.usernames-list",),
    tags=frozenset({"agent-safe"}),
)


class UsernameSetReq(Request):
    name: Annotated[
        str | None, arg(0, metavar="NAME", required=False, help="The username to act on.")
    ] = None
    check: Annotated[bool, opt("--check", help="Only test availability.")] = False
    clear: Annotated[bool, opt("--clear", help="Remove the main username.")] = False
    on: Annotated[bool, opt("--on", help="Activate a collectible username.")] = False
    off: Annotated[bool, opt("--off", help="Deactivate a collectible username.")] = False
    order: Annotated[
        str | None,
        opt("--order", metavar="LIST", help="Comma-separated: every active username, in order."),
    ] = None


async def username_set(ctx: OpContext, req: UsernameSetReq) -> UsernameSet:
    """Set, check, clear, activate/deactivate or reorder usernames.

    `USERNAME_PURCHASE_AVAILABLE` is the interesting failure: the name is not
    taken, it simply only exists for sale on Fragment. Reporting that as
    `purchasable: true` rather than as an opaque error is the difference
    between "pick another name" and "you can have this one, for money".
    """
    from telethon.tl.functions import account as fn

    handle = client(ctx)

    if req.order is not None:
        order = [part.strip().lstrip("@") for part in req.order.split(",") if part.strip()]
        if not order:
            raise UsageError("--order wants every active username, in order", field="order")
        await handle(fn.ReorderUsernamesRequest(order=order))
        return UsernameSet(usernames=_usernames(await handle.get_me()))

    if req.on or req.off:
        if not req.name:
            raise UsageError("--on/--off need the username to toggle", field="name")
        await handle(fn.ToggleUsernameRequest(username=req.name.lstrip("@"), active=bool(req.on)))
        return UsernameSet(
            username=req.name.lstrip("@"),
            active=bool(req.on),
            usernames=_usernames(await handle.get_me()),
        )

    if req.clear:
        await handle(fn.UpdateUsernameRequest(username=""))
        return UsernameSet(username=None, active=False, usernames=_usernames(await handle.get_me()))

    if not req.name:
        raise UsageError("give a username, or --clear / --order", field="name")
    wanted = req.name.lstrip("@")

    if req.check:
        try:
            free = bool(await handle(fn.CheckUsernameRequest(username=wanted)))
        except Exception as exc:
            if "USERNAME_PURCHASE_AVAILABLE" in f"{type(exc).__name__} {exc}".upper():
                return UsernameSet(username=wanted, available=False, purchasable=True)
            raise
        return UsernameSet(username=wanted, available=free)

    await handle(fn.UpdateUsernameRequest(username=wanted))
    ctx.emit("profile_username", {"username": wanted})
    return UsernameSet(username=wanted, active=True, usernames=_usernames(await handle.get_me()))


SPEC_USERNAME_SET = OperationSpec(
    id="profile.username.set",
    request=UsernameSetReq,
    response=UsernameSet,
    impl=username_set,
    summary="Set, check, clear, activate/deactivate or reorder my usernames",
    description=(
        "`--check` writes nothing. A name the server answers "
        "`USERNAME_PURCHASE_AVAILABLE` for is reported as `purchasable`, not "
        "as taken: it exists only on Fragment."
    ),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("username", "active", "available", "purchasable"),
    headers=("Username", "Active", "Available", "On Fragment"),
    example={"username": "ada", "active": True},
    example_args="profile username set ada",
    covers=("profile.set-username", "profile.username-reorder", "profile.username-toggle"),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# profile photo list / set / delete
# ---------------------------------------------------------------------------


class PhotoListReq(Request):
    download: Annotated[
        str | None,
        opt("--download", metavar="DIR", kind="path", help="Download the listed photos here."),
    ] = None


async def photo_list(ctx: OpContext, req: PhotoListReq) -> Page[ProfilePhoto]:
    """My profile-photo history, newest first.

    Also the source of the ids `profile photo set --photo-id` and `profile
    photo delete` take: a photo id alone is useless without the access hash
    and file reference this listing put in the session's cache, which is why
    reusing an id from a previous run fails with `FILE_REFERENCE_EXPIRED`.
    """
    from telethon.tl import types
    from telethon.tl.functions import photos as fn

    limit, state = window(ctx, "profile.photo.list", PageKind.PARTICIPANTS, default=20)
    offset = int(state.get("offset", 0) or 0)
    handle = client(ctx)
    result = await handle(
        fn.GetUserPhotosRequest(user_id=types.InputUserSelf(), offset=offset, max_id=0, limit=limit)
    )
    photos = list(getattr(result, "photos", None) or [])
    me = await handle.get_me()
    current = getattr(getattr(me, "photo", None), "photo_id", None)

    rows = [
        ProfilePhoto(
            id=int(getattr(photo, "id", 0) or 0),
            date=fmt_dt(getattr(photo, "date", None)),
            date_unix=to_unix(getattr(photo, "date", None)),
            sizes=[
                str(getattr(size, "type", ""))
                for size in getattr(photo, "sizes", None) or []
                if getattr(size, "type", None)
            ],
            video=bool(getattr(photo, "video_sizes", None)),
            dc_id=getattr(photo, "dc_id", None),
            current=current is not None and int(getattr(photo, "id", 0) or 0) == int(current),
        )
        for photo in photos
    ]

    if req.download:
        directory = Path(os.path.expanduser(req.download))
        directory.mkdir(parents=True, exist_ok=True)
        for photo, row in zip(photos, rows, strict=True):
            try:
                saved = await handle.download_media(photo, file=str(directory / f"{row.id}.jpg"))
            except Exception as exc:
                ctx.warn(f"could not download photo {row.id}: {exc}")
                continue
            row.file = str(saved) if saved else None

    return build_page(
        rows,
        op="profile.photo.list",
        kind=PageKind.PARTICIPANTS,
        state={"offset": offset + len(rows)},
        account=ctx.account,
        limit=limit,
        total=getattr(result, "count", None),
    )


SPEC_PHOTO_LIST = OperationSpec(
    id="profile.photo.list",
    request=PhotoListReq,
    response=Page[ProfilePhoto],
    impl=photo_list,
    summary="List (and optionally download) my profile photos",
    paginated=PageKind.PARTICIPANTS,
    idempotent=True,
    rate_class="file",
    timeout_s=300,
    columns=("id", "date", "video", "current"),
    headers=("Photo", "Taken", "Video", "Current"),
    example={
        "items": [{"id": 55123, "date": "2026-08-01T10:00:00Z", "video": False, "current": True}],
        "has_more": False,
    },
    example_args="profile photo list",
    covers=("profile.photo-list", "profile.photos-list-history"),
    tags=frozenset({"agent-safe"}),
)


class PhotoSetReq(Request):
    file: Annotated[
        str | None,
        arg(0, metavar="FILE", required=False, kind="path", help="Image or video to upload."),
    ] = None
    video: Annotated[bool, opt("--video", help="Treat the file as an animated avatar.")] = False
    start_ts: Annotated[
        float | None, opt("--start-ts", metavar="SECONDS", help="Cover frame of a video avatar.")
    ] = None
    photo_id: Annotated[
        str | None, opt("--photo-id", metavar="ID", help="Re-use a photo from `photo list`.")
    ] = None
    emoji: Annotated[
        str | None, opt("--emoji", metavar="ID", help="Build the avatar from a custom emoji.")
    ] = None
    colors: Annotated[
        str | None, opt("--colors", metavar="LIST", help="Background gradient for --emoji.")
    ] = None
    sticker_set: Annotated[
        str | None,
        opt("--sticker-set", metavar="SET:ID", help="Sticker markup instead of a custom emoji."),
    ] = None
    fallback: Annotated[
        bool, opt("--fallback", help="Set the public fallback photo instead of the main one.")
    ] = False


async def photo_set(ctx: OpContext, req: PhotoSetReq) -> ProfilePhotoSet:
    """Set my avatar from a file, a video, an older photo or a custom emoji.

    The fallback photo is what people who may *not* see the real avatar get,
    so it only means anything next to a restrictive `privacy set
    profile-photo` rule — setting one without that rule changes nothing
    anybody will ever see.
    """
    from telethon.tl import types
    from telethon.tl.functions import photos as fn

    handle = client(ctx)

    if req.photo_id:
        if not req.photo_id.strip().lstrip("-").isdigit():
            raise UsageError("--photo-id wants a photo id from `profile photo list`", field="photo")
        photo = await _input_photo(ctx, int(req.photo_id))
        result = await handle(fn.UpdateProfilePhotoRequest(id=photo, fallback=req.fallback or None))
        return ProfilePhotoSet(photo_id=_photo_id_of(result), fallback=req.fallback, is_video=False)

    kwargs: dict[str, Any] = {"fallback": req.fallback or None}
    markup: Any = None
    colours = [
        _settings.color_int(value, field="colors")
        for value in (req.colors or "").split(",")
        if value.strip()
    ]
    if req.emoji:
        if not req.emoji.strip().isdigit():
            raise UsageError("--emoji wants a custom-emoji document id", field="emoji")
        markup = types.VideoSizeEmojiMarkup(
            emoji_id=int(req.emoji), background_colors=[c for c in colours if c is not None]
        )
    elif req.sticker_set:
        from tlgr.ops import _media

        short, _, sticker_id = req.sticker_set.rpartition(":")
        if not short or not sticker_id.isdigit():
            raise UsageError("--sticker-set wants '<set>:<sticker id>'", field="sticker_set")
        markup = types.VideoSizeStickerMarkup(
            stickerset=_media.sticker_set_ref(short, field="sticker_set"),
            sticker_id=int(sticker_id),
            background_colors=[c for c in colours if c is not None],
        )
    if markup is not None:
        kwargs["video_emoji_markup"] = markup
    else:
        if not req.file:
            raise UsageError("give a FILE, or --photo-id / --emoji / --sticker-set", field="file")
        path = Path(os.path.expanduser(req.file))
        if not path.exists():
            raise UsageError(f"{req.file} does not exist", field="file")
        upload = getattr(ctx, "upload_file", None)
        if upload is None:  # pragma: no cover - the daemon always supplies one
            raise UsageError("this context cannot upload files")
        handle_file = await upload(path)
        if req.video:
            kwargs["video"] = handle_file
            kwargs["video_start_ts"] = req.start_ts
        else:
            kwargs["file"] = handle_file

    result = await handle(fn.UploadProfilePhotoRequest(**kwargs))
    ctx.emit("profile_photo", {"fallback": req.fallback})
    return ProfilePhotoSet(
        photo_id=_photo_id_of(result),
        is_video=bool(req.video),
        fallback=req.fallback,
        emoji_markup=markup is not None,
    )


def _photo_id_of(result: Any) -> int | None:
    photo = getattr(result, "photo", None)
    value = getattr(photo, "id", None)
    return int(value) if value is not None else None


async def _input_photo(ctx: OpContext, photo_id: int) -> Any:
    """An `InputPhoto` for one of my own photos, with its live file reference.

    The access hash and file reference are only valid for the session that
    fetched them, so the photo is looked up again rather than reconstructed
    from an id a caller kept from yesterday.
    """
    from telethon.tl import types
    from telethon.tl.functions import photos as fn

    result = await client(ctx)(
        fn.GetUserPhotosRequest(user_id=types.InputUserSelf(), offset=0, max_id=0, limit=100)
    )
    for photo in getattr(result, "photos", None) or []:
        if int(getattr(photo, "id", 0) or 0) == photo_id:
            return types.InputPhoto(
                id=photo.id,
                access_hash=photo.access_hash,
                file_reference=getattr(photo, "file_reference", b"") or b"",
            )
    raise NotFoundError(f"photo {photo_id} is not in my photo history")


SPEC_PHOTO_SET = OperationSpec(
    id="profile.photo.set",
    request=PhotoSetReq,
    response=ProfilePhotoSet,
    impl=photo_set,
    summary="Set my profile photo from a file, a video, an older photo or a custom emoji",
    description=(
        "v1's implementation called `client.upload_profile_photo()`, which "
        "Telethon 1.44 does not have; this uploads the file and sends raw "
        "`photos.uploadProfilePhoto`."
    ),
    mutating=True,
    rate_class="file",
    timeout_s=300,
    columns=("photo_id", "is_video", "fallback"),
    headers=("Photo", "Video", "Fallback"),
    example={"photo_id": 55123, "is_video": False, "fallback": False},
    example_args="profile photo set avatar.jpg",
    covers=(
        "profile.contact-personal-photo",
        "profile.photo-emoji-markup",
        "profile.photo-fallback",
        "profile.photo-fallback-public",
        "profile.photo-set",
        "profile.photo-set-as-main",
        "profile.photo-set-emoji-sticker",
        "profile.photo-set-existing",
        "profile.photo-set-video",
        "profile.photo-upload",
        "profile.photo-upload-video",
    ),
    tags=frozenset({"visible-to-others"}),
)


class PhotoDeleteReq(Request):
    photo_id: Annotated[
        tuple[str, ...],
        arg(0, metavar="PHOTO_ID", required=False, variadic=True, help="Photos to delete."),
    ] = ()
    current: Annotated[
        bool, opt("--current", help="Delete the current photo; the previous one is promoted.")
    ] = False
    every: Annotated[bool, opt("--every", help="Delete every profile photo.")] = False


async def photo_delete(ctx: OpContext, req: PhotoDeleteReq) -> PhotosDeleted:
    """Delete profile photos.

    Deleting the current one promotes the previous one, which is Telegram's
    behaviour and not tlgr's: there is no "no avatar, but keep the history"
    state short of `--every`.
    """
    from telethon.tl import types
    from telethon.tl.functions import photos as fn

    handle = client(ctx)
    wanted: list[int] = []
    if req.every or req.current:
        result = await handle(
            fn.GetUserPhotosRequest(user_id=types.InputUserSelf(), offset=0, max_id=0, limit=100)
        )
        photos = list(getattr(result, "photos", None) or [])
        if req.current:
            me = await handle.get_me()
            current = getattr(getattr(me, "photo", None), "photo_id", None)
            photos = [p for p in photos if current and int(p.id) == int(current)]
        wanted = [int(photo.id) for photo in photos]
    for value in req.photo_id:
        if not str(value).lstrip("-").isdigit():
            raise UsageError(f"{value!r} is not a photo id", field="photo_id")
        wanted.append(int(value))
    if not wanted:
        if req.every or req.current:
            return PhotosDeleted(deleted=0, already=True)
        raise UsageError("give one or more photo ids, or --current / --every", field="photo_id")

    inputs = [await _input_photo(ctx, photo_id) for photo_id in wanted]
    deleted = await handle(fn.DeletePhotosRequest(id=inputs))
    ctx.emit("profile_photo_deleted", {"photo_ids": wanted})
    return PhotosDeleted(deleted=len(list(deleted or wanted)), photo_ids=wanted)


SPEC_PHOTO_DELETE = OperationSpec(
    id="profile.photo.delete",
    request=PhotoDeleteReq,
    response=PhotosDeleted,
    impl=photo_delete,
    summary="Delete profile photos",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("deleted", "photo_ids"),
    headers=("Deleted", "Photos"),
    example={"deleted": 1, "photo_ids": [55123]},
    example_args="profile photo delete 55123",
    covers=("profile.photo-delete",),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# profile presence set
# ---------------------------------------------------------------------------


class PresenceSetReq(Request):
    state: Annotated[str, arg(0, metavar="STATE", help="online or offline.")]


async def presence_set(ctx: OpContext, req: PresenceSetReq) -> PresenceSet:
    """Go online or offline.

    A daemon needs a *policy* here, not a default. Always reporting online
    advertises that something is running around the clock; reading history
    while reporting offline is the classic bot tell. tlgr therefore never
    reports presence on its own — this command, and the `presence` config
    key, are the only two things that do.
    """
    from telethon.tl.functions import account as fn

    wanted = req.state.strip().lower()
    if wanted not in ("online", "offline"):
        raise UsageError("STATE is `online` or `offline`", field="state")
    online = wanted == "online"
    await client(ctx)(fn.UpdateStatusRequest(offline=not online))
    ctx.emit("profile_presence", {"online": online})
    return PresenceSet(online=online)


SPEC_PRESENCE_SET = OperationSpec(
    id="profile.presence.set",
    request=PresenceSetReq,
    response=PresenceSet,
    impl=presence_set,
    summary="Go online or offline (account.updateStatus)",
    aliases=("profile.online", "profile.offline"),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("online",),
    headers=("Online",),
    example={"online": True},
    example_args="profile presence set online",
    covers=("profile.online-status",),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# profile status list / set
# ---------------------------------------------------------------------------


class StatusListReq(Request):
    recent: Annotated[bool, opt("--recent", help="Recently used statuses.")] = False
    default: Annotated[bool, opt("--default", help="Telegram's default set.")] = False
    collectible: Annotated[
        bool, opt("--collectible", help="Collectible gift statuses you may wear.")
    ] = False
    groups: Annotated[bool, opt("--groups", help="The category chips.")] = False
    clear_recent: Annotated[bool, opt("--clear-recent", help="Clear the recent list.")] = False


async def status_list(ctx: OpContext, req: StatusListReq) -> Page[EmojiStatusItem]:
    """Browse the emoji statuses this account may wear.

    Four server lists behind one command, because the GUI shows them as four
    tabs of one picker. The op is a read, so `--dry-run` does not
    short-circuit it centrally — which is why `--clear-recent`, the single
    write in here, checks `ctx.dry_run` itself rather than being exempt.
    """
    from telethon.tl.functions import account as fn
    from telethon.tl.functions import messages as mfn

    handle = client(ctx)
    rows: list[EmojiStatusItem] = []

    if req.clear_recent:
        if getattr(ctx, "dry_run", False):
            ctx.warn("--dry-run: the recent emoji-status list was left alone")
            return Page(items=[], has_more=False, total=0)
        await handle(fn.ClearRecentEmojiStatusesRequest())
        return Page(items=[], has_more=False, total=0)

    if req.groups:
        result = await handle(mfn.GetEmojiStatusGroupsRequest(hash=0))
        for group in getattr(result, "groups", None) or []:
            title = str(getattr(group, "title", "") or "")
            for document_id in getattr(group, "document_id", None) or []:
                rows.append(EmojiStatusItem(document_id=int(document_id), group=title))
        return Page(items=rows, has_more=False, total=len(rows))

    wanted = [
        ("recent", req.recent, fn.GetRecentEmojiStatusesRequest),
        ("default", req.default, fn.GetDefaultEmojiStatusesRequest),
        ("collectible", req.collectible, fn.GetCollectibleEmojiStatusesRequest),
    ]
    if not any(flag for _, flag, _ in wanted):
        wanted = [(name, True, request) for name, _, request in wanted]

    for name, flag, request in wanted:
        if not flag:
            continue
        result = await handle(request(hash=0))
        for status in getattr(result, "statuses", None) or []:
            rows.append(
                EmojiStatusItem(
                    document_id=int(getattr(status, "document_id", 0) or 0),
                    collectible_id=getattr(status, "collectible_id", None),
                    title=getattr(status, "title", None),
                    slug=getattr(status, "slug", None),
                    group=name,
                    until=fmt_dt(getattr(status, "until", None)),
                )
            )
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_STATUS_LIST = OperationSpec(
    id="profile.status.list",
    request=StatusListReq,
    response=Page[EmojiStatusItem],
    impl=status_list,
    summary="Browse emoji-status suggestions (recent, default, themed groups, collectibles)",
    description=(
        "A read, with one exception: `--clear-recent` empties the recent "
        "list. It honours `--dry-run` on its own rather than making the "
        "whole listing a mutating operation."
    ),
    paginated=PageKind.LOCAL,
    columns=("document_id", "collectible_id", "title", "group"),
    headers=("Emoji", "Collectible", "Title", "List"),
    example={"items": [{"document_id": 5301, "group": "recent"}], "has_more": False},
    example_args="profile status list --recent",
    covers=(
        "emoji.status-lists",
        "profile.emoji-status-collectible",
        "profile.emoji-status-suggestions",
    ),
)


class StatusSetReq(Request):
    emoji: Annotated[
        str | None,
        arg(0, metavar="EMOJI", required=False, help="Document id, collectible:<id>, or 'none'."),
    ] = None
    until: Annotated[
        str | None, opt("--until", metavar="WHEN", kind="datetime", help="Expire the status.")
    ] = None
    clear: Annotated[bool, opt("--clear", help="Remove the status.")] = False


async def status_set(ctx: OpContext, req: StatusSetReq) -> EmojiStatusSet:
    """Set or clear my emoji status, including a collectible gift.

    A collectible status and a collectible profile palette are mutually
    exclusive on the server: setting one silently clears the other. tlgr says
    so in the docs rather than pretending both can be worn.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    until = parse_dt(req.until) if req.until else None
    if req.clear or (req.emoji or "").strip().lower() in ("none", "clear"):
        await client(ctx)(fn.UpdateEmojiStatusRequest(emoji_status=types.EmojiStatusEmpty()))
        return EmojiStatusSet(cleared=True)
    if not req.emoji:
        raise UsageError("give an emoji document id, collectible:<id>, or --clear", field="emoji")

    text = req.emoji.strip()
    if text.lower().startswith("collectible:"):
        value = text.split(":", 1)[1]
        if not value.isdigit():
            raise UsageError("collectible:<id> wants a collectible id", field="emoji")
        status: Any = types.InputEmojiStatusCollectible(collectible_id=int(value), until=until)
        result = EmojiStatusSet(collectible_id=int(value))
    else:
        if not text.isdigit():
            raise UsageError("give a custom-emoji document id, or collectible:<id>", field="emoji")
        status = types.EmojiStatus(document_id=int(text), until=until)
        result = EmojiStatusSet(document_id=int(text))
    await client(ctx)(fn.UpdateEmojiStatusRequest(emoji_status=status))
    result.until = fmt_dt(until)
    result.until_unix = to_unix(until)
    ctx.emit("profile_status", {"document_id": result.document_id})
    return result


SPEC_STATUS_SET = OperationSpec(
    id="profile.status.set",
    request=StatusSetReq,
    response=EmojiStatusSet,
    impl=status_set,
    summary="Set or clear my emoji status (including a collectible gift)",
    description=(
        "Premium only. A collectible status and a collectible message palette "
        "cannot both be worn: the server clears one when you set the other."
    ),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("document_id", "collectible_id", "until", "cleared"),
    headers=("Emoji", "Collectible", "Until", "Cleared"),
    example={"document_id": 5301, "until": "2026-09-10T00:00:00Z"},
    example_args="profile status set 5301 --until +7d",
    covers=("emoji.status-set", "profile.emoji-status"),
    covers_partial=("profile.emoji-status-collectible",),
    coverage_note="Browsing the wearable collectibles is `profile status list --collectible`.",
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# profile color list / set
# ---------------------------------------------------------------------------


class ColorListReq(Request):
    profile: Annotated[
        bool, opt("--profile", help="Profile-page palettes instead of name/message palettes.")
    ] = False
    emojis: Annotated[bool, opt("--emojis", help="Also list the default background emojis.")] = (
        False
    )


async def color_list(ctx: OpContext, req: ColorListReq) -> Page[ColorPalette]:
    """The accent palettes this account may wear.

    Ids 0-6 come back with no colours at all, because every client draws them
    from a built-in table. Reporting them as empty would read as "no colours
    available"; `builtin` plus the name is the honest answer.
    """
    from telethon.tl.functions import help as fn

    handle = client(ctx)
    request = fn.GetPeerProfileColorsRequest if req.profile else fn.GetPeerColorsRequest
    result = await handle(request(hash=0))
    rows: list[ColorPalette] = []
    for option in getattr(result, "colors", None) or []:
        color_id = int(getattr(option, "color_id", 0) or 0)
        colors = getattr(option, "colors", None)
        dark = getattr(option, "dark_colors", None)
        rows.append(
            ColorPalette(
                color_id=color_id,
                colors=[_settings.color_text(v) for v in getattr(colors, "colors", None) or []],
                dark_colors=[_settings.color_text(v) for v in getattr(dark, "colors", None) or []],
                min_level=int(getattr(option, "channel_min_level", 0) or 0),
                channel_min_level=getattr(option, "channel_min_level", None),
                group_min_level=getattr(option, "group_min_level", None),
                hidden=bool(getattr(option, "hidden", False)),
                builtin=color_id < len(_BUILTIN_PALETTES),
            )
        )
    if req.emojis:
        from telethon.tl.functions import account as afn

        emojis = await handle(afn.GetDefaultBackgroundEmojisRequest(hash=0))
        for document in getattr(emojis, "documents", None) or []:
            rows.append(ColorPalette(color_id=-1, colors=[str(getattr(document, "id", 0) or 0)]))
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_COLOR_LIST = OperationSpec(
    id="profile.color.list",
    request=ColorListReq,
    response=Page[ColorPalette],
    impl=color_list,
    summary="List the name and profile colour palettes",
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("color_id", "colors", "dark_colors", "min_level", "hidden"),
    headers=("Id", "Light", "Dark", "Min level", "Hidden"),
    example={"items": [{"color_id": 5, "colors": ["#3FA3E8"], "min_level": 0}], "has_more": False},
    example_args="profile color list",
    covers=("profile.name-color", "profile.profile-color"),
    tags=frozenset({"agent-safe"}),
)


class ColorSetReq(Request):
    color: Annotated[
        str, arg(0, metavar="COLOR", help="Palette id, collectible:<slug|id>, or 'none'.")
    ]
    profile: Annotated[
        bool, opt("--profile", help="Change the profile-page colour, not the message colour.")
    ] = False
    emoji: Annotated[
        str | None, opt("--emoji", metavar="ID", help="Background custom-emoji document id.")
    ] = None


async def color_set(ctx: OpContext, req: ColorSetReq) -> ColorSet:
    """Set my message accent colour, profile colour or collectible palette.

    A collectible palette is a gift you own, and the server only accepts it
    for the *message* colour — `--profile` with one is refused here rather
    than sent and rejected with an error that names neither flag.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    text = req.color.strip()
    emoji = int(req.emoji) if req.emoji and req.emoji.isdigit() else None
    if text.lower() in ("none", "clear", "default"):
        await client(ctx)(fn.UpdateColorRequest(for_profile=req.profile or None, color=None))
        return ColorSet(for_profile=req.profile)

    if text.lower().startswith("collectible:"):
        if req.profile:
            raise UsageError(
                "a collectible palette is only accepted for the message colour, not for --profile",
                field="color",
            )
        value = text.split(":", 1)[1]
        gift_id = int(value) if value.isdigit() else await _collectible_id(ctx, value)
        await client(ctx)(
            fn.UpdateColorRequest(color=types.InputPeerColorCollectible(collectible_id=gift_id))
        )
        return ColorSet(collectible_id=gift_id)

    if not text.lstrip("-").isdigit():
        raise UsageError("COLOR is a palette id, collectible:<slug|id>, or 'none'", field="color")
    await client(ctx)(
        fn.UpdateColorRequest(
            for_profile=req.profile or None,
            color=types.PeerColor(color=int(text), background_emoji_id=emoji),
        )
    )
    ctx.emit("profile_color", {"color": int(text), "for_profile": req.profile})
    return ColorSet(color=int(text), background_emoji_id=emoji, for_profile=req.profile)


async def _collectible_id(ctx: OpContext, slug: str) -> int:
    """The collectible id behind a gift slug, so `collectible:<slug>` works."""
    from telethon.tl.functions import payments as fn

    result = await client(ctx)(fn.GetUniqueStarGiftRequest(slug=_settings.slug_of(slug)))
    gift = getattr(result, "gift", None)
    value = getattr(gift, "id", None)
    if value is None:
        raise NotFoundError(f"no collectible named {slug!r}")
    return int(value)


SPEC_COLOR_SET = OperationSpec(
    id="profile.color.set",
    request=ColorSetReq,
    response=ColorSet,
    impl=color_set,
    summary="Set my message accent colour, profile colour, or a collectible palette",
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("color", "collectible_id", "background_emoji_id", "for_profile"),
    headers=("Palette", "Collectible", "Emoji", "Profile"),
    example={"color": 5, "for_profile": False},
    example_args="profile color set 5",
    covers=("gift.as-peer-color", "profile.collectible-message-palette"),
    covers_partial=("profile.name-color", "profile.profile-color"),
    coverage_note="Listing the palettes is `profile color list`.",
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# profile channel list / music list / wallpaper set / link
# ---------------------------------------------------------------------------


class ChannelListReq(Request):
    pass


async def channel_list(ctx: OpContext, req: ChannelListReq) -> Page[AdminedChannel]:
    """Public channels I administer that may be shown on my profile."""
    from telethon.tl import types
    from telethon.tl.functions import channels as fn
    from telethon.tl.functions import users as ufn

    handle = client(ctx)
    result = await handle(fn.GetAdminedPublicChannelsRequest(for_personal=True))
    answer = await handle(ufn.GetFullUserRequest(id=types.InputUserSelf()))
    current = getattr(getattr(answer, "full_user", None), "personal_channel_id", None)
    rows = [
        AdminedChannel(
            id=int(getattr(chat, "id", 0) or 0),
            title=str(getattr(chat, "title", "") or ""),
            username=getattr(chat, "username", None),
            participants_count=getattr(chat, "participants_count", None),
            current=current is not None and int(getattr(chat, "id", 0) or 0) == int(current),
        )
        for chat in getattr(result, "chats", None) or []
    ]
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_CHANNEL_LIST = OperationSpec(
    id="profile.channel.list",
    request=ChannelListReq,
    response=Page[AdminedChannel],
    impl=channel_list,
    summary="List public channels I administer that can be shown on my profile",
    description="Pick one with `profile update --channel <chat>`; `none` unlinks it.",
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("id", "title", "username", "participants_count", "current"),
    headers=("ID", "Title", "Username", "Members", "Shown"),
    example={"items": [{"id": 777, "title": "Notes", "username": "ada_notes"}], "has_more": False},
    example_args="profile channel list",
    covers=("profile.personal-channel",),
    tags=frozenset({"agent-safe"}),
)


class MusicListReq(Request):
    user: Annotated[
        PeerRef | None,
        opt("--user", metavar="USER", kind="user", help="Whose profile music (default: me)."),
    ] = None


async def music_list(ctx: OpContext, req: MusicListReq) -> Page[MusicTrack]:
    """The music pinned to a profile — mine unless `--user` names another.

    Somebody else's list obeys `inputPrivacyKeySavedMusic`, so an empty
    answer is not evidence that they pinned nothing.
    """
    from tlgr.models.peer import parse_peer_ref
    from tlgr.ops.user import MusicListReq as UserMusicListReq
    from tlgr.ops.user import music_list as user_music_list

    target = req.user or parse_peer_ref("me")
    return await user_music_list(ctx, UserMusicListReq(user=target))


SPEC_MUSIC_LIST = OperationSpec(
    id="profile.music.list",
    request=MusicListReq,
    response=Page[MusicTrack],
    impl=music_list,
    summary="List the music shown on a profile",
    paginated=PageKind.PARTICIPANTS,
    idempotent=True,
    rate_class="file",
    timeout_s=300,
    columns=("id", "title", "performer", "duration"),
    headers=("Document", "Title", "Performer", "Seconds"),
    example={"items": [{"id": 991, "title": "Nocturne", "performer": "Chopin"}], "has_more": False},
    example_args="profile music list",
    covers=("profile.saved-music", "profile.saved-music-list", "stories.story-music-save"),
    tags=frozenset({"agent-safe"}),
)


class WallpaperSetReq(Request):
    source: Annotated[
        str | None,
        arg(0, metavar="SOURCE", required=False, help="Image file, or a wallpaper slug."),
    ] = None
    blur: Annotated[bool, opt("--blur", help="wallPaperSettings.blur.")] = False
    motion: Annotated[bool, opt("--motion", help="wallPaperSettings.motion.")] = False
    intensity: Annotated[int | None, opt("--intensity", metavar="N", help="Pattern intensity.")] = (
        None
    )
    colors: Annotated[
        str | None, opt("--colors", metavar="LIST", help="Up to four gradient colours.")
    ] = None
    for_chat: Annotated[
        bool, opt("--for-chat", help="Upload it for use as a per-chat wallpaper.")
    ] = False
    save: Annotated[
        bool, opt("--save", help="Only add it to the saved list, do not install it.")
    ] = False
    reset: Annotated[bool, opt("--reset", help="Wipe the saved wallpaper list.")] = False


async def wallpaper_set(ctx: OpContext, req: WallpaperSetReq) -> WallpaperInstalled:
    """Upload or install my chat wallpaper, or reset the saved list.

    The catalogue itself is `media wallpaper list`; setting one chat's
    wallpaper is `chat wallpaper set`, which is a different server call with
    a "for both sides" flag. This is the account-wide write path, and it is
    here rather than in `media` because the GUI reaches it from Settings ▸
    Chat Settings and not from a file picker.
    """
    from tlgr.ops.media import WallpaperSetReq as MediaSetReq
    from tlgr.ops.media import WallpaperUploadReq, wallpaper_upload
    from tlgr.ops.media import wallpaper_set as media_wallpaper_set

    colours = [value.strip() for value in (req.colors or "").split(",") if value.strip()]
    if req.reset:
        return await media_wallpaper_set(ctx, MediaSetReq(reset=True))

    if not req.source:
        raise UsageError("give an image file or a wallpaper slug, or --reset", field="source")

    path = Path(os.path.expanduser(req.source))
    slug = req.source
    if path.exists():
        uploaded = await wallpaper_upload(
            ctx,
            WallpaperUploadReq(
                path=str(path),
                colors=colours,
                blur=req.blur,
                motion=req.motion,
                intensity=req.intensity if req.intensity is not None else 50,
                pattern=req.intensity is not None,
                for_chat=bool(req.for_chat),
            ),
        )
        slug = uploaded.slug or ""
        if req.save:
            return WallpaperInstalled(slug=slug, saved=True, settings=uploaded.settings)

    return await media_wallpaper_set(
        ctx,
        MediaSetReq(
            wallpaper=slug,
            blur=req.blur,
            motion=req.motion,
            intensity=req.intensity,
            colors=colours,
            save_only=req.save,
        ),
    )


SPEC_WALLPAPER_SET = OperationSpec(
    id="profile.wallpaper.set",
    request=WallpaperSetReq,
    response=WallpaperInstalled,
    impl=wallpaper_set,
    summary="Upload/install my chat wallpaper, or reset the saved wallpaper list",
    mutating=True,
    rate_class="file",
    timeout_s=300,
    columns=("slug", "installed", "saved", "reset"),
    headers=("Slug", "Installed", "Saved", "Reset"),
    example={"slug": "Ycb0FfC6", "installed": True, "saved": True},
    example_args="profile wallpaper set Ycb0FfC6",
    covers=("wallpaper.save-install-reset", "wallpaper.upload"),
)


class LinkReq(Request):
    target: Annotated[
        str | None,
        arg(0, metavar="TARGET", required=False, help="@username or +888…; default me."),
    ] = None
    qr: Annotated[bool, opt("--qr", help="Render a unicode-block QR of the link.")] = False
    out: Annotated[
        str | None, opt("--out", metavar="PATH", kind="path", help="Write a PNG QR instead.")
    ] = None
    collectible: Annotated[
        bool, opt("--collectible", help="Fetch Fragment purchase date and price.")
    ] = False


async def link(ctx: OpContext, req: LinkReq) -> ProfileLink:
    """My public link and QR code, and Fragment's record of a collectible.

    An account with no username has only the `tg://user?id=` form, and that
    only opens for peers who already know it — which is why the answer says
    `resolvable_by_strangers: false` instead of handing back a link that
    quietly does nothing.
    """
    from telethon.tl import types
    from telethon.tl.functions import fragment as fn

    handle = client(ctx)
    target = (req.target or "").strip()
    result = ProfileLink()

    if not target or target.lower() in ("me", "self"):
        me = await handle.get_me()
        result.user_id = int(getattr(me, "id", 0) or 0)
        result.username = getattr(me, "username", None)
    elif target.startswith("+"):
        result.username = target
    else:
        result.username = target.lstrip("@")

    if result.username:
        result.link = f"https://t.me/{str(result.username).lstrip('+')}"
    else:
        result.link = f"tg://user?id={result.user_id}"
        result.resolvable_by_strangers = False

    if req.collectible and result.username:
        name = str(result.username)
        collectible = (
            types.InputCollectiblePhone(phone=name)
            if name.startswith("+")
            else types.InputCollectibleUsername(username=name)
        )
        try:
            info = await handle(fn.GetCollectibleInfoRequest(collectible=collectible))
        except Exception as exc:
            ctx.warn(f"no Fragment record for {name}: {exc}")
        else:
            result.collectible = {
                "purchase_date": fmt_dt(getattr(info, "purchase_date", None)),
                "currency": getattr(info, "currency", None),
                "amount": getattr(info, "amount", None),
                "crypto_currency": getattr(info, "crypto_currency", None),
                "crypto_amount": getattr(info, "crypto_amount", None),
                "url": getattr(info, "url", None),
            }

    if req.qr or req.out:
        result.qr, result.qr_path = _qr(ctx, result.link, req.out)
    return result


def _qr(ctx: OpContext, text: str, out: str | None) -> tuple[str | None, str | None]:
    """The link as a QR, reusing the encoder `chat invite link` already uses.

    A QR carries nothing the link does not, so it is pure local rendering —
    and a second implementation of it would be a second thing to get wrong.
    """
    from tlgr.ops.chat_invite import _render_qr

    return _render_qr(ctx, text, out)


SPEC_LINK = OperationSpec(
    id="profile.link",
    request=LinkReq,
    response=ProfileLink,
    impl=link,
    summary="My public link and QR code, and Fragment details for a username or phone",
    description=(
        "The GUI's styled QR *image* is a rendering choice a terminal has no "
        "use for; the link, a block QR and an optional PNG are the parts that "
        "carry information."
    ),
    idempotent=True,
    columns=("link", "username", "resolvable_by_strangers"),
    headers=("Link", "Username", "Public"),
    example={"link": "https://t.me/ada", "username": "ada"},
    example_args="profile link --qr",
    covers=("profile.collectible-info", "profile.qr-code"),
    tags=frozenset({"agent-safe"}),
)

__all__ = [name for name in dir() if name.startswith("SPEC_")]
