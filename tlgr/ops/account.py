"""The `account` group: the local alias registry, and everything Telegram's
Settings ▸ Privacy and Security screen can do.

Five sub-nouns, one theme — *who can act as me*:

* `account session *` is the Devices list: every authorization, what each may
  do, and how to end one.
* `account password *` is 2-step verification, including the SRP prompt every
  other sensitive operation in tlgr reuses.
* `account website *` is Telegram Login on the web, which is a **different**
  list from Devices and is the one people forget.
* `account passkey *` is read-only on purpose: the server only accepts the
  RP id `telegram.org`, so tlgr can audit passkeys but can never mint one.
* `account ttl/phone/email/delete` are the account-level switches that are
  easy to get wrong once and impossible to undo.

Two rules run through all of it. **The daemon owns the session file** — v1's
`account add` opened it from the CLI while the daemon might already hold it,
which is the two-writer situation that earns `AUTH_KEY_DUPLICATED` and gets
the authorization revoked. And **a secret never reaches argv**: every
password, token and api_hash arrives through `--x-env`, `--x-stdin` or
`--x-file`.
"""

from __future__ import annotations

import contextlib
import shutil
from datetime import timedelta
from typing import Annotated, Any

from tlgr.core.errors import (
    AccountNotFoundError,
    AuthenticationError,
    TlgrError,
    UsageError,
)
from tlgr.core.pagination import PageKind
from tlgr.core.paths import secure_session_files, validate_alias, write_private
from tlgr.core.timefmt import parse_duration
from tlgr.models.auth import (
    AccountDeletion,
    AccountRecord,
    AccountState,
    AccountTtl,
    DeviceLock,
    Passkey,
    PasswordReset,
    PasswordState,
    PhoneChange,
    RecoveryEmail,
    Session,
    SessionChange,
    SessionTermination,
    SmsJobs,
    Suggestion,
    SupportInfo,
    TempPassword,
    WebSession,
    WebSessionRevocation,
)
from tlgr.models.base import Request
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops import _auth, _send
from tlgr.ops._params import arg, choice, opt
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [
    "SPEC_ADD",
    "SPEC_CHECK",
    "SPEC_DELETE",
    "SPEC_DEVICE_LOCKED_SET",
    "SPEC_EMAIL_SET",
    "SPEC_EXPORT",
    "SPEC_IMPORT",
    "SPEC_INFO",
    "SPEC_LIST",
    "SPEC_LOGOUT",
    "SPEC_PASSKEY_DELETE",
    "SPEC_PASSKEY_LIST",
    "SPEC_PASSWORD_CHANGE",
    "SPEC_PASSWORD_GET",
    "SPEC_PASSWORD_REMOVE",
    "SPEC_PASSWORD_RESET",
    "SPEC_PASSWORD_SET",
    "SPEC_PASSWORD_TEMP",
    "SPEC_PHONE_SET",
    "SPEC_REMOVE",
    "SPEC_RENAME",
    "SPEC_SESSION_ACCEPT_QR",
    "SPEC_SESSION_CONFIRM",
    "SPEC_SESSION_LIST",
    "SPEC_SESSION_SET",
    "SPEC_SESSION_TERMINATE",
    "SPEC_SMSJOBS_SET",
    "SPEC_SUGGESTION_LIST",
    "SPEC_SUPPORT_GET",
    "SPEC_SWITCH",
    "SPEC_SYNC",
    "SPEC_TTL_GET",
    "SPEC_TTL_SET",
    "SPEC_WEBSITE_LIST",
    "SPEC_WEBSITE_REVOKE",
]

_PASSWORD = opt(
    secret=True, envvar="TLGR_2FA_PASSWORD", help="The 2FA cloud password (never in argv)."
)


# ---------------------------------------------------------------------------
# account list / switch / rename — local, and they work with no daemon
# ---------------------------------------------------------------------------


class ListReq(Request):
    pass


async def list_accounts(ctx: OpContext, req: ListReq) -> Page[AccountRecord]:
    """Every configured account, with the health the daemon last recorded.

    Health is read from `accounts.json` rather than from a live daemon, which
    is the point: you ask "is my account still working?" exactly when the
    daemon is *not* running, and v1 answered "unknown" then.
    """
    manager = _auth.accounts(ctx)
    active = manager.get_active()
    items = [
        AccountRecord(
            alias=info.alias,
            name=info.display_name(),
            user_id=info.user_id,
            username=info.username,
            phone=info.phone,
            active=info.alias == active,
            connected=info.health.state == "online",
            state=info.health.state,
            created_at=info.created_at,
        )
        for info in manager.list_accounts()
    ]
    return Page(items=items, has_more=False, total=len(items))


SPEC_LIST = OperationSpec(
    id="account.list",
    request=ListReq,
    response=Page[AccountRecord],
    impl=list_accounts,
    summary="List the accounts configured in this installation",
    aliases=("accounts",),
    legacy_paths=("account list",),
    needs_account=False,
    needs_auth=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    paginated=PageKind.LOCAL,
    columns=("alias", "user_id", "name", "phone", "active", "state"),
    headers=("Alias", "User ID", "Name", "Phone", "Active", "State"),
    example={
        "items": [
            {
                "alias": "work",
                "name": "@me",
                "user_id": 4242,
                "phone": "+989123456789",
                "active": True,
                "state": "online",
            }
        ],
        "has_more": False,
    },
    example_args="account list",
    covers=("auth.multi-account",),
    coverage_note="Adding is `account add`, switching is `account switch`.",
    tags=frozenset({"agent-safe", "infrastructure"}),
)


class SwitchReq(Request):
    alias: Annotated[str, arg(0, metavar="ACCOUNT", help="The alias to make default.")]


async def switch(ctx: OpContext, req: SwitchReq) -> AccountState:
    """Make another configured account the default for later commands."""
    manager = _auth.accounts(ctx)
    alias = validate_alias(req.alias)
    if manager.get_account(alias) is None:
        raise AccountNotFoundError(f"no account named {alias!r}. Run: tlgr account list")
    if manager.get_active() == alias:
        _auth.already(ctx)
        return AccountState(ok=True, account=alias, alias=alias, already=True)
    manager.set_active(alias)
    return AccountState(ok=True, account=alias, alias=alias)


SPEC_SWITCH = OperationSpec(
    id="account.switch",
    request=SwitchReq,
    response=AccountState,
    impl=switch,
    summary="Make another configured account the default for later commands",
    legacy_paths=("account switch",),
    mutating=True,
    idempotent=True,
    needs_account=False,
    needs_auth=False,
    surface=Surface.LOCAL,
    rate_class="local",
    columns=("ok", "account"),
    example={"ok": True, "account": "work"},
    example_args="account switch work",
    covers=(),
    tags=frozenset({"agent-safe", "infrastructure"}),
)


class RenameReq(Request):
    alias: Annotated[str, arg(0, metavar="ACCOUNT", help="The alias to rename.")]
    name: Annotated[str, arg(1, metavar="NAME", help="Its new local label.")]


async def rename(ctx: OpContext, req: RenameReq) -> AccountState:
    """Rename an account's local label. Nothing server-side changes."""
    manager = _auth.accounts(ctx)
    old, new = validate_alias(req.alias), validate_alias(req.name)
    if not manager.rename_account(old, new):
        raise AccountNotFoundError(f"no account named {old!r}. Run: tlgr account list")
    return AccountState(ok=True, account=new, alias=new, old=old, new=new)


SPEC_RENAME = OperationSpec(
    id="account.rename",
    request=RenameReq,
    response=AccountState,
    impl=rename,
    summary="Rename a configured account (its local label)",
    legacy_paths=("account rename",),
    mutating=True,
    needs_account=False,
    needs_auth=False,
    surface=Surface.LOCAL,
    rate_class="local",
    columns=("ok", "old", "new"),
    example={"ok": True, "account": "personal", "old": "work", "new": "personal"},
    example_args="account rename work personal",
    covers=(),
    tags=frozenset({"agent-safe", "infrastructure"}),
)


# ---------------------------------------------------------------------------
# account add / import / export / remove / logout / check
# ---------------------------------------------------------------------------


class AddReq(Request):
    phone: Annotated[
        str, arg(0, metavar="PHONE", required=False, help="Omit with --bot or --qr.")
    ] = ""
    alias: Annotated[str | None, opt("--alias", help="Local name for the account.")] = None
    bot: Annotated[bool, opt("--bot", help="Log in as a bot with a token instead.")] = False
    token: Annotated[
        str | None, opt(secret=True, envvar="TLGR_BOT_TOKEN", help="The bot token.")
    ] = None
    use_qr: Annotated[bool, opt("--qr", help="Use QR login instead of a phone code.")] = False
    api_id: Annotated[
        int | None, opt("--api-id", metavar="ID", help="api_id for this account.")
    ] = None
    api_hash: Annotated[
        str | None, opt(secret=True, envvar="TLGR_API_HASH", help="api_hash for this account.")
    ] = None
    test_dc: Annotated[bool, opt("--test-dc", help="Use the Telegram test DCs.")] = False


async def add(ctx: OpContext, req: AddReq) -> AccountState:
    """Add an account: a bot in one step, a user in two.

    A bot token is a complete credential, so `--bot` finishes here. A phone
    login cannot: somebody has to read a code. Rather than hold a process
    open on `input()` the way v1 did, this starts the login and returns the
    exact next command — which is what makes the same path work for a human
    and for an agent.

    The login runs on the daemon's pre-auth client, never on a second
    `TelegramClient` opened over the session file.
    """
    from telethon.tl.functions import auth as fn

    from tlgr.ops.auth import SendCodeReq, _credentials, _default_alias, send_code

    if req.use_qr:
        raise UsageError(
            "QR login streams tokens until one is approved, so it is its own command. "
            "Run: tlgr auth qr --alias <name>",
            field="qr",
        )
    service = _auth.preauth(ctx)
    manager = _auth.accounts(ctx)

    if req.bot or req.token:
        if not req.token:
            raise UsageError(
                "--bot needs a token: --token-env TLGR_BOT_TOKEN, --token-stdin or --token-file",
                field="token",
            )
        alias = validate_alias(req.alias or f"bot{req.token.split(':', 1)[0]}")
        api_id, api_hash = _credentials(ctx, alias, req.api_id, req.api_hash)
        if manager.get_account(alias) is None:
            manager.add_account(alias)
        manager.save_credentials(api_id, api_hash, alias)
        client = await service.client_for(alias, api_id=api_id, api_hash=api_hash)
        await client(
            fn.ImportBotAuthorizationRequest(
                flags=0, api_id=api_id, api_hash=api_hash, bot_auth_token=req.token
            )
        )
        finished = await service.finish(alias)
        _mark_kind(manager, alias, "bot")
        return AccountState(
            alias=alias,
            account=alias,
            ok=True,
            authorized=True,
            kind="bot",
            user_id=finished.get("user_id"),
            username=finished.get("username"),
            test_dc=req.test_dc,
        )

    if not req.phone:
        raise UsageError("give a phone number, or --bot with a token", field="phone")
    sent = await send_code(
        ctx,
        SendCodeReq(
            phone=req.phone,
            alias=req.alias or _default_alias(req.phone),
            api_id=req.api_id,
            api_hash=req.api_hash,
            test_dc=req.test_dc,
        ),
    )
    alias = sent.account
    if sent.already:
        return AccountState(alias=alias, account=alias, ok=True, authorized=True, kind="user")
    return AccountState(
        alias=alias,
        account=alias,
        ok=True,
        phone=sent.phone,
        kind="user",
        test_dc=req.test_dc,
        hint=(
            f"a {sent.type} code was sent. Finish with: "
            f"tlgr auth verify-code <code> --alias {alias}"
        ),
    )


def _mark_kind(manager: Any, alias: str, kind: str) -> None:
    """Record `kind=bot` so user-only commands can fail fast rather than at the RPC."""
    path = manager.paths.account_dir(alias) / "kind"
    write_private(path, kind)


def _kind_of(manager: Any, alias: str) -> str:
    path = manager.paths.account_dir(alias) / "kind"
    with contextlib.suppress(OSError):
        return path.read_text(encoding="utf-8").strip() or "user"
    return "user"


SPEC_ADD = OperationSpec(
    id="account.add",
    request=AddReq,
    response=AccountState,
    impl=add,
    summary="Add and authenticate an account (phone, bot token, or QR)",
    description=(
        "A bot token finishes in one call. A phone login starts here and "
        "finishes with `auth verify-code`, because somebody has to read a "
        "code and a daemon cannot prompt. `kind=bot` is recorded so that "
        "user-only commands fail fast."
    ),
    aliases=("login",),
    legacy_paths=("account add", "login"),
    mutating=True,
    needs_account=False,
    needs_auth=False,
    rate_class="resolve",
    columns=("alias", "user_id", "username", "kind"),
    example={"alias": "work", "user_id": 4242, "username": "me", "kind": "user", "ok": True},
    example_args="account add +989123456789",
    covers=("auth.bot-token-login", "bots.login-as-bot"),
    covers_partial=(
        "auth.api-credentials",
        "auth.device-identity",
        "auth.multi-account",
        "auth.qr-login-generate",
        "auth.test-dc-login",
    ),
    coverage_note=(
        "The wrapper; the owning commands are `auth send-code` for codes and `auth qr` for QR."
    ),
)


class ImportReq(Request):
    source: Annotated[
        str,
        arg(0, metavar="SOURCE", kind="path", help="A .session file, or '-' for a StringSession."),
    ]
    alias: Annotated[str | None, opt("--alias", help="Local name for the account.")] = None
    string: Annotated[bool, opt("--string", help="Treat the input as a StringSession.")] = False
    api_id: Annotated[
        int | None, opt("--api-id", metavar="ID", help="api_id the session was made with.")
    ] = None
    api_hash: Annotated[
        str | None, opt(secret=True, envvar="TLGR_API_HASH", help="The matching api_hash.")
    ] = None


async def import_session(ctx: OpContext, req: ImportReq) -> AccountState:
    """Import an existing Telethon session as a tlgr account.

    Stop the source client first. One auth key used by two live connections
    is `AUTH_KEY_DUPLICATED`, and Telegram's response to that is to revoke
    the key — so a careless import kills the session it was importing.
    """
    import sys

    from telethon.sessions import SQLiteSession, StringSession

    from tlgr.ops.auth import _credentials

    manager = _auth.accounts(ctx)
    alias = validate_alias(req.alias or "imported")
    if manager.get_account(alias) is not None:
        raise TlgrError(f"account {alias!r} already exists; pass --alias for a different name")
    api_id, api_hash = _credentials(ctx, alias, req.api_id, req.api_hash)

    manager.add_account(alias)
    manager.save_credentials(api_id, api_hash, alias)
    destination = manager.paths.session_file(alias)
    try:
        if req.string or req.source == "-":
            text = (sys.stdin.read() if req.source == "-" else req.source).strip()
            source = StringSession(text)
            target = SQLiteSession(str(manager.paths.session(alias)))
            target.set_dc(source.dc_id, source.server_address, source.port)
            target.auth_key = source.auth_key
            target.save()
        else:
            shutil.copy2(req.source, destination)
        secure_session_files(manager.paths.session(alias))
        session = await _auth.sessions(ctx).ensure(alias)
        client = await session.acquire(timeout=60)
        if not await client.is_user_authorized():
            raise AuthenticationError("the imported session is not authorized")
        me = await client.get_me()
    except Exception:
        manager.remove_account(alias)
        raise
    manager.update_account(
        alias,
        phone=getattr(me, "phone", None),
        username=getattr(me, "username", None),
        first_name=getattr(me, "first_name", None),
        user_id=getattr(me, "id", None),
    )
    return AccountState(
        alias=alias,
        account=alias,
        user_id=getattr(me, "id", None),
        username=getattr(me, "username", None),
        authorized=True,
        imported=True,
        ok=True,
    )


SPEC_IMPORT = OperationSpec(
    id="account.import",
    request=ImportReq,
    response=AccountState,
    impl=import_session,
    summary="Import an existing Telethon session (file or StringSession)",
    legacy_paths=("account import",),
    mutating=True,
    needs_account=False,
    needs_auth=False,
    rate_class="resolve",
    columns=("alias", "user_id", "username", "authorized"),
    example={"alias": "work", "user_id": 4242, "username": "me", "authorized": True},
    example_args="account import ./work.session --alias work",
    covers=("auth.session-import",),
)


class ExportReq(Request):
    alias: Annotated[
        str | None, arg(0, metavar="ACCOUNT", required=False, help="Which account.")
    ] = None
    format: Annotated[str | None, choice("string", "file", help="Output form.")] = "string"
    out: Annotated[
        str | None, opt("--out", metavar="PATH", kind="path", help="Write here at 0600.")
    ] = None
    stdout: Annotated[
        bool, opt("--stdout", help="Print the credential to stdout — it is a bearer token.")
    ] = False


async def export(ctx: OpContext, req: ExportReq) -> AccountState:
    """Export an authorization as a StringSession or a session file copy.

    The exported value **is** the account: anyone holding it can act as you
    until the session is terminated. It therefore goes to a 0600 file unless
    `--stdout` says out loud that printing it is intended. Using the same
    auth key from two live connections earns `AUTH_KEY_DUPLICATED`, which is
    why tlgr routes everything through one daemon.
    """
    from pathlib import Path

    from telethon.sessions import StringSession

    if not req.stdout and not req.out:
        raise UsageError(
            "an exported session is a bearer credential: write it with --out PATH (0600), "
            "or pass --stdout to print it deliberately",
            field="out",
        )
    manager = _auth.accounts(ctx)
    alias = _auth.resolve_alias(ctx, req.alias)
    ctx.warn(
        "this is a full authorization; anyone holding it can act as this account until "
        "the session is terminated"
    )
    if req.format == "file":
        source = manager.paths.session_file(alias)
        if not req.out:
            raise UsageError("--format file needs --out PATH", field="out")
        shutil.copy2(source, req.out)
        Path(req.out).chmod(0o600)
        return AccountState(alias=alias, account=alias, format="file", path=str(req.out), ok=True)

    text = StringSession.save(_auth.client(ctx).session)
    if req.out:
        write_private(Path(req.out), text)
        return AccountState(alias=alias, account=alias, format="string", path=str(req.out), ok=True)
    return AccountState(alias=alias, account=alias, format="string", session=text, ok=True)


SPEC_EXPORT = OperationSpec(
    id="account.export",
    request=ExportReq,
    response=AccountState,
    impl=export,
    summary="Export an account's authorization as a session file or StringSession",
    mutating=False,
    rate_class="local",
    columns=("alias", "format", "path"),
    example={"alias": "work", "format": "string", "path": "/home/me/work.session"},
    example_args="account export work --out ./work.string",
    covers=("auth.session-export", "auth.single-connection-guard"),
    tags=frozenset({"redact"}),
)


class LogoutReq(Request):
    alias: Annotated[
        str | None, arg(0, metavar="ACCOUNT", required=False, help="Which account.")
    ] = None
    keep_local: Annotated[
        bool, opt("--keep-local", help="Keep the (now invalid) session file.")
    ] = False
    keep_token: Annotated[
        bool,
        opt(
            "--keep-token/--no-keep-token",
            help="Keep the future_auth_token for a code-less re-login.",
        ),
    ] = True


async def logout(ctx: OpContext, req: LogoutReq) -> AccountState:
    """Revoke the authorization on the server, and drop the dead session file.

    This is the gap v1 had: `account remove` deleted the local files and
    never called `auth.logOut`, so the authorization stayed alive in every
    other client's Devices list forever. Logging out is *not* removing the
    account — the alias, its credentials and its future auth token stay, so
    `tlgr auth send-code` can log back in.
    """
    from telethon.tl.functions import auth as fn

    manager = _auth.accounts(ctx)
    alias = _auth.resolve_alias(ctx, req.alias)
    sessions = _auth.sessions(ctx)
    session = await sessions.ensure(alias)
    client = await session.acquire(timeout=60)
    answer = await client(fn.LogOutRequest())
    stored = False
    if req.keep_token:
        stored = _auth.store_future_token(
            manager, alias, getattr(answer, "future_auth_token", None)
        )
    await sessions.release(alias)
    if not req.keep_local:
        for path in (manager.paths.session_file(alias), manager.paths.session(alias)):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
    manager.set_health(alias, "needs_login", reason="logged out")
    _auth.emit(ctx, "logged_out", {"alias": alias})
    return AccountState(
        alias=alias,
        account=alias,
        ok=True,
        logged_out=True,
        future_auth_token_stored=stored,
        hint=(
            "the alias is still configured; log back in with tlgr auth send-code, "
            "or delete it entirely with tlgr account remove"
        ),
    )


SPEC_LOGOUT = OperationSpec(
    id="account.logout",
    request=LogoutReq,
    response=AccountState,
    impl=logout,
    summary="Log out: revoke the authorization on the server and drop the session",
    description=(
        "v1's `account remove` never called `auth.logOut`, so a removed "
        "account went on showing up in every other client's Devices list. "
        "The returned `future_auth_token` is a bearer secret: 0600 next to "
        "the session, capped at 20, dropped by `account remove`."
    ),
    aliases=("logout",),
    legacy_paths=("logout",),
    mutating=True,
    destructive=True,
    needs_account=False,
    needs_auth=False,
    rate_class="resolve",
    columns=("alias", "logged_out", "future_auth_token_stored"),
    example={"alias": "work", "logged_out": True, "future_auth_token_stored": True},
    example_args="account logout work",
    covers=("auth.log-out",),
    covers_partial=(
        "auth.clear-local-data",
        "auth.future-auth-tokens",
        "auth.logout-alternatives",
    ),
    coverage_note=(
        "Wiping the alias is `account remove`; the token is minted by "
        "`auth send-code`; the support alternative is `account support get`."
    ),
)


class RemoveReq(Request):
    alias: Annotated[str, arg(0, metavar="ACCOUNT", help="Which account.")]
    server_logout: Annotated[
        bool,
        opt("--logout/--no-server-logout", help="Also revoke the authorization on the server."),
    ] = False


async def remove(ctx: OpContext, req: RemoveReq) -> AccountState:
    """Remove an account from this machine. Local only unless `--logout`.

    v1's behaviour is preserved and now explicit: without `--logout` the
    authorization keeps showing up in every other client's Devices list, and
    the answer says so instead of leaving you to find out.
    """
    manager = _auth.accounts(ctx)
    alias = validate_alias(req.alias)
    if manager.get_account(alias) is None:
        raise AccountNotFoundError(f"no account named {alias!r}. Run: tlgr account list")
    logged_out = False
    if req.server_logout:
        await logout(ctx, LogoutReq(alias=alias, keep_token=False))
        logged_out = True
    else:
        sessions = getattr(getattr(ctx, "daemon", None), "sessions", None)
        if sessions is not None:
            with contextlib.suppress(Exception):
                await sessions.release(alias)
    manager.remove_account(alias)
    return AccountState(
        alias=alias,
        account=alias,
        ok=True,
        removed=True,
        server_logout=logged_out,
        hint=(
            None
            if logged_out
            else "the server-side authorization is still alive and still listed in other "
            "clients' Devices. Pass --logout to revoke it."
        ),
    )


SPEC_REMOVE = OperationSpec(
    id="account.remove",
    request=RemoveReq,
    response=AccountState,
    impl=remove,
    summary="Remove an account from this machine (local only unless --logout)",
    legacy_paths=("account remove",),
    mutating=True,
    destructive=True,
    needs_account=False,
    needs_auth=False,
    rate_class="local",
    columns=("alias", "removed", "server_logout"),
    example={"alias": "work", "removed": True, "server_logout": False},
    example_args="account remove work",
    covers=("auth.clear-local-data",),
)


class CheckReq(Request):
    alias: Annotated[
        str | None,
        arg(0, metavar="ACCOUNT", required=False, help="One account; omit to check every one."),
    ] = None


async def check(ctx: OpContext, req: CheckReq) -> Page[AccountState]:
    """Is the stored authorization still good — and if not, why not?

    The distinction `daemon status` cannot make: "the network is down" and
    "Telegram revoked this auth key" both look like a disconnected client,
    and only one of them is fixed by waiting. `revoked` is exit 4 material,
    `banned`/`frozen` is exit 9, and `offline` is exit 8 — but this command
    *reports* rather than raises, because `--all` has to answer for every
    account even when one of them is dead.
    """
    manager = _auth.accounts(ctx)
    aliases = [validate_alias(req.alias)] if req.alias else manager.aliases()
    sessions = _auth.sessions(ctx)
    items: list[AccountState] = []
    for alias in aliases:
        items.append(await _check_one(ctx, manager, sessions, alias))
    return Page(items=items, has_more=False, total=len(items))


async def _check_one(ctx: OpContext, manager: Any, sessions: Any, alias: str) -> AccountState:
    from tlgr.core.errors import rule_for

    state = AccountState(alias=alias, account=alias)
    try:
        session = await sessions.ensure(alias)
        client = await session.acquire(timeout=60)
        if not await client.is_user_authorized():
            state.state = "revoked"
            state.hint = "run: tlgr auth send-code <phone> to log in again"
            return state
        me = await client.get_me()
        state.state = "authorized"
        state.user_id = getattr(me, "id", None)
        state.username = getattr(me, "username", None)
        return state
    except Exception as exc:
        rule = rule_for(exc)
        message = str(exc)
        state.error = message
        if "FROZEN" in message:
            state.state = "frozen"
            config = {}
            with contextlib.suppress(Exception):
                config = await _auth.app_config(await sessions.get(alias).acquire(timeout=10))
            state.frozen_since = _auth.iso(_unix(config.get("freeze_since_date")))
            state.frozen_until = _auth.iso(_unix(config.get("freeze_until_date")))
            state.appeal_url = str(config.get("freeze_appeal_url") or "") or None
            state.hint = "appeal at the URL above; the form is a web page a CLI cannot submit"
        elif "USER_DEACTIVATED_BAN" in message:
            state.state = "banned"
            state.hint = "write to recover@telegram.org — this is not something a client can undo"
        elif "USER_DEACTIVATED" in message:
            state.state = "deactivated"
            state.hint = "write to recover@telegram.org"
        elif rule.code in ("SESSION_ERROR", "AUTH_ERROR"):
            state.state = "revoked"
            state.hint = "run: tlgr auth send-code <phone> to log in again"
        else:
            state.state = "offline"
            state.hint = "the account could not be reached; this is not a statement about it"
        return state


def _unix(value: Any) -> Any:
    from datetime import datetime, timezone

    if isinstance(value, (int, float)) and value:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    return None


SPEC_CHECK = OperationSpec(
    id="account.check",
    request=CheckReq,
    response=Page[AccountState],
    impl=check,
    summary="Health-check the stored authorization: authorized, revoked, banned or frozen",
    description=(
        "Reports rather than raises, so one dead account cannot hide the "
        "health of the others. `frozen` carries Telegram's own appeal URL "
        "from `help.getAppConfig`; the appeal itself is a web form."
    ),
    paginated=PageKind.LOCAL,
    needs_account=False,
    needs_auth=False,
    rate_class="read",
    columns=("alias", "state", "user_id", "hint"),
    headers=("Alias", "State", "User ID", "What to do"),
    example={"items": [{"alias": "work", "state": "authorized", "user_id": 4242}]},
    example_args="account check",
    covers=(
        "account.deactivated-banned",
        "account.frozen-appeal",
        "account.frozen-state",
        "auth.session-health",
    ),
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# account info / sync
# ---------------------------------------------------------------------------


class InfoReq(Request):
    alias: Annotated[
        str | None, arg(0, metavar="ACCOUNT", required=False, help="Which account.")
    ] = None


async def info(ctx: OpContext, req: InfoReq) -> AccountState:
    """The active account: who it is, which DC it lives on, where its session is."""
    manager = _auth.accounts(ctx)
    alias = _auth.resolve_alias(ctx, req.alias)
    client = _auth.client(ctx)
    me = await client.get_me()
    record = manager.get_account(alias)
    return AccountState(
        alias=alias,
        account=alias,
        user_id=getattr(me, "id", None),
        username=getattr(me, "username", None),
        first_name=getattr(me, "first_name", None),
        phone=getattr(me, "phone", None),
        premium=bool(getattr(me, "premium", False)),
        dc_id=getattr(getattr(client, "session", None), "dc_id", None),
        kind="bot" if getattr(me, "bot", False) else _kind_of(manager, alias),
        session_path=str(manager.paths.session_file(alias)),
        created_at=record.created_at if record else None,
        state=record.health.state if record else "unknown",
    )


SPEC_INFO = OperationSpec(
    id="account.info",
    request=InfoReq,
    response=AccountState,
    impl=info,
    summary="Show the active account: user, phone, dc, session file and state",
    legacy_paths=("account info",),
    rate_class="read",
    columns=("alias", "user_id", "username", "phone", "dc_id", "premium"),
    example={
        "alias": "work",
        "user_id": 4242,
        "username": "me",
        "phone": "+989123456789",
        "dc_id": 4,
        "premium": False,
    },
    example_args="account info",
    covers=("dialogs.frozen-account",),
    coverage_note="The frozen state itself is established by `account check`.",
    tags=frozenset({"agent-safe"}),
)


class SyncReq(Request):
    full: Annotated[bool, opt("--full", help="Re-walk every dialog, not an incremental pass.")] = (
        False
    )


async def sync(ctx: OpContext, req: SyncReq) -> AccountState:
    """Refresh the cached entities, dialogs and update state for an account."""
    from telethon.tl.functions import updates as fn

    client = _auth.client(ctx)
    manager = _auth.accounts(ctx)
    alias = _auth.resolve_alias(ctx)
    dialogs = 0
    users = 0
    chats = 0
    async for dialog in client.iter_dialogs(limit=None if req.full else 200):
        dialogs += 1
        entity = getattr(dialog, "entity", None)
        if type(entity).__name__ == "User":
            users += 1
        elif entity is not None:
            chats += 1
    state = await client(fn.GetStateRequest())
    me = await client.get_me()
    manager.update_account(
        alias,
        phone=getattr(me, "phone", None),
        username=getattr(me, "username", None),
        first_name=getattr(me, "first_name", None),
        user_id=getattr(me, "id", None),
    )
    return AccountState(
        alias=alias,
        account=alias,
        ok=True,
        dialogs=dialogs,
        users=users,
        chats=chats,
        pts=getattr(state, "pts", None),
        user_id=getattr(me, "id", None),
        username=getattr(me, "username", None),
    )


SPEC_SYNC = OperationSpec(
    id="account.sync",
    request=SyncReq,
    response=AccountState,
    impl=sync,
    summary="Refresh cached entities, dialogs and update state for an account",
    legacy_paths=("account sync",),
    mutating=True,
    idempotent=True,
    rate_class="bulk",
    timeout_s=300,
    columns=("ok", "dialogs", "users", "chats", "pts"),
    example={"ok": True, "dialogs": 42, "users": 30, "chats": 12, "pts": 90210},
    example_args="account sync",
    covers=(),
    tags=frozenset({"agent-safe", "infrastructure"}),
)


# ---------------------------------------------------------------------------
# account session * — the Devices list
# ---------------------------------------------------------------------------


class SessionListReq(Request):
    hash: Annotated[
        str | None,
        arg(0, metavar="HASH", required=False, help="One session hash, or 'current'."),
    ] = None
    unconfirmed: Annotated[
        bool, opt("--unconfirmed", help="Only sessions still awaiting 'Yes, it's me'.")
    ] = False
    pending_password: Annotated[
        bool, opt("--pending-password", help="Only incomplete logins (password_pending).")
    ] = False
    bots: Annotated[bool, opt("--bots", help="Also list connected business bots.")] = False


async def session_list(ctx: OpContext, req: SessionListReq) -> Page[Session]:
    """List active sessions (Devices), or show one in detail.

    `hash` is the id every per-session action takes. Two fields are derived
    rather than left to the reader: `deny_deadline`, because an unconfirmed
    login stops being deniable once Telegram auto-confirms it, so a security
    cron that runs less often than `authorization_autoconfirm_period` will
    never see one; and `sensitive_actions_eligible_at`, which is what
    `SESSION_TOO_FRESH_X` counts down to before an ownership transfer.
    """
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    answer = await client(fn.GetAuthorizationsRequest())
    config = await _auth.app_config(client)
    period = int(config.get("authorization_autoconfirm_period") or 0)
    ttl = getattr(answer, "authorization_ttl_days", None)
    items = [
        _auth.session_model(auth, ttl_days=ttl, autoconfirm_period=period)
        for auth in getattr(answer, "authorizations", None) or []
    ]
    if req.hash:
        wanted = req.hash.strip().lower()
        items = [
            item
            for item in items
            if (item.current if wanted == "current" else item.hash == req.hash.strip())
        ]
    if req.unconfirmed:
        items = [item for item in items if item.unconfirmed]
    if req.pending_password:
        items = [item for item in items if item.password_pending]
    if req.bots:
        items.extend(await _connected_bots(ctx, client))
    return Page(items=items, has_more=False, total=len(items))


async def _connected_bots(ctx: OpContext, client: Any) -> list[Session]:
    """Business bots, which official clients show in the same Devices list.

    Premium/business only, so a failure here is a warning rather than an
    error: the Devices list is still the answer to the question that was
    asked.
    """
    from telethon.tl.functions import account as fn

    try:
        answer = await client(fn.GetConnectedBotsRequest())
    except Exception as exc:
        ctx.warn(f"connected business bots are unavailable on this account: {exc}")
        return []
    users = {user.id: user for user in getattr(answer, "users", None) or []}
    return [
        Session(
            hash=str(getattr(bot, "bot_id", "")),
            bot=True,
            bot_username=getattr(users.get(getattr(bot, "bot_id", 0)), "username", None),
            device_model=getattr(bot, "device", "") or "",
            app_name="business bot",
            date_created=_auth.iso(getattr(bot, "date", None)),
        )
        for bot in getattr(answer, "connected_bots", None) or []
    ]


SPEC_SESSION_LIST = OperationSpec(
    id="account.session.list",
    request=SessionListReq,
    response=Page[Session],
    impl=session_list,
    summary="List active sessions (Devices), or show one in detail",
    aliases=("session.list", "sessions.list"),
    paginated=PageKind.LOCAL,
    rate_class="read",
    columns=("hash", "device_model", "app_name", "ip", "country", "date_active", "current"),
    headers=("Hash", "Device", "App", "IP", "Country", "Last active", "This one"),
    example={
        "items": [
            {
                "hash": "0",
                "current": True,
                "device_model": "tlgr@host",
                "app_name": "tlgr",
                "ip": "203.0.113.7",
                "country": "NL",
                "date_active": "2026-09-03T09:14:07Z",
            }
        ]
    },
    example_args="account session list",
    covers=(
        "auth.session-age-requirement",
        "sessions.autoconfirm-period",
        "sessions.business-bot-entry",
        "sessions.incomplete-login-attempts",
        "sessions.list",
        "sessions.new-login-alert",
        "sessions.show",
    ),
    tags=frozenset({"agent-safe"}),
)


class SessionTerminateReq(Request):
    hash: Annotated[
        tuple[str, ...],
        arg(0, metavar="HASH", required=False, variadic=True, help="Sessions to terminate."),
    ] = ()
    all_others: Annotated[
        bool, opt("--all-others", help="Log out every session except this one.")
    ] = False
    deny: Annotated[
        bool, opt("--deny", help="'It wasn't me': terminate and print the password advice.")
    ] = False


async def session_terminate(ctx: OpContext, req: SessionTerminateReq) -> SessionTermination:
    """Terminate one session, several, or every session but this one.

    The current session cannot be terminated this way — that is `account
    logout`. `FRESH_RESET_AUTHORISATION_FORBIDDEN` means *this* session is
    younger than 24 hours; the wait is Telegram's, not tlgr's.
    """
    from telethon.tl.functions import account as fn
    from telethon.tl.functions import auth as auth_fn

    client = _auth.client(ctx)
    if req.all_others:
        await client(auth_fn.ResetAuthorizationsRequest())
        ctx.emit("sessions_terminated", {"all_others": True})
        return SessionTermination(
            terminated=-1,
            advice="every other session was logged out; the server reports no count",
        )
    if not req.hash:
        raise UsageError("give one or more session hashes, or --all-others", field="hash")
    done: list[str] = []
    for value in req.hash:
        await client(fn.ResetAuthorizationRequest(hash=_auth.parse_hash(value)))
        done.append(value)
    ctx.emit("sessions_terminated", {"hashes": done})
    return SessionTermination(
        terminated=len(done),
        hashes=done,
        advice=(
            "if that login was not you, change the cloud password too: tlgr account password change"
            if req.deny
            else None
        ),
    )


SPEC_SESSION_TERMINATE = OperationSpec(
    id="account.session.terminate",
    request=SessionTerminateReq,
    response=SessionTermination,
    impl=session_terminate,
    summary="Terminate one session, several, or every session but this one",
    aliases=("session.terminate", "sessions.terminate"),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("terminated", "hashes"),
    example={"terminated": 1, "hashes": ["9021045"]},
    example_args="account session terminate 9021045",
    covers=("sessions.deny-new-login", "sessions.terminate", "sessions.terminate-all-others"),
)


class SessionSetReq(Request):
    hash: Annotated[
        str | None,
        arg(0, metavar="HASH", required=False, help="Omit to change account-wide settings only."),
    ] = None
    calls: Annotated[
        str | None, choice("on", "off", help="Let this session accept incoming calls.")
    ] = None
    secret_chats: Annotated[
        str | None, choice("on", "off", help="Let this session accept secret chats.")
    ] = None
    auto_terminate: Annotated[
        str | None,
        opt(
            "--auto-terminate",
            metavar="DURATION",
            help="Account-wide: terminate sessions inactive this long (1-366 days).",
        ),
    ] = None


async def session_set(ctx: OpContext, req: SessionSetReq) -> SessionChange:
    """Per-session permissions, or the account-wide inactive-session TTL.

    The TTL is account-wide, so it is a flag on this command rather than a
    fourth path level; `account session list` reports the current value on
    every row.
    """
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    change = SessionChange(hash=req.hash or "")
    if req.auto_terminate:
        days = _days(req.auto_terminate, low=1, high=366, field="auto_terminate")
        await client(fn.SetAuthorizationTTLRequest(authorization_ttl_days=days))
        change.authorization_ttl_days = days
    if req.calls is None and req.secret_chats is None:
        if req.auto_terminate is None:
            raise UsageError(
                "nothing to change: pass --calls, --secret-chats or --auto-terminate",
                field="calls",
            )
        return change
    if not req.hash:
        raise UsageError("--calls and --secret-chats need a session hash", field="hash")
    calls_disabled = None if req.calls is None else req.calls == "off"
    secret_disabled = None if req.secret_chats is None else req.secret_chats == "off"
    await client(
        fn.ChangeAuthorizationSettingsRequest(
            hash=_auth.parse_hash(req.hash),
            call_requests_disabled=calls_disabled,
            encrypted_requests_disabled=secret_disabled,
        )
    )
    change.call_requests_disabled = calls_disabled
    change.encrypted_requests_disabled = secret_disabled
    return change


def _days(value: str, *, low: int, high: int, field: str) -> int:
    """A duration or a month shorthand as a whole number of days, range-checked."""
    text = value.strip().lower()
    months = {"1m": 30, "3m": 90, "6m": 180, "12m": 365, "18m": 548, "24m": 730}
    if text in months:
        days = months[text]
    elif text.endswith("y") and text[:-1].isdigit():
        days = int(text[:-1]) * 365
    else:
        seconds = parse_duration(text)
        if seconds is None:
            raise UsageError(f"{value!r} is not a duration (try 90d, 6m or 1y)", field=field)
        days = max(1, round(seconds / 86400))
    if not low <= days <= high:
        raise UsageError(f"{days} days is outside the allowed {low}-{high}", field=field)
    return days


SPEC_SESSION_SET = OperationSpec(
    id="account.session.set",
    request=SessionSetReq,
    response=SessionChange,
    impl=session_set,
    summary="Change per-session permissions, or the account-wide inactive-session TTL",
    aliases=("session.set", "sessions.set"),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("hash", "call_requests_disabled", "authorization_ttl_days"),
    example={"hash": "9021045", "call_requests_disabled": True},
    example_args="account session set 9021045 --calls off",
    covers=(
        "calls.session-accept-calls",
        "sessions.accept-calls",
        "sessions.accept-secret-chats",
        "sessions.auto-terminate-ttl",
    ),
)


class SessionConfirmReq(Request):
    hash: Annotated[str, arg(0, metavar="HASH", help="The unconfirmed session.")]


async def session_confirm(ctx: OpContext, req: SessionConfirmReq) -> SessionChange:
    """Confirm an unconfirmed new login — the 'Yes, it's me' button.

    Only meaningful inside `authorization_autoconfirm_period`; afterwards
    Telegram has confirmed it for you and this reports `already`.
    """
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    answer = await client(fn.GetAuthorizationsRequest())
    match = [
        auth
        for auth in getattr(answer, "authorizations", None) or []
        if str(getattr(auth, "hash", "")) == req.hash.strip()
    ]
    if match and not getattr(match[0], "unconfirmed", False):
        _auth.already(ctx)
        return SessionChange(hash=req.hash, confirmed=True, already=True)
    await client(
        fn.ChangeAuthorizationSettingsRequest(hash=_auth.parse_hash(req.hash), confirmed=True)
    )
    return SessionChange(hash=req.hash, confirmed=True)


SPEC_SESSION_CONFIRM = OperationSpec(
    id="account.session.confirm",
    request=SessionConfirmReq,
    response=SessionChange,
    impl=session_confirm,
    summary="Confirm an unconfirmed new login ('Yes, it's me')",
    aliases=("session.confirm", "sessions.confirm"),
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("hash", "confirmed", "already"),
    example={"hash": "9021045", "confirmed": True},
    example_args="account session confirm 9021045",
    covers=("sessions.confirm-new-login",),
)


class SessionAcceptQrReq(Request):
    link: Annotated[
        str, arg(0, metavar="LINK", help="tg://login?token=… (paste it, or pipe from zbarimg).")
    ]


async def session_accept_qr(ctx: OpContext, req: SessionAcceptQrReq) -> SessionChange:
    """Approve another device's QR login from this account.

    A CLI has no camera, so the token is pasted (or piped from `zbarimg`).
    The authorization that was just created is printed back, because "I
    approved something" is not a useful answer to "what did I approve".
    """
    from telethon.tl.functions import auth as fn

    client = _auth.client(ctx)
    created = await client(fn.AcceptLoginTokenRequest(token=_auth.unb64(req.link)))
    ctx.emit("session_created", {"hash": str(getattr(created, "hash", ""))})
    return SessionChange(
        hash=str(getattr(created, "hash", "")),
        device_model=getattr(created, "device_model", None),
        app_name=getattr(created, "app_name", None),
        ip=getattr(created, "ip", None),
        country=getattr(created, "country", None),
    )


SPEC_SESSION_ACCEPT_QR = OperationSpec(
    id="account.session.accept-qr",
    request=SessionAcceptQrReq,
    response=SessionChange,
    impl=session_accept_qr,
    summary="Approve another device's QR login from this account",
    aliases=("session.accept-qr", "sessions.accept-qr"),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("hash", "device_model", "app_name", "ip", "country"),
    example={"hash": "9021045", "device_model": "Desktop", "app_name": "Telegram Desktop"},
    example_args="account session accept-qr tg://login?token=AQI",
    covers=("auth.qr-login-accept",),
)


# ---------------------------------------------------------------------------
# account website * — Telegram Login on the web
# ---------------------------------------------------------------------------


class WebsiteListReq(Request):
    pass


async def website_list(ctx: OpContext, req: WebsiteListReq) -> Page[WebSession]:
    """Websites and bots you are logged into with Telegram Login.

    A different list from Devices, and the one people forget: terminating
    every session does not disconnect a single website.
    """
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    answer = await client(fn.GetWebAuthorizationsRequest())
    users = {user.id: user for user in getattr(answer, "users", None) or []}
    items = [
        _auth.web_session_model(auth, users)
        for auth in getattr(answer, "authorizations", None) or []
    ]
    return Page(items=items, has_more=False, total=len(items))


SPEC_WEBSITE_LIST = OperationSpec(
    id="account.website.list",
    request=WebsiteListReq,
    response=Page[WebSession],
    impl=website_list,
    summary="Websites and bots you are logged into with Telegram Login",
    aliases=("websites.list", "privacy.website.list"),
    paginated=PageKind.LOCAL,
    rate_class="read",
    columns=("hash", "domain", "bot_username", "browser", "ip", "date_active"),
    headers=("Hash", "Domain", "Bot", "Browser", "IP", "Last active"),
    example={
        "items": [
            {
                "hash": "770",
                "domain": "example.com",
                "browser": "Firefox",
                "ip": "203.0.113.7",
                "date_active": "2026-09-03T09:14:07Z",
            }
        ]
    },
    example_args="account website list",
    covers=("privacy.connected-websites", "websites.list"),
    tags=frozenset({"agent-safe"}),
)


class WebsiteRevokeReq(Request):
    hash: Annotated[
        tuple[str, ...],
        arg(0, metavar="HASH", required=False, variadic=True, help="Websites to disconnect."),
    ] = ()
    every: Annotated[bool, opt("--all", help="Disconnect every website.")] = False
    block_bot: Annotated[bool, opt("--block-bot", help="Also block the bot behind the login.")] = (
        False
    )


async def website_revoke(ctx: OpContext, req: WebsiteRevokeReq) -> WebSessionRevocation:
    """Disconnect one website, or all of them. Irreversible either way."""
    from telethon.tl.functions import account as fn
    from telethon.tl.functions import contacts as contacts_fn

    client = _auth.client(ctx)
    if req.every:
        await client(fn.ResetWebAuthorizationsRequest())
        return WebSessionRevocation(revoked=-1)
    if not req.hash:
        raise UsageError("give one or more website hashes, or --all", field="hash")

    blocked: list[int] = []
    bots: dict[str, int] = {}
    if req.block_bot:
        answer = await client(fn.GetWebAuthorizationsRequest())
        bots = {
            str(getattr(auth, "hash", "")): int(getattr(auth, "bot_id", 0) or 0)
            for auth in getattr(answer, "authorizations", None) or []
        }
    done: list[str] = []
    for value in req.hash:
        await client(fn.ResetWebAuthorizationRequest(hash=_auth.parse_hash(value)))
        done.append(value)
        bot_id = bots.get(value.strip())
        if bot_id:
            # Through the resolver, never `client.get_input_entity`: the
            # access hash is per account, and the resolver is what makes the
            # NOT_FOUND / INDETERMINATE distinction in one place (§6.6).
            await client(contacts_fn.BlockRequest(id=await _send.resolve(ctx, str(bot_id))))
            blocked.append(bot_id)
    return WebSessionRevocation(revoked=len(done), hashes=done, blocked=blocked)


SPEC_WEBSITE_REVOKE = OperationSpec(
    id="account.website.revoke",
    request=WebsiteRevokeReq,
    response=WebSessionRevocation,
    impl=website_revoke,
    summary="Disconnect one website, or all of them",
    aliases=("websites.revoke", "privacy.website.revoke"),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("revoked", "hashes", "blocked"),
    example={"revoked": 1, "hashes": ["770"]},
    example_args="account website revoke 770",
    covers=("websites.disconnect", "websites.disconnect-all"),
    covers_partial=("privacy.connected-websites",),
    coverage_note="Listing them is `account website list`.",
)


# ---------------------------------------------------------------------------
# account passkey * — auditable, never usable from here
# ---------------------------------------------------------------------------


class PasskeyListReq(Request):
    pass


async def passkey_list(ctx: OpContext, req: PasskeyListReq) -> Page[Passkey]:
    """List passkeys registered on the account.

    Read-only, and that is not a limitation tlgr can lift: the server only
    accepts the relying-party id `telegram.org`, so no third-party client can
    ever create or use one. Auditing *what can log in* is still worth having.
    """
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    answer = await client(fn.GetPasskeysRequest())
    items = [_auth.passkey_model(raw) for raw in getattr(answer, "passkeys", None) or []]
    return Page(items=items, has_more=False, total=len(items))


SPEC_PASSKEY_LIST = OperationSpec(
    id="account.passkey.list",
    request=PasskeyListReq,
    response=Page[Passkey],
    impl=passkey_list,
    summary="List passkeys registered on the account",
    aliases=("security.passkey.list",),
    paginated=PageKind.LOCAL,
    rate_class="read",
    columns=("id", "name", "date", "last_usage_date"),
    headers=("ID", "Name", "Added", "Last used"),
    example={"items": [{"id": "pk_1", "name": "iPhone", "date": "2026-09-03T09:14:07Z"}]},
    example_args="account passkey list",
    covers=("auth.passkey-list",),
    tags=frozenset({"agent-safe"}),
)


class PasskeyDeleteReq(Request):
    id: Annotated[str, arg(0, metavar="ID", help="The passkey to delete.")]


async def passkey_delete(ctx: OpContext, req: PasskeyDeleteReq) -> Passkey:
    """Delete a passkey. It cannot be re-created from here."""
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    await client(fn.DeletePasskeyRequest(id=req.id))
    return Passkey(id=req.id, deleted=True)


SPEC_PASSKEY_DELETE = OperationSpec(
    id="account.passkey.delete",
    request=PasskeyDeleteReq,
    response=Passkey,
    impl=passkey_delete,
    summary="Delete a passkey",
    aliases=("security.passkey.delete",),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("id", "deleted"),
    example={"id": "pk_1", "deleted": True},
    example_args="account passkey delete pk_1",
    covers=("auth.passkey-delete",),
)


# ---------------------------------------------------------------------------
# account password * — 2-step verification
# ---------------------------------------------------------------------------


class PasswordGetReq(Request):
    password: Annotated[str | None, _PASSWORD] = None
    verify: Annotated[
        bool, opt("--verify", help="Check the supplied password against the server.")
    ] = False


async def password_get(ctx: OpContext, req: PasswordGetReq) -> PasswordState:
    """2-step verification status, and with the password the recovery address.

    Nothing cryptographic is printed: `srp_B`, `srp_id`, `secure_random` and
    the KDF salts are live parameters of an in-flight exchange, not status,
    and a status command that leaks them teaches everyone to paste them into
    an issue tracker.
    """
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    state = await _auth.get_password(client)
    model = _auth.password_state(state)
    if req.verify and req.password is None:
        raise UsageError(
            "--verify needs the password: --password-env, --password-stdin or --password-file",
            field="password",
        )
    if req.password is None:
        return model
    try:
        settings = await _auth.with_password(
            client,
            lambda check: fn.GetPasswordSettingsRequest(password=check),
            req.password,
            state=state,
        )
    except Exception as exc:
        if req.verify and "PASSWORD_HASH_INVALID" in str(exc):
            model.password_ok = False
            return model
        raise
    model.password_ok = True
    model.recovery_email = getattr(settings, "email", None)
    return model


SPEC_PASSWORD_GET = OperationSpec(
    id="account.password.get",
    request=PasswordGetReq,
    response=PasswordState,
    impl=password_get,
    summary="2-step verification status, and with the password the recovery address",
    description=(
        "`--verify` is the shared SRP path every other sensitive operation "
        "reuses: call with `inputCheckPasswordEmpty`, and on "
        "PASSWORD_HASH_INVALID ask and retry. SRP_ID_INVALID refetches "
        "`account.getPassword`, because an srp_id is single-use."
    ),
    aliases=("security.password.get", "account.password.status"),
    rate_class="read",
    columns=("has_password", "has_recovery", "hint", "password_ok"),
    example={"has_password": True, "has_recovery": True, "hint": "the usual"},
    example_args="account password get",
    covers=(
        "password.check-remembered",
        "password.recovery-email-view",
        "password.setup-required-after-login",
        "password.srp-for-sensitive-actions",
        "password.status",
    ),
    tags=frozenset({"agent-safe"}),
)


class PasswordSetReq(Request):
    new_password: Annotated[
        str | None,
        opt(secret=True, envvar="TLGR_2FA_NEW_PASSWORD", help="The password to set."),
    ] = None
    hint: Annotated[str | None, opt("--hint", help="Password hint (visible at login).")] = None
    email: Annotated[
        str | None, opt("--email", help="Recovery email; the server then wants a code.")
    ] = None
    code: Annotated[str | None, opt("--code", help="Confirmation code for --email.")] = None


async def password_set(ctx: OpContext, req: PasswordSetReq) -> PasswordState:
    """Turn on 2-step verification.

    `v = g^x mod p` is computed from `account.getPassword().new_algo` with a
    fresh salt suffix (`telethon.password.compute_digest`);
    NEW_SALT_INVALID / NEW_SETTINGS_INVALID mean the KDF was wrong and
    PASSWORD_HASH_INVALID means one already exists — that is `change`.
    """
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    if req.code:
        await client(fn.ConfirmPasswordEmailRequest(code=req.code))
        return _auth.password_state(await _auth.get_password(client))
    if not req.new_password:
        raise UsageError(
            "give the new password through --new-password-env, --new-password-stdin "
            "or --new-password-file — never on the command line",
            field="new_password",
        )
    state = await _auth.get_password(client)
    if getattr(state, "has_password", False):
        raise UsageError(
            "this account already has a cloud password; change it with: "
            "tlgr account password change",
            field="new_password",
        )
    settings = _auth.new_password_settings(
        state, new_password=req.new_password, hint=req.hint or "", email=req.email
    )
    try:
        await client(
            fn.UpdatePasswordSettingsRequest(password=_auth.empty_password(), new_settings=settings)
        )
    except Exception as exc:
        if "EMAIL_UNCONFIRMED" not in str(exc):
            raise
        model = _auth.password_state(await _auth.get_password(client))
        ctx.warn(
            "the password is set but the recovery email is unconfirmed; "
            "finish with: tlgr account password set --code <code from the email>"
        )
        return model
    ctx.emit("password_set", {})
    return _auth.password_state(await _auth.get_password(client))


SPEC_PASSWORD_SET = OperationSpec(
    id="account.password.set",
    request=PasswordSetReq,
    response=PasswordState,
    impl=password_set,
    summary="Turn on 2-step verification (cloud password, hint, recovery email)",
    aliases=("security.password.set",),
    mutating=True,
    rate_class="send",
    columns=("has_password", "hint", "email_unconfirmed_pattern"),
    example={"has_password": True, "hint": "the usual"},
    example_args="account password set --hint 'the usual'",
    covers=("password.recovery-email-set", "password.set"),
)


class PasswordChangeReq(Request):
    password: Annotated[str | None, _PASSWORD] = None
    new_password: Annotated[
        str | None,
        opt(secret=True, envvar="TLGR_2FA_NEW_PASSWORD", help="The replacement password."),
    ] = None
    hint: Annotated[str | None, opt("--hint", help="New hint (can be changed on its own).")] = None
    keep_passport: Annotated[
        bool,
        opt(
            "--keep-passport",
            help="Acknowledge that Passport data is dropped when it cannot be re-encrypted.",
        ),
    ] = False


async def password_change(ctx: OpContext, req: PasswordChangeReq) -> PasswordState:
    """Change the cloud password, or just its hint.

    Refused when the account holds Passport data unless `--keep-passport`
    acknowledges the loss: the Passport secure secret is encrypted under the
    password, and re-encrypting it needs a KDF tlgr does not implement. A
    change that silently destroyed a user's identity documents would be a
    much worse bug than a refusal.
    """
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    if req.password is None:
        raise UsageError(
            "changing the password needs the current one: --password-env, "
            "--password-stdin or --password-file",
            field="password",
        )
    if not req.new_password and req.hint is None:
        raise UsageError("nothing to change: pass a new password or --hint", field="new_password")
    state = await _auth.get_password(client)
    if getattr(state, "has_secure_values", False) and not req.keep_passport:
        raise UsageError(
            "this account stores Telegram Passport documents, which are encrypted under the "
            "cloud password. tlgr cannot re-encrypt them, so changing the password would "
            "destroy them. Delete them first (tlgr passport delete …) or pass --keep-passport "
            "to accept the loss.",
            field="keep_passport",
        )
    settings = _auth.new_password_settings(
        state, new_password=req.new_password, hint=req.hint if req.hint is not None else None
    )
    await _auth.with_password(
        client,
        lambda check: fn.UpdatePasswordSettingsRequest(password=check, new_settings=settings),
        req.password,
        state=state,
    )
    ctx.warn(
        "changing the password starts Telegram's 24-hour PASSWORD_TOO_FRESH cooldown "
        "on sensitive operations such as transferring channel ownership"
    )
    model = _auth.password_state(await _auth.get_password(client))
    model.changed = True
    model.sensitive_actions_eligible_at = _auth.iso(_auth.now() + timedelta(hours=24))
    return model


SPEC_PASSWORD_CHANGE = OperationSpec(
    id="account.password.change",
    request=PasswordChangeReq,
    response=PasswordState,
    impl=password_change,
    summary="Change the cloud password and/or its hint",
    aliases=("security.password.change",),
    mutating=True,
    rate_class="send",
    columns=("changed", "hint", "sensitive_actions_eligible_at"),
    example={"changed": True, "has_password": True, "hint": "the new one"},
    example_args="account password change --hint 'the new one'",
    covers=("password.change", "password.hint", "password.secure-secret-reencrypt"),
)


class PasswordRemoveReq(Request):
    password: Annotated[str | None, _PASSWORD] = None


async def password_remove(ctx: OpContext, req: PasswordRemoveReq) -> PasswordState:
    """Turn 2-step verification off. Passport documents go with it."""
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    if req.password is None:
        raise UsageError(
            "removing the password needs the current one: --password-env, "
            "--password-stdin or --password-file",
            field="password",
        )
    state = await _auth.get_password(client)
    if not getattr(state, "has_password", False):
        _auth.already(ctx)
        return _auth.password_state(state)
    if getattr(state, "has_secure_values", False):
        ctx.warn("this also deletes the Telegram Passport documents stored on the account")
    settings = _auth.new_password_settings(state)
    await _auth.with_password(
        client,
        lambda check: fn.UpdatePasswordSettingsRequest(password=check, new_settings=settings),
        req.password,
        state=state,
    )
    return _auth.password_state(await _auth.get_password(client))


SPEC_PASSWORD_REMOVE = OperationSpec(
    id="account.password.remove",
    request=PasswordRemoveReq,
    response=PasswordState,
    impl=password_remove,
    summary="Turn off 2-step verification",
    aliases=("security.password.remove",),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("has_password",),
    example={"has_password": False},
    example_args="account password remove",
    covers=("password.remove",),
)


class PasswordResetReq(Request):
    cancel: Annotated[bool, opt("--cancel", help="Decline the pending reset.")] = False


async def password_reset(ctx: OpContext, req: PasswordResetReq) -> PasswordReset:
    """Start (or cancel) the 7-day password reset for a password nobody has.

    Only works from a session that is still logged in — which is what makes
    it different from `auth recover` and from `auth reset-account`: the
    account survives, only the password goes.
    """
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    if req.cancel:
        await client(fn.DeclinePasswordResetRequest())
        return PasswordReset(status="cancelled", cancelled=True)
    answer = await client(fn.ResetPasswordRequest())
    name = type(answer).__name__
    if name == "ResetPasswordRequestedWait":
        return PasswordReset(
            status="wait", until_date=_auth.iso(getattr(answer, "until_date", None))
        )
    if name == "ResetPasswordFailedWait":
        return PasswordReset(
            status="too_soon", retry_date=_auth.iso(getattr(answer, "retry_date", None))
        )
    return PasswordReset(status="ok")


SPEC_PASSWORD_RESET = OperationSpec(
    id="account.password.reset",
    request=PasswordResetReq,
    response=PasswordReset,
    impl=password_reset,
    summary="Request a password reset without the recovery email, or cancel a pending one",
    aliases=("security.password.reset",),
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("status", "until_date", "retry_date"),
    example={"status": "wait", "until_date": "2026-09-10T09:14:07Z"},
    example_args="account password reset",
    covers=("password.reset-without-email",),
)


class PasswordTempReq(Request):
    password: Annotated[str | None, _PASSWORD] = None
    period: Annotated[str, opt("--period", metavar="DURATION", help="Validity window.")] = "1h"


async def password_temp(ctx: OpContext, req: PasswordTempReq) -> TempPassword:
    """Issue a temporary password for payment confirmations.

    Issuing it is harmless; the payment that consumes it must be completed by
    a human, which is why tlgr has no command that spends one.
    """
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    if req.password is None:
        raise UsageError(
            "a temporary password is minted from the cloud password: use --password-env",
            field="password",
        )
    seconds = parse_duration(req.period)
    if seconds is None or not 60 <= seconds <= 3600:
        raise UsageError("--period must be between 1m and 1h", field="period")
    answer = await _auth.with_password(
        client,
        lambda check: fn.GetTmpPasswordRequest(password=check, period=int(seconds)),
        req.password,
    )
    raw = getattr(answer, "tmp_password", b"") or b""
    return TempPassword(
        tmp_password=_auth.b64(raw),
        valid_until=_auth.iso(getattr(answer, "valid_until", None)),
    )


SPEC_PASSWORD_TEMP = OperationSpec(
    id="account.password.temp",
    request=PasswordTempReq,
    response=TempPassword,
    impl=password_temp,
    summary="Issue a temporary password for payment confirmations",
    aliases=("security.tmp-password", "account.password.tmp"),
    mutating=True,
    rate_class="send",
    columns=("valid_until",),
    example={"tmp_password": "3q2-7w", "valid_until": "2026-09-03T10:14:07Z"},
    example_args="account password temp --period 1h",
    covers=("password.temporary-payment-password",),
    tags=frozenset({"redact"}),
)


# ---------------------------------------------------------------------------
# account email set / phone set
# ---------------------------------------------------------------------------


class EmailSetReq(Request):
    email: Annotated[
        str | None, arg(0, metavar="EMAIL", required=False, help="The address to write.")
    ] = None
    password: Annotated[str | None, _PASSWORD] = None
    show: Annotated[bool, opt("--show", help="Only print the current addresses/patterns.")] = False
    kind: Annotated[str | None, choice("recovery", "login", help="Which email to write.")] = (
        "recovery"
    )
    code: Annotated[str | None, opt("--code", help="Confirmation code from the address.")] = None
    resend: Annotated[bool, opt("--resend", help="Resend the pending confirmation code.")] = False
    cancel: Annotated[bool, opt("--cancel", help="Cancel the pending email change.")] = False


async def email_set(ctx: OpContext, req: EmailSetReq) -> RecoveryEmail:
    """Show, set or change the recovery or login email, and confirm it.

    The two kinds are different mechanisms wearing the same word: a
    *recovery* address is part of the cloud-password settings and needs the
    password, a *login* address is a second factor at the login screen and
    goes through `emailVerifyPurposeLoginChange`.
    """
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    client = _auth.client(ctx)
    kind = req.kind or "recovery"

    if req.show:
        state = await _auth.get_password(client)
        model = RecoveryEmail(
            kind=kind,
            email_pattern=(
                getattr(state, "login_email_pattern", None)
                if kind == "login"
                else getattr(state, "email_unconfirmed_pattern", None)
            ),
            confirmed=not getattr(state, "email_unconfirmed_pattern", None),
        )
        if req.password and kind == "recovery":
            settings = await _auth.with_password(
                client,
                lambda check: fn.GetPasswordSettingsRequest(password=check),
                req.password,
                state=state,
            )
            model.email_pattern = getattr(settings, "email", None) or model.email_pattern
        return model

    if req.cancel:
        await client(fn.CancelPasswordEmailRequest())
        return RecoveryEmail(kind=kind, cancelled=True)
    if req.resend:
        await client(fn.ResendPasswordEmailRequest())
        return RecoveryEmail(kind=kind, resent=True)

    if kind == "login":
        purpose = types.EmailVerifyPurposeLoginChange()
        if req.code:
            verified = await client(
                fn.VerifyEmailRequest(
                    purpose=purpose, verification=types.EmailVerificationCode(code=req.code)
                )
            )
            return RecoveryEmail(
                kind=kind, confirmed=True, email_pattern=getattr(verified, "email", None)
            )
        if not req.email:
            raise UsageError("give an email address, or --code to confirm one", field="email")
        sent = await client(fn.SendVerifyEmailCodeRequest(purpose=purpose, email=req.email))
        return RecoveryEmail(
            kind=kind,
            email_pattern=getattr(sent, "email_pattern", None),
            sent_code_length=getattr(sent, "length", None),
        )

    if req.code:
        await client(fn.ConfirmPasswordEmailRequest(code=req.code))
        return RecoveryEmail(kind=kind, confirmed=True)
    if not req.email:
        raise UsageError("give an email address, or --show/--code/--resend/--cancel", field="email")
    if req.password is None:
        raise UsageError(
            "changing the recovery email needs the cloud password: --password-env",
            field="password",
        )
    state = await _auth.get_password(client)
    settings = _auth.new_password_settings(state, email=req.email)
    try:
        await _auth.with_password(
            client,
            lambda check: fn.UpdatePasswordSettingsRequest(password=check, new_settings=settings),
            req.password,
            state=state,
        )
    except Exception as exc:
        if "EMAIL_UNCONFIRMED" not in str(exc):
            raise
        return RecoveryEmail(kind=kind, email_pattern=req.email, confirmed=False)
    return RecoveryEmail(kind=kind, email_pattern=req.email, confirmed=True)


SPEC_EMAIL_SET = OperationSpec(
    id="account.email.set",
    request=EmailSetReq,
    response=RecoveryEmail,
    impl=email_set,
    summary="Show, set or change the login / recovery email and confirm it",
    mutating=True,
    rate_class="send",
    columns=("kind", "email_pattern", "confirmed"),
    example={"kind": "recovery", "email_pattern": "a**@e*****e.com", "confirmed": False},
    example_args="account email set ada@example.com",
    covers=("account.verify-phone-email", "password.recovery-email-resend-cancel"),
    covers_partial=(
        "auth.login-email-change",
        "password.recovery-email-set",
        "password.recovery-email-view",
    ),
    coverage_note=(
        "During a pending login the owner is `auth login-email set`; the "
        "password settings themselves are `account password set/get`."
    ),
)


class PhoneSetReq(Request):
    phone: Annotated[
        str | None,
        arg(0, metavar="PHONE", required=False, help="New number; omit when submitting a code."),
    ] = None
    code: Annotated[str | None, opt("--code", help="Code that arrived at the number.")] = None
    code_hash: Annotated[
        str | None,
        opt("--code-hash", metavar="HASH", help="Override the hash the first step returned."),
    ] = None
    confirm_hash: Annotated[
        str | None,
        opt("--confirm-hash", metavar="HASH", help="Hash from a tg://confirmphone link."),
    ] = None
    resend: Annotated[bool, opt("--resend", help="Resend the code.")] = False
    cancel: Annotated[bool, opt("--cancel", help="Cancel the pending code.")] = False


async def phone_set(ctx: OpContext, req: PhoneSetReq) -> PhoneChange:
    """Change the account's phone number, or confirm a phone-based action.

    Two flows on one command because they are the same code-then-confirm
    shape. TDLib marks the change-number code as official-apps-only, so a
    third-party `api_id` may simply get `SEND_CODE_UNAVAILABLE`;
    `FRESH_CHANGE_PHONE_FORBIDDEN` means the session is younger than 24
    hours, and `PHONE_NUMBER_OCCUPIED` means the target number already has an
    account. All three are reported verbatim, because none of them is
    something a client can work around.
    """
    from telethon.tl.functions import account as fn
    from telethon.tl.functions import auth as auth_fn

    client = _auth.client(ctx)
    manager = _auth.accounts(ctx)
    alias = _auth.resolve_alias(ctx)
    stored = _phone_state(manager, alias)
    settings = _auth.code_settings()

    if req.confirm_hash and not req.code:
        sent = await client(
            fn.SendConfirmPhoneCodeRequest(hash=req.confirm_hash, settings=settings)
        )
        fields = _auth.sent_code_fields(sent)
        _phone_state(manager, alias, {"code_hash": fields["code_hash"], "confirm": True})
        return PhoneChange(
            code_hash=fields["code_hash"], type=fields["type"], timeout=fields.get("timeout")
        )

    code_hash = req.code_hash or str(stored.get("code_hash", ""))
    if req.cancel or req.resend:
        phone = req.phone or str(stored.get("phone", ""))
        if not phone or not code_hash:
            raise UsageError("no phone-number change is in progress", field="phone")
        if req.cancel:
            await client(auth_fn.CancelCodeRequest(phone_number=phone, phone_code_hash=code_hash))
            _phone_state(manager, alias, {})
            return PhoneChange(phone=_auth.masked(phone), cancelled=True)
        sent = await client(
            auth_fn.ResendCodeRequest(phone_number=phone, phone_code_hash=code_hash)
        )
        fields = _auth.sent_code_fields(sent)
        _phone_state(manager, alias, {"phone": phone, "code_hash": fields["code_hash"]})
        return PhoneChange(
            phone=_auth.masked(phone),
            code_hash=fields["code_hash"],
            type=fields["type"],
            resent=True,
        )

    if req.code:
        if not code_hash:
            raise UsageError(
                "no code is pending: run `account phone set <new number>` first, "
                "or pass --code-hash",
                field="code_hash",
            )
        if stored.get("confirm"):
            await client(fn.ConfirmPhoneRequest(phone_code_hash=code_hash, phone_code=req.code))
            _phone_state(manager, alias, {})
            return PhoneChange(confirmed=True)
        phone = req.phone or str(stored.get("phone", ""))
        await client(
            fn.ChangePhoneRequest(
                phone_number=phone, phone_code_hash=code_hash, phone_code=req.code
            )
        )
        _phone_state(manager, alias, {})
        manager.update_account(alias, phone=phone)
        return PhoneChange(phone=_auth.masked(phone), changed=True)

    if not req.phone:
        raise UsageError("give the new phone number, or --code/--confirm-hash", field="phone")
    sent = await client(fn.SendChangePhoneCodeRequest(phone_number=req.phone, settings=settings))
    fields = _auth.sent_code_fields(sent)
    _phone_state(manager, alias, {"phone": req.phone, "code_hash": fields["code_hash"]})
    return PhoneChange(
        phone=_auth.masked(req.phone),
        code_hash=fields["code_hash"],
        type=fields["type"],
        timeout=fields.get("timeout"),
    )


def _phone_state(manager: Any, alias: str, write: dict[str, Any] | None = None) -> dict[str, Any]:
    """Remember the `phone_code_hash` between the two halves of a change.

    The same reason `login-state.json` exists: the second command runs in a
    different process, and Telegram will not accept a code without the hash
    that came with it.
    """
    import json

    path = manager.paths.account_dir(alias) / "phone-change.json"
    if write is not None:
        if write:
            write_private(path, json.dumps(write, sort_keys=True))
        else:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        return write
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


SPEC_PHONE_SET = OperationSpec(
    id="account.phone.set",
    request=PhoneSetReq,
    response=PhoneChange,
    impl=phone_set,
    summary="Change the account's phone number, or confirm a tg://confirmphone action",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("phone", "code_hash", "type", "changed"),
    example={"phone": "989…89", "code_hash": "5f2a…", "type": "app"},
    example_args="account phone set +989123456789",
    covers=("account.cancel-deletion-confirm-phone", "account.change-phone"),
)


# ---------------------------------------------------------------------------
# account ttl / device-locked / delete
# ---------------------------------------------------------------------------


class TtlGetReq(Request):
    pass


async def ttl_get(ctx: OpContext, req: TtlGetReq) -> AccountTtl:
    """Show the self-destruct timer: delete this account after N days away."""
    from telethon.tl.functions import account as fn

    answer = await _auth.client(ctx)(fn.GetAccountTTLRequest())
    return AccountTtl(days=int(getattr(answer, "days", 0) or 0))


SPEC_TTL_GET = OperationSpec(
    id="account.ttl.get",
    request=TtlGetReq,
    response=AccountTtl,
    impl=ttl_get,
    summary="Show the self-destruct timer (delete my account if I am away for N months)",
    rate_class="read",
    columns=("days",),
    example={"days": 365},
    example_args="account ttl get",
    covers=("account.self-destruct-ttl",),
    tags=frozenset({"agent-safe"}),
)


class TtlSetReq(Request):
    value: Annotated[
        str, arg(0, metavar="VALUE", help="30-730 days, or a preset: 1m 3m 6m 12m 18m 24m.")
    ]


async def ttl_set(ctx: OpContext, req: TtlSetReq) -> AccountTtl:
    """Set the self-destruct timer. `TTL_DAYS_INVALID` outside 30-730 days."""
    from telethon.tl import types
    from telethon.tl.functions import account as fn

    days = _days(req.value, low=30, high=730, field="value")
    await _auth.client(ctx)(fn.SetAccountTTLRequest(ttl=types.AccountDaysTTL(days=days)))
    return AccountTtl(days=days)


SPEC_TTL_SET = OperationSpec(
    id="account.ttl.set",
    request=TtlSetReq,
    response=AccountTtl,
    impl=ttl_set,
    summary="Set the self-destruct timer (30-730 days)",
    mutating=True,
    destructive=True,
    idempotent=True,
    rate_class="send",
    columns=("days",),
    example={"days": 365},
    example_args="account ttl set 12m",
    covers=("messages-core.ttl-default-new-chats",),
    covers_partial=("account.self-destruct-ttl",),
    coverage_note="Reading it is `account ttl get`.",
)


class DeviceLockedSetReq(Request):
    period: Annotated[
        str | None,
        arg(0, metavar="PERIOD", required=False, help="How long the device stays locked."),
    ] = None
    unlock: Annotated[bool, opt("--unlock", help="Report the device as unlocked.")] = False


async def device_locked_set(ctx: OpContext, req: DeviceLockedSetReq) -> DeviceLock:
    """Tell the server this device is locked, so push arrives without content.

    Only meaningful for an account that also has a push token registered
    somewhere else — tlgr receives updates over MTProto and registers none —
    which is exactly the headless-box case: suppress Telegram's own push
    previews on the phone while a script is working the account.
    """
    from telethon.tl.functions import account as fn

    if req.unlock:
        seconds = 0
    elif req.period:
        seconds = int(parse_duration(req.period) or 0)
    else:
        raise UsageError("give a period, or --unlock", field="period")
    await _auth.client(ctx)(fn.UpdateDeviceLockedRequest(period=seconds))
    return DeviceLock(locked_for=seconds)


SPEC_DEVICE_LOCKED_SET = OperationSpec(
    id="account.device-locked.set",
    request=DeviceLockedSetReq,
    response=DeviceLock,
    impl=device_locked_set,
    summary="Tell the server this device is locked so push arrives without content",
    mutating=True,
    idempotent=True,
    rate_class="send",
    columns=("locked_for",),
    example={"locked_for": 3600},
    example_args="account device-locked set 1h",
    covers=("auth.device-locked", "privacy.device-locked"),
)


class DeleteReq(Request):
    password: Annotated[str | None, _PASSWORD] = None
    reason: Annotated[str, opt("--reason", help="Reason string sent to the server.")] = ""
    confirm_phone: Annotated[
        str | None,
        opt("--confirm-phone", metavar="PHONE", help="Retype the account's number — required."),
    ] = None


async def delete(ctx: OpContext, req: DeleteReq) -> AccountDeletion:
    """Delete this Telegram account permanently.

    Irreversible, and gated by the number typed back as well as `--yes`. With
    2FA set and no password supplied the server answers `2FA_CONFIRM_WAIT_X`
    and schedules the deletion instead of performing it; that countdown is
    reported, along with how to cancel it.
    """
    from telethon.tl.functions import account as fn

    from tlgr.ops.auth import _digits, _wait_seconds

    client = _auth.client(ctx)
    me = await client.get_me()
    phone = getattr(me, "phone", "") or ""
    if not req.confirm_phone or _digits(req.confirm_phone) != _digits(phone):
        raise UsageError(
            "pass --confirm-phone with this account's own number: deleting it is permanent",
            field="confirm_phone",
        )
    try:
        if req.password is None:
            await client(fn.DeleteAccountRequest(reason=req.reason, password=None))
        else:
            await _auth.with_password(
                client,
                lambda check: fn.DeleteAccountRequest(reason=req.reason, password=check),
                req.password,
            )
    except Exception as exc:
        remaining = _wait_seconds(str(exc))
        if remaining is None:
            raise
        return AccountDeletion(
            status="wait",
            wait_seconds=remaining,
            until=_auth.iso(_auth.now() + timedelta(seconds=remaining)),
            confirm_hint=(
                "the account has 2-step verification: Telegram scheduled the deletion instead. "
                "Cancel it with the tg://confirmphone link it sent: "
                "tlgr account phone set --confirm-hash <hash>"
            ),
        )
    return AccountDeletion(deleted=True, status="deleted")


SPEC_DELETE = OperationSpec(
    id="account.delete",
    request=DeleteReq,
    response=AccountDeletion,
    impl=delete,
    summary="Delete this Telegram account permanently",
    mutating=True,
    destructive=True,
    rate_class="send",
    columns=("deleted", "status", "wait_seconds"),
    example={"deleted": True, "status": "deleted"},
    example_args="account delete --confirm-phone +989123456789",
    covers=("account.delete",),
)


# ---------------------------------------------------------------------------
# account smsjobs / suggestion / support
# ---------------------------------------------------------------------------


class SmsJobsSetReq(Request):
    join: Annotated[bool, opt("--join", help="Join the Peer-to-Peer Login Program.")] = False
    leave: Annotated[bool, opt("--leave", help="Leave it.")] = False
    allow_international: Annotated[
        bool | None,
        opt("--allow-international/--no-allow-international", help="Accept international jobs."),
    ] = None


async def smsjobs_set(ctx: OpContext, req: SmsJobsSetReq) -> SmsJobs:
    """The Peer-to-Peer Login Program: status, join, leave.

    Control-only, deliberately: fulfilling a job means sending an SMS from a
    real modem, so `smsjobs.getSmsJob`/`finishSmsJob` are not exposed —
    joining a programme a CLI cannot honour would earn the account a
    reputation hit for messages it never sent.
    """
    from telethon.tl.functions import smsjobs as fn

    client = _auth.client(ctx)
    if req.join and req.leave:
        raise UsageError("--join and --leave are opposites", field="join")
    if req.join:
        await client(fn.JoinRequest())
    if req.leave:
        await client(fn.LeaveRequest())
    if req.allow_international is not None:
        await client(fn.UpdateSettingsRequest(allow_international=req.allow_international))

    model = SmsJobs()
    try:
        status = await client(fn.GetStatusRequest())
    except Exception:
        eligible = None
        with contextlib.suppress(Exception):
            eligible = await client(fn.IsEligibleToJoinRequest())
        return SmsJobs(
            eligible=eligible is not None,
            joined=False,
            terms_url=getattr(eligible, "terms_url", None),
        )
    return SmsJobs(
        eligible=True,
        joined=True,
        allow_international=bool(getattr(status, "allow_international", False)),
        recent_sent=getattr(status, "recent_sent", None),
        recent_since=_auth.iso(getattr(status, "recent_since", None)),
        recent_remains=getattr(status, "recent_remains", None),
        terms_url=getattr(status, "terms_url", None) or model.terms_url,
    )


SPEC_SMSJOBS_SET = OperationSpec(
    id="account.smsjobs.set",
    request=SmsJobsSetReq,
    response=SmsJobs,
    impl=smsjobs_set,
    summary="Peer-to-Peer Login Program (SMS jobs): status, join, leave",
    mutating=True,
    rate_class="send",
    columns=("eligible", "joined", "recent_sent", "recent_remains"),
    example={"eligible": True, "joined": False, "terms_url": "https://telegram.org/tos/sms"},
    example_args="account smsjobs set",
    covers=("account.sms-jobs",),
)


class SuggestionListReq(Request):
    dismiss: Annotated[
        str | None, opt("--dismiss", metavar="NAME", help="Dismiss this suggestion.")
    ] = None
    hide_promo: Annotated[
        PeerRef | None,
        opt("--hide-promo", metavar="CHAT", kind="peer", help="Hide the promoted dialog."),
    ] = None


async def suggestion_list(ctx: OpContext, req: SuggestionListReq) -> Page[Suggestion]:
    """Pending server suggestions and the promoted chat, and dismissing them.

    `SETUP_LOGIN_EMAIL_NOSKIP` is reported as non-dismissible and `--dismiss`
    refuses it: the server means it, and pretending otherwise would make the
    command lie about what it did.
    """
    from telethon.tl import types
    from telethon.tl.functions import help as fn

    client = _auth.client(ctx)
    if req.dismiss:
        if req.dismiss.strip().upper().endswith("NOSKIP"):
            raise UsageError(
                f"{req.dismiss} cannot be dismissed; the server re-issues it until it is done",
                field="dismiss",
            )
        await client(
            fn.DismissSuggestionRequest(peer=types.InputPeerEmpty(), suggestion=req.dismiss)
        )
    if req.hide_promo:
        await client(fn.HidePromoDataRequest(peer=await _send.resolve(ctx, req.hide_promo)))

    config = await _auth.app_config(client)
    pending = [str(name) for name in (config.get("pending_suggestions") or [])]
    dismissed = {str(name) for name in (config.get("dismissed_suggestions") or [])}
    items = [
        Suggestion(
            suggestion=name,
            dismissible=not name.upper().endswith("NOSKIP"),
            dismissed=name == req.dismiss,
        )
        for name in pending
        if name not in dismissed
    ]
    promo = None
    with contextlib.suppress(Exception):
        promo = await client(fn.GetPromoDataRequest())
    peer = getattr(promo, "peer", None)
    if peer is not None:
        from telethon import utils

        items.append(
            Suggestion(
                suggestion="promo",
                dismissible=True,
                promo_peer=int(utils.get_peer_id(peer)),
                psa_type=getattr(promo, "psa_type", None),
                hidden=bool(req.hide_promo),
            )
        )
    return Page(items=items, has_more=False, total=len(items))


SPEC_SUGGESTION_LIST = OperationSpec(
    id="account.suggestion.list",
    request=SuggestionListReq,
    response=Page[Suggestion],
    impl=suggestion_list,
    summary="Pending server suggestions and promoted chats, and dismissing them",
    mutating=True,
    paginated=PageKind.LOCAL,
    rate_class="read",
    columns=("suggestion", "dismissible", "promo_peer"),
    example={"items": [{"suggestion": "VALIDATE_PASSWORD", "dismissible": True}]},
    example_args="account suggestion list",
    covers=("account.promo-data", "auth.security-suggestions"),
    covers_partial=("password.check-remembered",),
    coverage_note="Actually checking the password is `account password get --verify`.",
)


class SupportGetReq(Request):
    user: Annotated[
        PeerRef | None,
        arg(0, metavar="USER", required=False, kind="peer", help="Support accounts only."),
    ] = None
    info: Annotated[bool, opt("--info", help="Read the support note on USER.")] = False
    set_note: Annotated[
        str | None, opt("--set", "--set-note", metavar="TEXT", help="Write the support note.")
    ] = None


async def support_get(ctx: OpContext, req: SupportGetReq) -> SupportInfo:
    """Telegram support contact, the FAQ links, and the invite text.

    `--info`/`--set` only work from a Telegram support account; every other
    account gets an error from the server, which is reported as-is rather
    than hidden behind a capability check tlgr cannot perform.
    """
    from telethon.tl.functions import help as fn

    client = _auth.client(ctx)
    if req.user and (req.info or req.set_note is not None):
        target = await _send.resolve(ctx, req.user)
        if req.set_note is not None:
            answer = await client(
                fn.EditUserInfoRequest(user_id=target, message=req.set_note, entities=[])
            )
        else:
            answer = await client(fn.GetUserInfoRequest(user_id=target))
        return SupportInfo(
            note=getattr(answer, "message", None),
            author=getattr(answer, "author", None),
            date=_auth.iso(getattr(answer, "date", None)),
        )

    support = await client(fn.GetSupportRequest())
    name = None
    with contextlib.suppress(Exception):
        name = getattr(await client(fn.GetSupportNameRequest()), "name", None)
    invite = None
    with contextlib.suppress(Exception):
        invite = getattr(await client(fn.GetInviteTextRequest()), "message", None)
    config = await _auth.app_config(client)
    me = await client.get_me()
    user = getattr(support, "user", None)
    return SupportInfo(
        support_user=getattr(user, "id", None),
        support_name=name or getattr(user, "first_name", None),
        support_phone=getattr(support, "phone_number", None),
        faq_url=str(config.get("faq_url") or "https://telegram.org/faq"),
        privacy_url=str(config.get("privacy_url") or "https://telegram.org/privacy"),
        features_url=str(config.get("features_url") or "https://telegram.org/blog"),
        invite_text=invite,
        my_link=f"https://t.me/{me.username}" if getattr(me, "username", None) else None,
    )


SPEC_SUPPORT_GET = OperationSpec(
    id="account.support.get",
    request=SupportGetReq,
    response=SupportInfo,
    impl=support_get,
    summary="Telegram support contact, FAQ/privacy links and the invite text",
    mutating=True,
    rate_class="read",
    columns=("support_user", "support_name", "faq_url"),
    example={
        "support_user": 333000,
        "support_name": "Telegram Support",
        "faq_url": "https://telegram.org/faq",
    },
    example_args="account support get",
    covers=(
        "account.faq-links",
        "account.invite-friends",
        "account.support-chat",
        "account.support-user-info",
        "auth.logout-alternatives",
        "contacts-users.user-support",
        "contacts-users.user-support-info",
    ),
    tags=frozenset({"agent-safe"}),
)
