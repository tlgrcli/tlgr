"""The `job` group: the gateway's rules, as data rather than as an editor.

v1's `job add` opened `$EDITOR` on `jobs.yaml`. That is a perfectly good way
for a person to write a rule and a completely useless one for an agent, which
is the caller this whole product is for — so the flag form is the primary path
and `--edit` keeps the old behaviour.

The other change is what a job can hear. v1 jobs only ever saw `NewMessage`,
because the engine registered Telethon's high-level handlers directly. A job
now declares `events:` from the same taxonomy `watch` and `webhook set` use,
and subscribes to the same bus — so "which events exist" has one answer across
the three places that ask.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

from tlgr.core import eventtypes
from tlgr.core.errors import EXIT_EMPTY, NotFoundError, UsageError
from tlgr.core.pagination import PageKind, build_page
from tlgr.models.base import Request
from tlgr.models.daemon import Job, JobState, JobTestFrame
from tlgr.models.page import Page
from tlgr.models.peer import PeerRef
from tlgr.ops._params import arg, opt
from tlgr.ops._spec import OpContext, OperationSpec, Surface

__all__ = [name for name in dir() if name.startswith("SPEC_")]


# ---------------------------------------------------------------------------
# jobs.yaml
# ---------------------------------------------------------------------------


def _jobs_path() -> Path:
    from tlgr.core.paths import TlgrPaths

    return TlgrPaths().jobs


def _load_raw() -> dict[str, Any]:
    """Read `jobs.yaml` as plain data, so a rewrite keeps what it did not touch.

    Round-tripping through `GatewayConfig` would silently drop every filter and
    processor the parser does not model, which is how an "add a job" command
    quietly deletes the four already there.
    """
    import yaml

    path = _jobs_path()
    if not path.exists():
        return {"jobs": []}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise UsageError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise UsageError(f"{path} must be a mapping with a `jobs:` list")
    loaded.setdefault("jobs", [])
    if not isinstance(loaded["jobs"], list):
        raise UsageError(f"{path}: `jobs` must be a list")
    return loaded


def _save_raw(document: dict[str, Any]) -> None:
    import yaml

    from tlgr.core.paths import write_private

    write_private(
        _jobs_path(),
        yaml.safe_dump(document, default_flow_style=False, sort_keys=False, allow_unicode=True),
    )


def _find(document: dict[str, Any], name: str) -> dict[str, Any]:
    for entry in document["jobs"]:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    raise NotFoundError(f"no job named {name!r}. Run: tlgr job list")


def _runner(ctx: OpContext) -> Any:
    daemon = getattr(ctx, "daemon", None)
    if daemon is None:
        raise UsageError("this operation runs inside the daemon")
    return daemon


# ---------------------------------------------------------------------------
# job list / get
# ---------------------------------------------------------------------------


class JobListReq(Request):
    enabled_only: Annotated[bool, opt("--enabled-only", help="Hide disabled jobs.")] = False


async def job_list(ctx: OpContext, req: JobListReq) -> Page[JobState]:
    """Every configured job, with what the running engine has done with it."""
    daemon = _runner(ctx)
    live = {row.get("name"): row for row in daemon.list_jobs()}
    document = _load_raw()
    rows: list[JobState] = []
    for entry in document["jobs"]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        running = live.get(name, {})
        enabled = bool(entry.get("enabled", True))
        if req.enabled_only and not enabled:
            continue
        account = str(entry.get("account", ""))
        if ctx.account and ctx.account != "all" and account and account != ctx.account:
            continue
        rows.append(
            JobState(
                name=name,
                account=account,
                enabled=enabled,
                running=bool(running.get("running")),
                events=[str(e) for e in (entry.get("events") or ["new_message"])],
                matched=int(running.get("matched") or 0),
                skipped=int(running.get("skipped") or 0),
                errors=int(running.get("errors") or 0),
            )
        )
    return build_page(
        rows,
        op="job.list",
        kind=PageKind.LOCAL,
        has_more=False,
        total=len(rows),
    )


SPEC_JOB_LIST = OperationSpec(
    id="job.list",
    request=JobListReq,
    response=Page[JobState],
    impl=job_list,
    summary="List gateway jobs and their state",
    description=(
        "Configured *and* running are different facts: a job can be enabled "
        "in `jobs.yaml` and not running because its account will not connect."
    ),
    legacy_paths=("job list",),
    paginated=PageKind.LOCAL,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    idempotent=True,
    rate_class="local",
    timeout_s=30,
    columns=("name", "account", "enabled", "running", "matched", "errors"),
    example={
        "items": [
            {
                "name": "archive",
                "account": "work",
                "enabled": True,
                "running": True,
                "events": ["new_message"],
            }
        ],
        "has_more": False,
    },
    example_args="job list",
    covers_partial=("updates.stream-event-filtering",),
    coverage_note="lists the rules; proving one fires is `job test`.",
    tags=frozenset({"agent-safe"}),
)


class JobGetReq(Request):
    name: Annotated[str, arg(0, metavar="NAME")]
    explain: Annotated[
        bool, opt("--explain", help="Annotate each filter with the registry entry it resolves to.")
    ] = False


async def job_get(ctx: OpContext, req: JobGetReq) -> JobState:
    """One job's resolved pipeline: filters, processors, actions."""
    entry = _find(_load_raw(), req.name)
    state = JobState(
        name=req.name,
        account=str(entry.get("account", "")),
        enabled=bool(entry.get("enabled", True)),
        events=[str(e) for e in (entry.get("events") or ["new_message"])],
        filters=entry.get("filters") or {},
        processors=[str(p) for p in (entry.get("processors") or [])],
        actions=[a for a in (entry.get("actions") or []) if isinstance(a, dict)],
    )
    if req.explain:
        state.filters = {
            key: {"value": value, "resolves_to": _explain_filter(key)}
            for key, value in (state.filters or {}).items()
        }
    return state


def _explain_filter(name: str) -> str:
    """Which filter implementation a key resolves to, or that it resolves to none.

    "This job never fires" is almost always a filter name nobody registered,
    and the pipeline's silence about it is the reason it takes an hour to find.
    """
    with contextlib.suppress(Exception):
        from tlgr.filters import get_filter

        found = get_filter(name)
        if found is not None:
            return getattr(found, "__name__", str(found))
    return "UNKNOWN — no filter is registered under this name; the job will never match"


SPEC_JOB_GET = OperationSpec(
    id="job.get",
    request=JobGetReq,
    response=JobState,
    impl=job_get,
    summary="Show one job's resolved pipeline (filters, processors, actions)",
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    idempotent=True,
    rate_class="local",
    timeout_s=15,
    example={
        "name": "archive",
        "account": "work",
        "enabled": True,
        "events": ["new_message"],
        "actions": [{"forward": {"to": "@archive"}}],
    },
    example_args="job get archive --explain",
    covers_partial=("updates.stream-event-filtering",),
    coverage_note="shows the rule; evaluating it against real events is `job test`.",
    empty_exit=EXIT_EMPTY,
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# job add
# ---------------------------------------------------------------------------


def _pairs(values: list[str], what: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for value in values:
        key, sep, raw = value.partition("=")
        if not sep:
            raise UsageError(f"--{what} wants key=value, got {value!r}", field=what)
        out[key.strip()] = _coerce(raw.strip())
    return out


def _coerce(raw: str) -> Any:
    lowered = raw.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    with contextlib.suppress(ValueError):
        return int(raw)
    if raw.startswith(("[", "{")):
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(raw)
    return raw


def _action(spec: str) -> dict[str, Any]:
    """`reply:hello` or `forward:to=@archive` → one action entry."""
    name, sep, rest = spec.partition(":")
    name = name.strip()
    if not name:
        raise UsageError(f"--action wants NAME[:CONFIG], got {spec!r}", field="action")
    if not sep or not rest:
        return {name: {}}
    if "=" in rest:
        return {name: _pairs([part for part in rest.split(",") if part], "action")}
    return {name: rest}


class JobAddReq(Request):
    name: Annotated[str | None, opt("--name", metavar="NAME", help="Job name.")] = None
    from_file: Annotated[
        str | None,
        opt("--from-file", metavar="PATH", help="Read one job (or a jobs list) from YAML/JSON."),
    ] = None
    job_account: Annotated[
        str | None, opt("--for-account", metavar="ALIAS", help="Account the job runs on.")
    ] = None
    events: Annotated[
        str, opt("--events", metavar="TYPES", help="Event types the job subscribes to.")
    ] = "new_message"
    filter: Annotated[
        list[str], opt("--filter", metavar="KEY=VALUE", help="Filter entry (repeatable).")
    ] = []
    action: Annotated[
        list[str],
        opt("--action", metavar="SPEC", help="Action entry, e.g. 'reply:hello' (repeatable)."),
    ] = []
    processor: Annotated[
        list[str], opt("--processor", metavar="NAME", help="Processor entry (repeatable).")
    ] = []
    enabled: Annotated[bool, opt("--enabled/--disabled", help="Initial state.")] = True
    edit: Annotated[
        bool, opt("--edit", help="Open jobs.yaml in $EDITOR instead (the v1 behaviour).")
    ] = False


async def job_add(ctx: OpContext, req: JobAddReq) -> Job:
    """Add a job from flags, a file, or an editor.

    The event names are validated here rather than at load time: `jobs.yaml`
    parsing drops an event it does not recognise, so a typo used to produce a
    job that simply never fired and never said why.
    """
    if req.edit:
        _open_editor()
        return Job(name=req.name or "", already=True)

    document = _load_raw()
    entries = _entries_from(req)
    added: list[str] = []
    for entry in entries:
        name = str(entry.get("name", ""))
        if not name:
            raise UsageError("a job needs a name (--name, or `name:` in the file)", field="name")
        if any(isinstance(e, dict) and e.get("name") == name for e in document["jobs"]):
            raise UsageError(f"a job named {name!r} already exists; remove it first", field="name")
        eventtypes.resolve_selectors(entry.get("events") or ["new_message"])
        document["jobs"].append(entry)
        added.append(name)

    _save_raw(document)
    first = entries[0]
    return Job(
        name=str(first.get("name", "")),
        account=str(first.get("account", "")),
        enabled=bool(first.get("enabled", True)),
        events=[str(e) for e in (first.get("events") or [])],
        added=added,
    )


def _entries_from(req: JobAddReq) -> list[dict[str, Any]]:
    if req.from_file:
        return _entries_from_file(req.from_file)
    entry: dict[str, Any] = {
        "name": req.name,
        "events": [e.strip() for e in req.events.split(",") if e.strip()],
        "enabled": req.enabled,
    }
    if req.job_account:
        entry["account"] = req.job_account
    if req.filter:
        entry["filters"] = _pairs(req.filter, "filter")
    if req.processor:
        entry["processors"] = list(req.processor)
    if req.action:
        entry["actions"] = [_action(spec) for spec in req.action]
    if not entry.get("actions"):
        raise UsageError("a job with no actions would do nothing; pass --action", field="action")
    return [entry]


def _entries_from_file(source: str) -> list[dict[str, Any]]:
    import sys

    import yaml

    if source == "-":
        if sys.stdin is None or sys.stdin.isatty():
            raise UsageError("--from-file - was given but stdin is a terminal", field="from_file")
        text = sys.stdin.read()
    else:
        try:
            text = Path(source).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"{source}: {exc.strerror or exc}", field="from_file") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise UsageError(f"{source} is not valid YAML or JSON: {exc}", field="from_file") from exc
    if isinstance(loaded, dict) and isinstance(loaded.get("jobs"), list):
        return [entry for entry in loaded["jobs"] if isinstance(entry, dict)]
    if isinstance(loaded, dict):
        return [loaded]
    if isinstance(loaded, list):
        return [entry for entry in loaded if isinstance(entry, dict)]
    raise UsageError(f"{source} does not contain a job", field="from_file")


def _open_editor() -> None:
    path = _jobs_path()
    if not path.exists():
        from tlgr.core.paths import write_private

        write_private(
            path,
            "# Gateway jobs. See `tlgr job add --help` for the non-interactive form.\njobs: []\n",
        )
    os.execlp(os.environ.get("EDITOR", "vi"), os.environ.get("EDITOR", "vi"), str(path))


SPEC_JOB_ADD = OperationSpec(
    id="job.add",
    request=JobAddReq,
    response=Job,
    impl=job_add,
    summary="Add a gateway job",
    description=(
        "v1 only opened `$EDITOR`, which no agent can drive. The flags are "
        "the agent path, `--from-file -` takes YAML or JSON on stdin, and "
        "`--edit` keeps the old behaviour."
    ),
    legacy_paths=("job add",),
    mutating=True,
    needs_account=False,
    needs_auth=False,
    needs_client=False,
    surface=Surface.LOCAL,
    rate_class="local",
    timeout_s=30,
    example={"name": "archive", "account": "work", "enabled": True, "added": ["archive"]},
    example_args="job add --name archive --action 'forward:to=@archive'",
    covers_partial=("updates.stream-event-filtering",),
    coverage_note="writes the rule; the filter vocabulary belongs to the gateway.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# enable / disable / remove / reload
# ---------------------------------------------------------------------------


async def _set_enabled(ctx: OpContext, name: str, enabled: bool) -> Job:
    document = _load_raw()
    entry = _find(document, name)
    if bool(entry.get("enabled", True)) == enabled:
        ctx.mark_already()
        return Job(name=name, enabled=enabled, already=True)
    entry["enabled"] = enabled
    _save_raw(document)
    daemon = getattr(ctx, "daemon", None)
    if daemon is not None:
        with contextlib.suppress(Exception):
            await (daemon.enable_job(name) if enabled else daemon.disable_job(name))
    return Job(name=name, enabled=enabled, reloaded=daemon is not None)


class JobNameReq(Request):
    name: Annotated[str, arg(0, metavar="NAME")]


async def job_enable(ctx: OpContext, req: JobNameReq) -> Job:
    """Enable a disabled job, in `jobs.yaml` and in the running engine."""
    return await _set_enabled(ctx, req.name, True)


SPEC_JOB_ENABLE = OperationSpec(
    id="job.enable",
    request=JobNameReq,
    response=Job,
    impl=job_enable,
    summary="Enable a disabled job",
    legacy_paths=("job enable",),
    mutating=True,
    idempotent=True,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=30,
    example={"name": "archive", "enabled": True},
    example_args="job enable archive",
    covers_partial=("updates.stream-event-filtering",),
    coverage_note="toggles a rule; the filtering itself is the gateway's.",
    tags=frozenset({"agent-safe"}),
)


async def job_disable(ctx: OpContext, req: JobNameReq) -> Job:
    """Disable a job without removing it."""
    return await _set_enabled(ctx, req.name, False)


SPEC_JOB_DISABLE = OperationSpec(
    id="job.disable",
    request=JobNameReq,
    response=Job,
    impl=job_disable,
    summary="Disable a job without removing it",
    legacy_paths=("job disable",),
    mutating=True,
    idempotent=True,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=30,
    example={"name": "archive", "enabled": False},
    example_args="job disable archive",
    covers_partial=("updates.stream-event-filtering",),
    coverage_note="toggles a rule; the filtering itself is the gateway's.",
    tags=frozenset({"agent-safe"}),
)


async def job_remove(ctx: OpContext, req: JobNameReq) -> Job:
    """Remove a job from `jobs.yaml` and stop it."""
    document = _load_raw()
    _find(document, req.name)
    document["jobs"] = [
        entry
        for entry in document["jobs"]
        if not (isinstance(entry, dict) and entry.get("name") == req.name)
    ]
    _save_raw(document)
    daemon = getattr(ctx, "daemon", None)
    if daemon is not None:
        with contextlib.suppress(Exception):
            await daemon.remove_job(req.name)
    return Job(name=req.name, removed=True)


SPEC_JOB_REMOVE = OperationSpec(
    id="job.remove",
    request=JobNameReq,
    response=Job,
    impl=job_remove,
    summary="Remove a job",
    legacy_paths=("job remove",),
    mutating=True,
    destructive=True,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=30,
    example={"name": "archive", "removed": True},
    example_args="job remove archive",
    covers_partial=("updates.stream-event-filtering",),
    coverage_note="deletes a rule; the filtering itself is the gateway's.",
    tags=frozenset({"agent-safe"}),
)


class JobReloadReq(Request):
    validate_only: Annotated[
        bool, opt("--validate-only", help="Parse and report without swapping the pipeline.")
    ] = False


async def job_reload(ctx: OpContext, req: JobReloadReq) -> Job:
    """Re-read `jobs.yaml` and swap the running pipeline.

    `--validate-only` parses and reports without swapping, which is the check
    to run before a reload rather than after one: a config with a typo would
    otherwise take effect as "that job is gone".
    """
    from tlgr.gateway.config import load_gateway_configs

    daemon = _runner(ctx)
    base = getattr(getattr(daemon, "paths", None), "base", None)
    configs = load_gateway_configs(base)
    problems: list[str] = []
    for config in configs:
        if not config.name:
            problems.append("a job has no `name`")
        if not config.actions:
            problems.append(f"job {config.name!r} has no actions and would do nothing")
        for action in config.actions:
            from tlgr.actions import get_action

            if get_action(action.name) is None:
                problems.append(f"job {config.name!r} uses unknown action {action.name!r}")

    if req.validate_only or problems:
        return Job(name="", loaded=len(configs), errors=problems)

    result = await daemon.reload_jobs()
    return Job(
        name="",
        reloaded=True,
        loaded=len(configs),
        added=sorted(result.get("added", [])),
        removed_names=sorted(result.get("removed", [])),
        changed=sorted(result.get("updated", [])),
    )


SPEC_JOB_RELOAD = OperationSpec(
    id="job.reload",
    request=JobReloadReq,
    response=Job,
    impl=job_reload,
    summary="Hot-reload jobs.yaml without restarting the daemon",
    legacy_paths=("job reload",),
    mutating=True,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=60,
    example={"name": "", "reloaded": True, "loaded": 3, "added": ["archive"]},
    example_args="job reload --validate-only",
    covers_partial=("updates.stream-webhook-delivery",),
    coverage_note="reloads the consumers; delivery is the webhook pusher's.",
    tags=frozenset({"agent-safe"}),
)


# ---------------------------------------------------------------------------
# job test
# ---------------------------------------------------------------------------


class JobTestReq(Request):
    name: Annotated[str, arg(0, metavar="NAME")]
    event: Annotated[
        str | None,
        opt("--event", metavar="TYPE", help="Synthesise an event of this type instead."),
    ] = None
    since: Annotated[
        int | None, opt("--since", metavar="SEQ", help="Replay buffered events from this seq.")
    ] = None
    chat: Annotated[
        PeerRef | None, opt("--chat", metavar="CHAT", kind="peer", help="Restrict the replay.")
    ] = None
    from_file: Annotated[
        str | None, opt("--from-file", metavar="PATH", help="Feed envelopes from NDJSON.")
    ] = None
    run_actions: Annotated[
        bool,
        opt("--run-actions", help="Actually execute the actions instead of reporting them."),
    ] = False


async def job_test(ctx: OpContext, req: JobTestReq) -> AsyncIterator[dict[str, Any]]:
    """Feed events through one job's filters and report what it decided.

    `filter_trace` is the whole point. "The job never fires" is the commonest
    complaint about a rule engine and the hardest to diagnose, because a
    pipeline that silently drops an event looks exactly like an event that
    never arrived. Every filter node is named here, with the reason it passed
    or rejected.
    """
    entry = _find(_load_raw(), req.name)
    events = await _test_events(ctx, req)
    if not events:
        yield {
            "type": "note",
            "message": (
                "no events matched the selection; pass --event <type> to synthesise one, "
                "or --since <seq> to replay the buffer"
            ),
        }
        return

    wanted = eventtypes.resolve_selectors(entry.get("events") or ["new_message"])
    filters = entry.get("filters") or {}
    actions = [a for a in (entry.get("actions") or []) if isinstance(a, dict)]

    for index, event in enumerate(events, start=1):
        subscribed = event.get("type") in wanted
        trace, matched = _evaluate(filters, event)
        if not subscribed:
            trace.insert(0, f"events: {event.get('type')} is not subscribed — rejected")
            matched = False
        frame = JobTestFrame(
            seq=int(event.get("seq") or index),
            event=str(event.get("type", "")),
            matched=matched,
            filter_trace=trace,
            actions=[
                {"name": name, "would_do": config, "result": "not run (dry run)"}
                for action in actions
                for name, config in action.items()
            ]
            if matched
            else [],
        )
        from tlgr.models.base import to_builtins

        body = to_builtins(frame)
        yield {"type": "job-test", **(body if isinstance(body, dict) else {})}

    if req.run_actions:
        yield {
            "type": "note",
            "message": (
                "--run-actions is refused here: executing a rule's actions against real "
                "chats is `job enable` plus a live event, not a test"
            ),
        }


async def _test_events(ctx: OpContext, req: JobTestReq) -> list[dict[str, Any]]:
    from tlgr.models.base import to_builtins

    if req.from_file:
        return _events_from_file(req.from_file)
    if req.event:
        eventtypes.resolve_selectors(req.event, allow_all=False)
        return [{"type": req.event, "seq": 0, "payload": {}, "account": ctx.account}]

    bus = getattr(ctx, "bus", None)
    if bus is None:
        return []
    replayed, _gap = bus.replay(ctx.account, req.since if req.since is not None else 0)
    limit = int(getattr(ctx, "limit", None) or 20)
    out: list[dict[str, Any]] = []
    for event in replayed[:limit]:
        body = to_builtins(event)
        if isinstance(body, dict):
            out.append(body)
    return out


def _events_from_file(source: str) -> list[dict[str, Any]]:
    import sys

    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError as exc:
            raise UsageError(f"{source}: not NDJSON: {exc}", field="from_file") from exc
        if isinstance(loaded, dict):
            out.append(loaded)
    return out


def _evaluate(filters: dict[str, Any], event: dict[str, Any]) -> tuple[list[str], bool]:
    """Every filter key, and why it passed or rejected. Never a bare boolean."""
    if not filters:
        return ["(no filters — everything matches)"], True
    trace: list[str] = []
    matched = True
    payload = event.get("payload") or {}
    for key, expected in filters.items():
        actual = event.get(key, payload.get(key))
        ok = _compare(actual, expected)
        trace.append(
            f"{key}: {actual!r} {'==' if ok else '!='} {expected!r} — "
            f"{'passed' if ok else 'rejected'}"
        )
        matched = matched and ok
    return trace, matched


def _compare(actual: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return actual in expected
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.lower() in actual.lower()
    return bool(actual == expected)


SPEC_JOB_TEST = OperationSpec(
    id="job.test",
    request=JobTestReq,
    response=None,
    impl=job_test,
    summary="Dry-run a job's filters against real or synthetic events",
    description=(
        "`filter_trace` names every filter node and says why it passed or "
        "rejected, which is the missing piece when a job silently never "
        "fires. Actions are reported, never executed."
    ),
    stream=True,
    needs_account=False,
    needs_client=False,
    surface=Surface.DAEMON,
    rate_class="local",
    timeout_s=120,
    example={"type": "job-test", "event": "message_new", "matched": True},
    example_args="job test archive --event message_new",
    covers=("updates.stream-event-filtering",),
    tags=frozenset({"agent-safe", "frames", "live-stream"}),
)
