"""The only code in the CLI that opens a socket (§5.1).

v1 wrote the request line by hand, concatenated headers into an f-string,
`json.dumps(..., default=str)`-ed the body, read until the socket went quiet
and then decoded chunked transfer-encoding with a hand-rolled loop over a
`str`. That produced COR-04 (a Persian search query never arrived), COR-31 (a
"timeout" was indistinguishable from a short read, so a truncated response
looked like success) and COR-32 (a chunked body with a multi-byte character on
a chunk boundary was corrupted).

All three are consequences of not using `http.client`, which already knows how
to frame a request, how to read a chunked body and how to raise on a truncated
one. This module supplies the two things it does not know: how to connect to a
Unix socket, and what tlgr's error envelope means.
"""

from __future__ import annotations

import contextlib
import http.client
import os
import socket
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import msgspec

from tlgr.core.errors import (
    DaemonNotRunningError,
    IPCError,
    RetryableError,
    TlgrError,
    UsageError,
)
from tlgr.core.paths import TlgrPaths
from tlgr.models.envelope import OpRequest
from tlgr.transport import autostart
from tlgr.transport.ndjson import parse_frame
from tlgr.version import (
    HEADER_CLIENT,
    HEADER_PROTOCOL,
    HEADER_REQUEST_ID,
    HEADER_TOKEN,
    PROTOCOL,
    VERSION,
)

__all__ = [
    "DaemonClient",
    "admin",
    "error_from_body",
    "events",
    "legacy_request",
    "make_dispatcher",
    "op",
    "set_default_flood_wait_max",
    "status",
    "stream",
]

DEFAULT_TIMEOUT = 120.0
#: A stream is open for as long as the caller wants it; the read timeout has
#: to be longer than the heartbeat interval or every quiet minute looks dead.
STREAM_TIMEOUT = 3600.0


class _UnixHTTPConnection(http.client.HTTPConnection):
    """`http.client` over `AF_UNIX`. The whole connection story."""

    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


#: Error codes that have a dedicated exception class. Everything else becomes
#: a `RemoteError` carrying the daemon's own code and exit status, so a code
#: added on the daemon side does not need a CLI release to exit correctly.
def _code_classes() -> dict[str, type[TlgrError]]:
    from tlgr.core import errors

    return {
        "USAGE": errors.UsageError,
        "ACCOUNT_REQUIRED": errors.AccountRequiredError,
        "AUTH_ERROR": errors.AuthenticationError,
        "AUTH_PASSWORD_REQUIRED": errors.AuthPasswordRequiredError,
        "SESSION_ERROR": errors.SessionError,
        "NOT_FOUND": errors.NotFoundError,
        "CHAT_NOT_FOUND": errors.ChatNotFoundError,
        "ACCOUNT_NOT_FOUND": errors.AccountNotFoundError,
        "PERMISSION_DENIED": errors.PermissionError_,
        "RATE_LIMITED": errors.RateLimitError,
        "PEER_FLOOD": errors.SpamFlagError,
        "ACCOUNT_FROZEN": errors.AccountFrozenError,
        "RETRYABLE": errors.RetryableError,
        "CONFIG_ERROR": errors.ConfigurationError,
        "DAEMON_ERROR": errors.DaemonError,
        "DAEMON_NOT_RUNNING": errors.DaemonNotRunningError,
        "DAEMON_VERSION_MISMATCH": errors.DaemonVersionMismatchError,
        "INDETERMINATE": errors.IndeterminateError,
        "IPC_ERROR": errors.IPCError,
    }


class RemoteError(TlgrError):
    """An error the daemon classified, carried across the socket intact.

    The daemon has already decided the code, the exit status and whether a
    retry is worth attempting; re-deriving any of that from an HTTP status on
    this side would be a second opinion that can disagree with the first.
    """


def error_from_body(body: dict[str, Any], *, status_code: int = 500) -> TlgrError:
    """Rebuild the exception the daemon classified."""
    message = str(body.get("message") or body.get("error") or f"daemon returned {status_code}")
    code = str(body.get("code") or "IPC_ERROR")
    classes = _code_classes()
    klass = classes.get(code)

    error: TlgrError
    if code == "RATE_LIMITED" and klass is not None:
        error = klass(message, wait_seconds=int(body.get("wait_seconds") or 0))  # type: ignore[call-arg]
    elif klass is not None:
        error = klass(message)
    else:
        error = RemoteError(message, code=code)

    error.code = code
    exit_code = body.get("exit_code")
    if isinstance(exit_code, int):
        error.exit_code = exit_code
    if body.get("hint"):
        error.hint = str(body["hint"])
    if body.get("retryable"):
        error.retryable = True
    for extra in ("field", "wait_seconds", "rpc", "account", "request_id", "reason"):
        if body.get(extra) is not None:
            setattr(error, extra, body[extra])
    return error


class DaemonClient:
    """One connection story: connect, send JSON, read JSON or NDJSON.

    Instances are cheap and hold no socket between calls — HTTP/1.1 with
    `Connection: close` is the right trade for a CLI that makes one or two
    requests and exits, and it removes a whole class of "the daemon restarted
    under our keep-alive" failures.
    """

    def __init__(
        self,
        base: Path | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        auto_start: bool | None = None,
        no_restart: bool = False,
        token: str | None = None,
    ) -> None:
        self.paths = TlgrPaths(base)
        self.timeout = timeout
        self._auto_start = auto_start
        self._no_restart = no_restart
        self._token = token
        self._ready = False
        self._restarted = False

    # -- configuration -----------------------------------------------------

    @property
    def auto_start(self) -> bool:
        if self._auto_start is not None:
            return self._auto_start
        try:
            from tlgr.core.config import load_app_config

            return bool(load_app_config(self.paths.base).daemon.auto_start)
        except Exception:
            return True

    @property
    def start_timeout(self) -> float:
        try:
            from tlgr.core.config import load_app_config

            return float(load_app_config(self.paths.base).daemon.start_timeout)
        except Exception:
            return 30.0

    def token(self) -> str | None:
        if self._token is not None:
            return self._token
        env = os.environ.get("TLGR_IPC_TOKEN", "").strip()
        if env:
            self._token = env
            return env
        try:
            self._token = self.paths.token.read_text().strip() or None
        except OSError:
            self._token = None
        return self._token

    def _headers(self, request_id: str, *, body: bool) -> dict[str, str]:
        headers = {
            HEADER_CLIENT: f"tlgr/{VERSION}",
            HEADER_PROTOCOL: str(PROTOCOL),
            HEADER_REQUEST_ID: request_id,
            "Accept": "application/json, application/x-ndjson",
            "Connection": "close",
        }
        if body:
            headers["Content-Type"] = "application/json"
        token = self.token()
        if token:
            headers[HEADER_TOKEN] = token
        return headers

    # -- raw plumbing ------------------------------------------------------

    def _open(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        timeout: float | None = None,
        request_id: str = "",
    ) -> tuple[_UnixHTTPConnection, http.client.HTTPResponse]:
        conn = _UnixHTTPConnection(str(self.paths.socket), timeout or self.timeout)
        try:
            conn.request(
                method, path, body=body, headers=self._headers(request_id, body=body is not None)
            )
            response = conn.getresponse()
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            conn.close()
            raise DaemonNotRunningError(f"cannot connect to the daemon socket: {exc}") from exc
        except TimeoutError as exc:
            conn.close()
            raise RetryableError(
                f"the daemon did not answer within {timeout or self.timeout:g}s"
            ) from exc
        except BaseException:
            conn.close()
            raise
        return conn, response

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        idempotent: bool | None = None,
        request_id: str = "",
    ) -> Any:
        """One request, one decoded JSON body. Raises the daemon's error.

        A connection that breaks *before* a reply is one retry for an
        idempotent request (a GET, or an op that declares itself idempotent):
        the daemon restarting between our connect and our write is common and
        is not the caller's problem. A request that reached the daemon is
        never retried — replaying a send is worse than reporting a failure.
        """
        if idempotent is None:
            idempotent = method.upper() in ("GET", "HEAD")
        target = _with_query(path, params)
        payload = _encode_body(body)
        request_id = request_id or _request_id()

        attempts = 2 if idempotent else 1
        last: Exception | None = None
        for attempt in range(attempts):
            self._ensure_daemon()
            try:
                conn, response = self._open(
                    method, target, body=payload, timeout=timeout, request_id=request_id
                )
            except (DaemonNotRunningError, ConnectionError, http.client.HTTPException) as exc:
                last = exc
                self._ready = False
                if attempt + 1 < attempts:
                    continue
                if isinstance(exc, TlgrError):
                    raise
                raise IPCError(f"the daemon connection broke before it answered: {exc}") from exc
            try:
                raw = response.read()
            except (http.client.IncompleteRead, ConnectionResetError, OSError) as exc:
                conn.close()
                last = exc
                self._ready = False
                if attempt + 1 < attempts:
                    continue
                raise IPCError(f"the daemon closed the connection mid-reply: {exc}") from exc
            finally:
                conn.close()
            return _decode(raw, response.status)
        raise IPCError(str(last) if last else "request failed")

    def stream(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield NDJSON frames until the daemon closes the response.

        A stream that ends without an `end` frame is `RETRYABLE`, never a
        silent success: "the connection dropped after 400 of 4,120 items" and
        "there were 400 items" are different facts (§5.3).
        """
        self._ensure_daemon()
        conn, response = self._open(
            method,
            _with_query(path, params),
            body=_encode_body(body),
            timeout=timeout if timeout is not None else STREAM_TIMEOUT,
            request_id=_request_id(),
        )
        try:
            if response.status >= 400:
                yield from _error_frames(response.read(), response.status)
                return
            saw_end = False
            while True:
                line = response.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                frame = parse_frame(stripped)
                if frame.get("type") == "end":
                    saw_end = True
                yield frame
                if saw_end:
                    break
            if not saw_end:
                raise RetryableError(
                    "the daemon closed the stream without an end frame; "
                    "the result is incomplete — retry"
                )
        finally:
            conn.close()

    # -- protocol ----------------------------------------------------------

    def probe_status(self) -> dict[str, Any] | None:
        """`GET /v1/status`, or None when nothing is listening."""
        try:
            conn, response = self._open("GET", "/v1/status", timeout=5.0)
        except (DaemonNotRunningError, RetryableError):
            return None
        try:
            raw = response.read()
        except OSError:
            return None
        finally:
            conn.close()
        if response.status != 200:
            return None
        try:
            decoded = msgspec.json.decode(raw)
        except msgspec.DecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None

    def _ensure_daemon(self) -> None:
        """Start the daemon if needed and agree on a protocol, once per client."""
        if self._ready:
            return
        state = autostart.ensure_running(
            self.paths,
            self.probe_status,
            auto_start=self.auto_start,
            start_timeout=self.start_timeout,
        )
        if autostart.check_protocol(state, client_protocol=PROTOCOL) == -1:
            self._restart_older_daemon(state)
        self._ready = True

    def _restart_older_daemon(self, state: dict[str, Any]) -> None:
        daemon = state.get("daemon") or {}
        running = daemon.get("protocol", 0)
        if self._no_restart:
            from tlgr.core.errors import DaemonVersionMismatchError

            raise DaemonVersionMismatchError(
                f"the running daemon speaks protocol {running}, this CLI speaks {PROTOCOL}, "
                "and --no-daemon-restart was given"
            )
        if self._restarted:
            from tlgr.core.errors import DaemonVersionMismatchError

            raise DaemonVersionMismatchError(
                "restarted the daemon once and it still speaks an older protocol"
            )
        self._restarted = True
        managed = autostart.daemon_state(self.paths)
        if managed.supervised:
            from tlgr.core.errors import DaemonVersionMismatchError

            raise DaemonVersionMismatchError(
                f"the daemon is managed by {managed.managed_by}; restart it with "
                "tlgr daemon restart so the supervisor keeps ownership"
            )

        import sys

        print(
            f"daemon is running an older protocol ({running} < {PROTOCOL}); restarting it",
            file=sys.stderr,
        )
        # A stop that fails is not fatal here: the point is only to make the
        # old daemon let go, and the readiness poll below is the real check.
        with contextlib.suppress(TlgrError):
            self.request("POST", "/v1/admin/stop", body={"drain_s": 5}, timeout=10.0)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and self.probe_status() is not None:
            time.sleep(0.05)
        fresh = autostart.ensure_running(
            self.paths,
            self.probe_status,
            auto_start=True,
            start_timeout=self.start_timeout,
        )
        autostart.check_protocol(fresh, client_protocol=PROTOCOL)

    # -- the four verbs ----------------------------------------------------

    def op(
        self,
        op_id: str,
        request: Any = None,
        *,
        account: str = "",
        dry_run: bool = False,
        flood_wait_max: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        fetch_all: bool = False,
        idempotent: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """`POST /v1/op` — the decoded success envelope, or the daemon's error."""
        request_id = _request_id()
        body = OpRequest(
            op=op_id,
            account=account,
            request=_as_builtins(request),
            dry_run=dry_run,
            flood_wait_max=flood_wait_max,
            request_id=request_id,
            client_version=VERSION,
            protocol=PROTOCOL,
            limit=limit,
            cursor=cursor,
            all=fetch_all,
        )
        result = self.request(
            "POST",
            "/v1/op",
            body=body,
            timeout=timeout,
            idempotent=idempotent,
            request_id=request_id,
        )
        if not isinstance(result, dict):
            raise IPCError("the daemon returned a non-object envelope")
        return result

    def op_stream(
        self,
        op_id: str,
        request: Any = None,
        *,
        account: str = "",
        dry_run: bool = False,
        flood_wait_max: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        fetch_all: bool = False,
        timeout: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        body = OpRequest(
            op=op_id,
            account=account,
            request=_as_builtins(request),
            dry_run=dry_run,
            flood_wait_max=flood_wait_max,
            request_id=_request_id(),
            client_version=VERSION,
            protocol=PROTOCOL,
            stream=True,
            limit=limit,
            cursor=cursor,
            all=fetch_all,
        )
        return self.stream("POST", "/v1/op", body=body, timeout=timeout)

    def events(
        self,
        *,
        account: str,
        types: str = "",
        since: int | None = None,
        chats: str = "",
        timeout: int = 3600,
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"account": account, "timeout": timeout}
        if types:
            params["types"] = types
        if since is not None:
            params["since"] = since
        if chats:
            params["chats"] = chats
        return self.stream("GET", "/v1/events", params=params, timeout=timeout + 30)

    def status(self) -> dict[str, Any]:
        result = self.request("GET", "/v1/status", timeout=15.0)
        if not isinstance(result, dict):
            raise IPCError("the daemon returned a non-object status")
        return result

    def admin(self, action: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if "/" in action:
            raise UsageError(f"invalid admin action {action!r}")
        result = self.request("POST", f"/v1/admin/{action}", body=body or {})
        if not isinstance(result, dict):
            raise IPCError("the daemon returned a non-object admin reply")
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request_id() -> str:
    return uuid.uuid4().hex


def _encode_body(body: Any) -> bytes | None:
    if body is None:
        return None
    if isinstance(body, bytes):
        return body
    return msgspec.json.encode(body)


def _as_builtins(request: Any) -> dict[str, Any]:
    if request is None:
        return {}
    if isinstance(request, dict):
        return request
    converted = msgspec.to_builtins(request)
    return converted if isinstance(converted, dict) else {}


def _with_query(path: str, params: dict[str, Any] | None) -> str:
    """Append *params*, always through `urlencode`.

    This is COR-04's fix on the query side: `f"?chat={chat}"` with `chat` set
    to `سلام #12` produced a path the server split at the `#` and decoded as
    Latin-1. `urlencode` is the only spelling allowed in this file.
    """
    if not params:
        return path
    from urllib.parse import urlencode

    pairs = [(k, v) for k, v in params.items() if v is not None]
    if not pairs:
        return path
    query = urlencode([(k, _stringify(v)) for k, v in pairs])
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{query}"


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _decode(raw: bytes, status_code: int) -> Any:
    try:
        decoded = msgspec.json.decode(raw) if raw else None
    except msgspec.DecodeError as exc:
        if status_code >= 400:
            raise IPCError(f"daemon error ({status_code}): {raw[:200]!r}") from exc
        raise IPCError(f"the daemon returned a body that is not JSON: {raw[:200]!r}") from exc

    if status_code < 400:
        return decoded

    body: dict[str, Any] = {}
    if isinstance(decoded, dict):
        inner = decoded.get("error")
        body = inner if isinstance(inner, dict) else decoded
    raise error_from_body(body, status_code=status_code)


def _error_frames(raw: bytes, status_code: int) -> Iterator[dict[str, Any]]:
    try:
        _decode(raw, status_code)
    except TlgrError as exc:
        from tlgr.core.errors import classify, error_body_dict

        yield {"type": "end", "ok": False, "error": error_body_dict(classify(exc))}
        return
    yield {"type": "end", "ok": False, "error": {"code": "IPC_ERROR", "message": "stream failed"}}


# ---------------------------------------------------------------------------
# Module-level convenience — one shared client per process
# ---------------------------------------------------------------------------

_default: DaemonClient | None = None


def default_client(base: Path | None = None) -> DaemonClient:
    global _default
    if _default is None or (base is not None and _default.paths.base != Path(base)):
        _default = DaemonClient(base)
    return _default


def reset_default_client() -> None:
    """Drop the shared client. Tests that move `TLGR_HOME` need this."""
    global _default
    _default = None


def op(op_id: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    return default_client().op(op_id, request, **kwargs)


def stream(op_id: str, request: Any = None, **kwargs: Any) -> Iterator[dict[str, Any]]:
    return default_client().op_stream(op_id, request, **kwargs)


def events(**kwargs: Any) -> Iterator[dict[str, Any]]:
    return default_client().events(**kwargs)


def status() -> dict[str, Any]:
    return default_client().status()


def admin(action: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return default_client().admin(action, body)


#: Set once by the CLI root from `--flood-wait-max`. Legacy commands do not
#: thread the flag through their own bodies (there are forty of them), and
#: dropping it silently is COR-15 — the flag existed and did nothing.
_default_flood_wait_max: int | None = None


def set_default_flood_wait_max(seconds: int | None) -> None:
    global _default_flood_wait_max
    _default_flood_wait_max = seconds


def legacy_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    base: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """The v1 IPC call, over the v2 transport (§12.4).

    Unmigrated commands keep their route and their JSON shape, and gain
    correct encoding, real timeouts and the §7.2 error mapping on day one
    instead of at their own group's PR.
    """
    client = DaemonClient(base, timeout=timeout)
    if _default_flood_wait_max is not None:
        if body is not None:
            body = {"flood_wait_max": _default_flood_wait_max, **body}
        else:
            params = {**(params or {}), "flood_wait_max": _default_flood_wait_max}
    result = client.request(method, path, body=body, params=params, timeout=timeout)
    if result is None:
        return {}
    if not isinstance(result, dict):
        return {"result": result}
    return result


# ---------------------------------------------------------------------------
# The CLI dispatcher
# ---------------------------------------------------------------------------


def make_dispatcher(base: Path | None = None) -> Any:
    """Build the callable `cli.gen.set_dispatcher()` wants.

    `run_op` has already resolved the account, enforced `--enable-commands`
    by canonical id and short-circuited `--dry-run`; all that is left is the
    round trip. Keeping it that thin is what makes the CLI testable without a
    daemon and the daemon testable without a CLI.
    """

    def dispatch(spec: Any, request: Any, state: Any) -> dict[str, Any]:
        client = DaemonClient(
            base,
            timeout=float(state.timeout) if state.timeout else float(spec.timeout_s),
            no_restart=bool(state.no_daemon_restart),
        )
        common: dict[str, Any] = {
            "account": state.account or "",
            "dry_run": bool(state.dry_run),
            "flood_wait_max": state.flood_wait_max,
            "limit": getattr(state, "limit", None),
            "cursor": getattr(state, "cursor", None),
            "fetch_all": bool(getattr(state, "fetch_all", False)),
        }
        if spec.stream or common["fetch_all"]:
            return _collect(client.op_stream(spec.id, request, **common), spec.id)
        return client.op(spec.id, request, idempotent=spec.idempotent, **common)

    return dispatch


def _collect(frames: Iterator[dict[str, Any]], op_id: str) -> dict[str, Any]:
    """Fold an NDJSON walk back into one envelope for the renderer."""
    items: list[Any] = []
    meta: dict[str, Any] = {}
    page: dict[str, Any] | None = None
    account: str | None = None
    for frame in frames:
        kind = frame.get("type")
        if kind == "meta":
            account = frame.get("account") or None
            meta["request_id"] = frame.get("request_id", "")
        elif kind == "item":
            items.append(frame.get("data"))
        elif kind == "page":
            page = {
                "has_more": bool(frame.get("has_more")),
                "next_cursor": frame.get("next_cursor"),
            }
        elif kind == "end":
            if not frame.get("ok", True):
                raise error_from_body(frame.get("error") or {})
            meta["elapsed_ms"] = frame.get("elapsed_ms", 0)
            meta["count"] = frame.get("count", len(items))
    envelope: dict[str, Any] = {"ok": True, "op": op_id, "result": items, "meta": meta}
    if account:
        envelope["account"] = account
    if page is not None:
        envelope["page"] = page
    return envelope
