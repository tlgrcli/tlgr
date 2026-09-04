"""The `privacy` group: Settings ▸ Privacy and Security, minus the sessions.

Fourteen privacy keys, eight global switches and two blocklists, and the
whole group exists to make two dangerous APIs safe to use from a script.

* **`account.setPrivacy` replaces the whole ordered rule vector.** Sending
  "allow contacts" wipes every exception the user had. So `privacy set`
  always GETs first, and the `--add-*`/`--remove` flags exist precisely so a
  script never has to re-state a list it did not mean to touch.
* **`account.setGlobalPrivacySettings` replaces the whole constructor.** Same
  hazard, same answer: read, patch the named fields, write. A switch nobody
  mentioned is written back exactly as it was found.

The rule vocabulary — `everybody`, `contacts`, `close-friends`, `premium`,
`bots`, `nobody` — is the one the official clients show, not the one the TL
schema uses, because `privacyValueDisallowAll` plus `privacyValueAllowUsers`
is a *shape* rather than a setting anybody chose.

Telethon is imported inside functions, never at module scope (§2.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from tlgr.core.errors import UsageError
from tlgr.core.pagination import PageKind
from tlgr.models.base import Request
from tlgr.models.contact import BlockedPeer, BlockedSet
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.models.privacy import GlobalPrivacy, PaidMessageRevenue, PrivacyRule, PrivacySettings
from tlgr.ops import _settings
from tlgr.ops._common import client
from tlgr.ops._params import arg, opt
from tlgr.ops._spec import OpContext, OperationSpec

__all__ = [name for name in dir() if name.startswith("SPEC_")]

#: tlgr's key name → the `inputPrivacyKey*` class name. The tlgr spelling is
#: the GUI's row label; the TL name is an implementation detail nobody should
#: have to learn to mute their last-seen time.
KEYS: dict[str, str] = {
    "last-seen": "InputPrivacyKeyStatusTimestamp",
    "profile-photo": "InputPrivacyKeyProfilePhoto",
    "phone-number": "InputPrivacyKeyPhoneNumber",
    "forwards": "InputPrivacyKeyForwards",
    "calls": "InputPrivacyKeyPhoneCall",
    "phone-p2p": "InputPrivacyKeyPhoneP2P",
    "chat-invite": "InputPrivacyKeyChatInvite",
    "voice-messages": "InputPrivacyKeyVoiceMessages",
    "bio": "InputPrivacyKeyAbout",
    "birthday": "InputPrivacyKeyBirthday",
    "gifts-auto-save": "InputPrivacyKeyStarGiftsAutoSave",
    "no-paid-messages": "InputPrivacyKeyNoPaidMessages",
    "saved-music": "InputPrivacyKeySavedMusic",
    "added-by-phone": "InputPrivacyKeyAddedByPhone",
}

#: The base rules, in the order `account.setPrivacy` wants them written: the
#: broad allow/disallow first, exceptions after, because the server applies
#: the vector in order and a trailing `allowAll` would undo everything.
BASES = ("everybody", "contacts", "close-friends", "premium", "bots", "nobody")

#: `disallowedGiftsSettings` field ⇄ the keyword `--disallow-gifts` takes.
GIFT_KINDS: dict[str, str] = {
    "unlimited": "disallow_unlimited_stargifts",
    "limited": "disallow_limited_stargifts",
    "unique": "disallow_unique_stargifts",
    "premium": "disallow_premium_gifts",
    "from-channels": "disallow_stargifts_from_channels",
}


def _key_tl(name: str) -> Any:
    """The `inputPrivacyKey*` for a tlgr key name, or a usage error that lists them."""
    from telethon.tl import types

    wanted = name.strip().lower()
    if wanted == "stories":
        raise UsageError(
            "story visibility is not a privacy key: the audience is chosen per "
            "story (`story post --audience`) and the exclusion list is "
            "`story blocklist set` / `privacy blocked set --stories`",
            field="key",
        )
    if wanted not in KEYS:
        raise UsageError(
            f"unknown privacy key {name!r}; one of: {' '.join(sorted(KEYS))}", field="key"
        )
    return getattr(types, KEYS[wanted])()


def _rules_model(key: str, rules: Any) -> PrivacySettings:
    """A `privacyValue*` vector split into the shape the GUI shows.

    The base is whichever broad rule the vector carries; the four exception
    lists are the user/chat rules beside it. `raw_rules` keeps the server's
    own ordering so a later write can reproduce it exactly.
    """
    model = PrivacySettings(key=key, base="nobody")
    for rule in rules or []:
        name = type(rule).__name__.removeprefix("PrivacyValue")
        action = "allow" if name.startswith("Allow") else "disallow"
        scope = name.removeprefix("Allow").removeprefix("Disallow")
        ids = [int(v) for v in (getattr(rule, "users", None) or getattr(rule, "chats", None) or [])]
        model.raw_rules.append(
            PrivacyRule(action=action, scope=_SCOPE_NAMES.get(scope, scope.lower()), ids=ids)
        )
        if scope == "Users":
            (model.allow_users if action == "allow" else model.deny_users).extend(ids)
        elif scope == "ChatParticipants":
            (model.allow_chats if action == "allow" else model.deny_chats).extend(ids)
        elif scope in _BASE_FOR:
            model.base = _BASE_FOR[scope] if action == "allow" else _DENY_BASE[scope]
    return model


_SCOPE_NAMES = {
    "All": "all",
    "Contacts": "contacts",
    "CloseFriends": "close-friends",
    "Premium": "premium",
    "Bots": "bots",
    "Users": "users",
    "ChatParticipants": "chats",
}
_BASE_FOR = {
    "All": "everybody",
    "Contacts": "contacts",
    "CloseFriends": "close-friends",
    "Premium": "premium",
    "Bots": "bots",
}
#: `disallowContacts` and friends are the *other* half of the same switch:
#: "not my contacts" is how "nobody" is spelled for some keys.
_DENY_BASE = {
    "All": "nobody",
    "Contacts": "nobody",
    "CloseFriends": "nobody",
    "Premium": "nobody",
    "Bots": "nobody",
}


# ---------------------------------------------------------------------------
# privacy get / set
# ---------------------------------------------------------------------------


class GetReq(Request):
    key: Annotated[
        str | None,
        arg(0, metavar="KEY", required=False, help="One key; omit for every key."),
    ] = None
    resolve: Annotated[
        bool, opt("--resolve/--no-resolve", help="Resolve exception ids to names.")
    ] = True


async def get(ctx: OpContext, req: GetReq) -> Page[PrivacySettings]:
    """Read one privacy setting, or all of them.

    The output is exactly what `privacy set` accepts back, which is what
    makes "copy this account's privacy to that one" a pipeline rather than a
    reading exercise.
    """
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    wanted = [req.key.strip().lower()] if req.key else sorted(KEYS)
    rows: list[PrivacySettings] = []
    for name in wanted:
        answer = await handle(fn.GetPrivacyRequest(key=_key_tl(name)))
        model = _rules_model(name, getattr(answer, "rules", None))
        if req.resolve:
            known = _settings.entity_map(answer)
            model.peers = [
                peer
                for raw_id in (*model.allow_users, *model.deny_users)
                if (peer := _settings.peer_model(known.get(raw_id))) is not None
            ]
        rows.append(model)
    return Page(items=rows, has_more=False, total=len(rows))


SPEC_GET = OperationSpec(
    id="privacy.get",
    request=GetReq,
    response=Page[PrivacySettings],
    impl=get,
    summary="Read one privacy setting, or all of them",
    description=(
        "`base` is the headline value the GUI shows and the four lists are "
        "its exceptions; `raw_rules` keeps the server's own ordered vector so "
        "nothing is lost in the translation."
    ),
    paginated=PageKind.LOCAL,
    idempotent=True,
    columns=("key", "base", "allow_users", "deny_users"),
    headers=("Key", "Base", "Always allow", "Never allow"),
    example={
        "items": [{"key": "last-seen", "base": "contacts", "deny_users": [777123]}],
        "has_more": False,
    },
    example_args="privacy get last-seen",
    covers=(
        "contacts-users.privacy-about",
        "contacts-users.privacy-added-by-phone",
        "contacts-users.privacy-chat-invite",
        "contacts-users.privacy-forwards",
        "contacts-users.privacy-gifts",
        "contacts-users.privacy-no-paid-messages",
        "contacts-users.privacy-phone-number",
        "contacts-users.privacy-voice-messages",
        "gift.auto-save-privacy",
        "privacy.get-rules",
        "privacy.key-birthday",
        "privacy.key-calls",
        "privacy.key-no-paid-messages",
        "privacy.key-phone-number",
        "privacy.key-voice-messages",
    ),
    covers_partial=(
        "privacy.key-bio",
        "privacy.key-chat-invite",
        "privacy.key-forwards",
        "privacy.key-gifts-auto-save",
        "privacy.key-last-seen",
        "privacy.key-profile-photo",
        "privacy.key-saved-music",
    ),
    coverage_note="Writing any of these keys is `privacy set`.",
    tags=frozenset({"agent-safe"}),
)


class SetReq(Request):
    key: Annotated[str, arg(0, metavar="KEY", help="The privacy key to change.")]
    rule: Annotated[
        str | None,
        arg(1, metavar="RULE", required=False, help="everybody|contacts|close-friends|nobody…"),
    ] = None
    allow: Annotated[
        str | None, opt("--allow", metavar="LIST", help="Replace the 'always allow' list.")
    ] = None
    disallow: Annotated[
        str | None, opt("--disallow", metavar="LIST", help="Replace the 'never allow' list.")
    ] = None
    add_allow: Annotated[
        str | None, opt("--add-allow", metavar="LIST", help="Append to the allow list.")
    ] = None
    add_disallow: Annotated[
        str | None, opt("--add-disallow", metavar="LIST", help="Append to the deny list.")
    ] = None
    remove: Annotated[
        str | None, opt("--remove", metavar="LIST", help="Drop these from both lists.")
    ] = None
    clear_exceptions: Annotated[
        bool, opt("--clear-exceptions", help="Send only the base rule.")
    ] = False


async def _ids_of(ctx: OpContext, text: str | None) -> tuple[list[int], list[int]]:
    """A comma-separated peer list, split into `(user ids, chat ids)`."""
    users: list[int] = []
    chats: list[int] = []
    for entry in (text or "").split(","):
        ref = entry.strip()
        if not ref:
            continue
        peer = await _settings.resolve(ctx, ref)
        marked = _settings.peer_of(peer)
        (users if marked > 0 else chats).append(abs(marked) if marked < 0 else marked)
    return users, chats


async def set_(ctx: OpContext, req: SetReq) -> PrivacySettings:
    """Change a privacy key: a base rule plus optional exception lists.

    Read-modify-write, always. `account.setPrivacy` takes the *complete*
    ordered vector, so sending only what changed would delete everything
    else — which is the whole reason `--add-allow` and `--remove` exist.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    name = req.key.strip().lower()
    key = _key_tl(name)
    handle = client(ctx)
    current = _rules_model(
        name, getattr(await handle(fn.GetPrivacyRequest(key=key)), "rules", None)
    )

    base = current.base
    if req.rule is not None:
        base = req.rule.strip().lower()
        if base not in BASES:
            raise UsageError(f"RULE is one of: {' '.join(BASES)}", field="rule")

    allow_users, allow_chats = list(current.allow_users), list(current.allow_chats)
    deny_users, deny_chats = list(current.deny_users), list(current.deny_chats)

    if req.clear_exceptions:
        allow_users, allow_chats, deny_users, deny_chats = [], [], [], []
    if req.allow is not None:
        allow_users, allow_chats = await _ids_of(ctx, req.allow)
    if req.disallow is not None:
        deny_users, deny_chats = await _ids_of(ctx, req.disallow)
    if req.add_allow:
        more_users, more_chats = await _ids_of(ctx, req.add_allow)
        allow_users += [i for i in more_users if i not in allow_users]
        allow_chats += [i for i in more_chats if i not in allow_chats]
    if req.add_disallow:
        more_users, more_chats = await _ids_of(ctx, req.add_disallow)
        deny_users += [i for i in more_users if i not in deny_users]
        deny_chats += [i for i in more_chats if i not in deny_chats]
    if req.remove:
        gone_users, gone_chats = await _ids_of(ctx, req.remove)
        allow_users = [i for i in allow_users if i not in gone_users]
        deny_users = [i for i in deny_users if i not in gone_users]
        allow_chats = [i for i in allow_chats if i not in gone_chats]
        deny_chats = [i for i in deny_chats if i not in gone_chats]

    rules: list[Any] = []
    # The exceptions go first: the server evaluates the vector in order, so a
    # broad rule written before them would decide every case on its own.
    if allow_users:
        rules.append(
            types.InputPrivacyValueAllowUsers(
                users=[await _settings.input_user(ctx, str(i)) for i in allow_users]
            )
        )
    if deny_users:
        rules.append(
            types.InputPrivacyValueDisallowUsers(
                users=[await _settings.input_user(ctx, str(i)) for i in deny_users]
            )
        )
    if allow_chats:
        rules.append(types.InputPrivacyValueAllowChatParticipants(chats=allow_chats))
    if deny_chats:
        rules.append(types.InputPrivacyValueDisallowChatParticipants(chats=deny_chats))
    rules.append(_base_rule(base))

    answer = await handle(fn.SetPrivacyRequest(key=key, rules=rules))
    ctx.emit("privacy_set", {"key": name, "base": base})
    return _rules_model(name, getattr(answer, "rules", None))


def _base_rule(base: str) -> Any:
    from telethon.tl import types

    return {
        "everybody": types.InputPrivacyValueAllowAll,
        "contacts": types.InputPrivacyValueAllowContacts,
        "close-friends": types.InputPrivacyValueAllowCloseFriends,
        "premium": types.InputPrivacyValueAllowPremium,
        "bots": types.InputPrivacyValueAllowBots,
        "nobody": types.InputPrivacyValueDisallowAll,
    }[base]()


SPEC_SET = OperationSpec(
    id="privacy.set",
    request=SetReq,
    response=PrivacySettings,
    impl=set_,
    summary="Change a privacy setting: a base rule plus optional exception lists",
    description=(
        "`account.setPrivacy` replaces the whole ordered vector, so this "
        "always reads the current rules first. `--add-allow`/`--remove` edit "
        "the lists in place; `--allow`/`--disallow` replace them."
    ),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("key", "base", "allow_users", "deny_users"),
    headers=("Key", "Base", "Always allow", "Never allow"),
    example={"key": "last-seen", "base": "contacts", "deny_users": [777123]},
    example_args="privacy set last-seen contacts --add-disallow @nosy",
    covers=(
        "bots.privacy-rule-bots",
        "calls.privacy-p2p",
        "calls.privacy-who-can-call",
        "contacts-users.privacy-exception-lists",
        "contacts-users.user-status-reveal",
        "dialogs.new-chats-privacy",
        "gift.privacy-disallowed",
        "privacy.exceptions",
        "privacy.key-bio",
        "privacy.key-chat-invite",
        "privacy.key-forwards",
        "privacy.key-gifts-auto-save",
        "privacy.key-last-seen",
        "privacy.key-profile-photo",
        "privacy.key-saved-music",
        "privacy.set-rules",
    ),
    covers_partial=(
        "privacy.key-birthday",
        "privacy.key-calls",
        "privacy.key-no-paid-messages",
        "privacy.key-phone-number",
        "privacy.key-voice-messages",
    ),
    coverage_note="Reading any of these keys back is `privacy get`.",
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# privacy global get / set
# ---------------------------------------------------------------------------


def _global_model(raw: Any) -> GlobalPrivacy:
    gifts = getattr(raw, "disallowed_gifts", None)
    return GlobalPrivacy(
        hide_read_marks=getattr(raw, "hide_read_marks", None),
        archive_and_mute_new_noncontact_peers=getattr(
            raw, "archive_and_mute_new_noncontact_peers", None
        ),
        new_noncontact_peers_require_premium=getattr(
            raw, "new_noncontact_peers_require_premium", None
        ),
        noncontact_peers_paid_stars=getattr(raw, "noncontact_peers_paid_stars", None),
        keep_archived_unmuted=getattr(raw, "keep_archived_unmuted", None),
        keep_archived_folders=getattr(raw, "keep_archived_folders", None),
        display_gifts_button=getattr(raw, "display_gifts_button", None),
        disallowed_gifts=sorted(
            word for word, field in GIFT_KINDS.items() if getattr(gifts, field, False)
        ),
    )


class GlobalGetReq(Request):
    pass


async def global_get(ctx: OpContext, req: GlobalGetReq) -> GlobalPrivacy:
    """The account-wide privacy switches: read marks, archiving, paid messages."""
    from telethon.tl.functions import account as fn

    return _global_model(await client(ctx)(fn.GetGlobalPrivacySettingsRequest()))


SPEC_GLOBAL_GET = OperationSpec(
    id="privacy.global.get",
    request=GlobalGetReq,
    response=GlobalPrivacy,
    impl=global_get,
    summary="Read the global privacy settings (read time, archiving, paid messages, gifts)",
    idempotent=True,
    columns=(
        "hide_read_marks",
        "archive_and_mute_new_noncontact_peers",
        "new_noncontact_peers_require_premium",
        "noncontact_peers_paid_stars",
    ),
    headers=("Hide read marks", "Archive strangers", "Premium only", "Stars/message"),
    example={"hide_read_marks": False, "new_noncontact_peers_require_premium": True},
    example_args="privacy global get",
    covers=(
        "contacts-users.privacy-global",
        "privacy.global-disallowed-gifts",
        "privacy.global-keep-archived-unmuted",
        "privacy.global-require-premium-to-message",
    ),
    covers_partial=(
        "privacy.global-archive-new-noncontacts",
        "privacy.global-display-gifts-button",
        "privacy.global-hide-read-marks",
        "privacy.global-keep-archived-folders",
        "privacy.global-paid-messages-price",
    ),
    coverage_note="Writing any of these switches is `privacy global set`.",
    tags=frozenset({"agent-safe"}),
)


class GlobalSetReq(Request):
    hide_read_marks: Annotated[
        str | None, opt("--hide-read-marks", metavar="ON|OFF", help="Hide when I read messages.")
    ] = None
    archive_new_noncontacts: Annotated[
        str | None,
        opt("--archive-new-noncontacts", metavar="ON|OFF", help="Archive+mute unknown senders."),
    ] = None
    require_premium_to_message: Annotated[
        str | None,
        opt("--require-premium-to-message", metavar="ON|OFF", help="Premium non-contacts only."),
    ] = None
    paid_messages_price: Annotated[
        int | None,
        opt("--paid-messages-price", metavar="STARS", help="Stars per message; 0 turns it off."),
    ] = None
    keep_archived_unmuted: Annotated[
        str | None,
        opt("--keep-archived-unmuted", metavar="ON|OFF", help="Unmuted archived chats stay put."),
    ] = None
    keep_archived_folders: Annotated[
        str | None,
        opt("--keep-archived-folders", metavar="ON|OFF", help="Folder chats stay archived."),
    ] = None
    display_gifts_button: Annotated[
        str | None,
        opt("--display-gifts-button", metavar="ON|OFF", help="Gift button in private chats."),
    ] = None
    disallow_gifts: Annotated[
        str | None, opt("--disallow-gifts", metavar="LIST", help="Gift categories to refuse.")
    ] = None
    allow_gifts: Annotated[
        str | None, opt("--allow-gifts", metavar="LIST", help="Gift categories to accept again.")
    ] = None


async def global_set(ctx: OpContext, req: GlobalSetReq) -> GlobalPrivacy:
    """Change one global privacy switch; tlgr does the read-modify-write.

    `account.setGlobalPrivacySettings` replaces the whole constructor, so a
    flag nobody passed would be written back as `false` unless the current
    value is fetched first. It is, every time.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    handle = client(ctx)
    current = await handle(fn.GetGlobalPrivacySettingsRequest())
    before = _global_model(current)

    values: dict[str, Any] = {
        "hide_read_marks": before.hide_read_marks,
        "archive_and_mute_new_noncontact_peers": before.archive_and_mute_new_noncontact_peers,
        "new_noncontact_peers_require_premium": before.new_noncontact_peers_require_premium,
        "keep_archived_unmuted": before.keep_archived_unmuted,
        "keep_archived_folders": before.keep_archived_folders,
        "display_gifts_button": before.display_gifts_button,
    }
    changed: list[str] = []
    for flag, field in (
        ("hide_read_marks", "hide_read_marks"),
        ("archive_new_noncontacts", "archive_and_mute_new_noncontact_peers"),
        ("require_premium_to_message", "new_noncontact_peers_require_premium"),
        ("keep_archived_unmuted", "keep_archived_unmuted"),
        ("keep_archived_folders", "keep_archived_folders"),
        ("display_gifts_button", "display_gifts_button"),
    ):
        value = _settings.on_off(getattr(req, flag), field=flag)
        if value is not None:
            values[field] = value
            changed.append(field)

    stars = before.noncontact_peers_paid_stars
    if req.paid_messages_price is not None:
        if req.paid_messages_price < 0:
            raise UsageError(
                "--paid-messages-price cannot be negative", field="paid_messages_price"
            )
        stars = req.paid_messages_price or None
        changed.append("noncontact_peers_paid_stars")

    kinds = set(before.disallowed_gifts)
    for text, add in ((req.disallow_gifts, True), (req.allow_gifts, False)):
        for word in (part.strip().lower() for part in (text or "").split(",") if part.strip()):
            if word not in GIFT_KINDS:
                raise UsageError(
                    f"unknown gift category {word!r}; one of: {' '.join(sorted(GIFT_KINDS))}",
                    field="disallow_gifts",
                )
            kinds.add(word) if add else kinds.discard(word)
            changed.append("disallowed_gifts")

    if not changed:
        raise UsageError("nothing to change: pass at least one flag", field="hide_read_marks")

    gifts = (
        types.DisallowedGiftsSettings(**{GIFT_KINDS[word]: True for word in sorted(kinds)})
        if kinds
        else None
    )
    answer = await handle(
        fn.SetGlobalPrivacySettingsRequest(
            settings=types.GlobalPrivacySettings(
                **{k: v or None for k, v in values.items()},
                noncontact_peers_paid_stars=stars,
                disallowed_gifts=gifts,
            )
        )
    )
    ctx.emit("privacy_global", {"changed": sorted(set(changed))})
    result = _global_model(answer)
    result.changed = sorted(set(changed))
    return result


SPEC_GLOBAL_SET = OperationSpec(
    id="privacy.global.set",
    request=GlobalSetReq,
    response=GlobalPrivacy,
    impl=global_set,
    summary="Change global privacy settings (one flag at a time; tlgr read-modify-writes)",
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("hide_read_marks", "new_noncontact_peers_require_premium", "changed"),
    headers=("Hide read marks", "Premium only", "Changed"),
    example={"hide_read_marks": True, "changed": ["hide_read_marks"]},
    example_args="privacy global set --hide-read-marks on",
    covers=(
        "privacy.global-archive-new-noncontacts",
        "privacy.global-display-gifts-button",
        "privacy.global-hide-read-marks",
        "privacy.global-keep-archived-folders",
        "privacy.global-paid-messages-price",
    ),
    covers_partial=(
        "privacy.global-disallowed-gifts",
        "privacy.global-keep-archived-unmuted",
        "privacy.global-require-premium-to-message",
    ),
    coverage_note="Reading them back is `privacy global get`.",
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# privacy blocked list / set
# ---------------------------------------------------------------------------


class BlockedListReq(Request):
    stories: Annotated[
        bool, opt("--stories", help="The 'blocked from my stories' list instead.")
    ] = False


async def blocked_list(ctx: OpContext, req: BlockedListReq) -> Page[BlockedPeer]:
    """Blocked users and bots, or the separate story blocklist.

    The same list `contact blocked list` answers with — reached from the
    Privacy screen, which is where the GUI puts it, rather than from the
    address book.
    """
    from tlgr.ops.contact import BlockedListReq as ContactBlockedListReq
    from tlgr.ops.contact import blocked_list as contact_blocked_list

    return await contact_blocked_list(ctx, ContactBlockedListReq(stories=req.stories))


SPEC_BLOCKED_LIST = OperationSpec(
    id="privacy.blocked.list",
    request=BlockedListReq,
    response=Page[BlockedPeer],
    impl=blocked_list,
    summary="List blocked users (and the separate story-only block list)",
    aliases=("block.list",),
    paginated=PageKind.PARTICIPANTS,
    idempotent=True,
    columns=("peer.id", "peer.title", "date", "kind"),
    headers=("Id", "Peer", "Blocked", "List"),
    example={
        "items": [{"peer": {"id": 777123, "raw_id": 777123, "kind": "user"}, "kind": "main"}],
        "has_more": False,
    },
    example_args="privacy blocked list",
    covers_partial=("privacy.blocked-list",),
    coverage_note="The write half is `privacy blocked set`; `contact blocked list` is the same list.",
    tags=frozenset({"agent-safe"}),
)


class BlockedSetReq(Request):
    peer: Annotated[
        tuple[PeerRef, ...],
        arg(0, metavar="PEER", required=False, variadic=True, kind="peer", help="Who to block."),
    ] = ()
    unblock: Annotated[bool, opt("--unblock", help="Remove the block instead of adding it.")] = (
        False
    )
    stories: Annotated[bool, opt("--stories", help="Only block them from seeing my stories.")] = (
        False
    )
    replace_with: Annotated[
        str | None,
        opt("--replace-with", metavar="LIST", help="Replace the whole list at once."),
    ] = None


async def blocked_set(ctx: OpContext, req: BlockedSetReq) -> BlockedSet:
    """Block or unblock peers, or replace the whole blocklist.

    The answer is always the diff — who this call blocked and who it
    unblocked — because `--replace-with` is `contacts.setBlocked`, which
    *replaces* the list: everyone not named is unblocked. One shape for both
    paths means a script never has to branch on which flag it passed.
    """
    from telethon.tl.functions import contacts as fn

    if req.replace_with is not None:
        from tlgr.models.peer import parse_peer_ref
        from tlgr.ops.contact import BlockedSetReq as ContactBlockedSetReq
        from tlgr.ops.contact import blocked_set as contact_blocked_set

        refs = [
            parse_peer_ref(part.strip()) for part in req.replace_with.split(",") if part.strip()
        ]
        return await contact_blocked_set(ctx, ContactBlockedSetReq(user=refs, stories=req.stories))

    if not req.peer:
        raise UsageError("give one or more peers, or --replace-with", field="peer")

    handle = client(ctx)
    marked: list[int] = []
    for ref in req.peer:
        peer = await _settings.resolve(ctx, ref)
        request = fn.UnblockRequest if req.unblock else fn.BlockRequest
        await handle(request(id=peer, my_stories_from=req.stories or None))
        marked.append(_settings.peer_of(peer))
    ctx.emit("privacy_blocked", {"peer_ids": marked, "unblock": req.unblock})
    return BlockedSet(
        count=len(marked),
        blocked=[] if req.unblock else marked,
        unblocked=marked if req.unblock else [],
        kind="stories" if req.stories else "main",
        applied=True,
    )


SPEC_BLOCKED_SET = OperationSpec(
    id="privacy.blocked.set",
    request=BlockedSetReq,
    response=BlockedSet,
    impl=blocked_set,
    summary="Block or unblock a user or bot (also the story-only block list)",
    description=(
        "The same operation as `user block` / `user unblock`, reached from "
        "the Privacy screen. `--replace-with` is the bulk form and answers "
        "with the diff it applied."
    ),
    aliases=("block.set",),
    mutating=True,
    rate_class="send",
    columns=("count", "blocked", "unblocked", "kind"),
    headers=("Peers", "Blocked", "Unblocked", "List"),
    example={"count": 1, "blocked": [777123], "unblocked": [], "kind": "main", "applied": True},
    example_args="privacy blocked set @spammer",
    covers=("privacy.blocked-list",),
    tags=frozenset({"visible-to-others"}),
)


# ---------------------------------------------------------------------------
# privacy revenue get
# ---------------------------------------------------------------------------


class RevenueGetReq(Request):
    user: Annotated[PeerRef, arg(0, metavar="USER", kind="user", help="Who paid me.")]
    parent_peer: Annotated[
        str | None,
        opt("--parent-peer", metavar="CHAT", help="Business or channel parent peer."),
    ] = None


async def revenue_get(ctx: OpContext, req: RevenueGetReq) -> PaidMessageRevenue:
    """Stars earned from paid messages one user sent me."""
    from telethon.tl.functions import account as fn

    target = await _settings.input_user(ctx, req.user)
    parent = await _settings.resolve(ctx, req.parent_peer) if req.parent_peer else None
    result = await client(ctx)(fn.GetPaidMessagesRevenueRequest(user_id=target, parent_peer=parent))
    return PaidMessageRevenue(
        user_id=_settings.peer_of(await _settings.resolve(ctx, req.user)),
        stars_amount=int(getattr(result, "stars_amount", 0) or 0),
    )


SPEC_REVENUE_GET = OperationSpec(
    id="privacy.revenue.get",
    request=RevenueGetReq,
    response=PaidMessageRevenue,
    impl=revenue_get,
    summary="Stars earned from paid messages sent by a user",
    idempotent=True,
    columns=("user_id", "stars_amount"),
    headers=("User", "Stars"),
    example={"user_id": 777123, "stars_amount": 25},
    example_args="privacy revenue get @alice",
    covers=("privacy.paid-message-revenue",),
    tags=frozenset({"agent-safe"}),
)

__all__ = [name for name in dir() if name.startswith("SPEC_")]
