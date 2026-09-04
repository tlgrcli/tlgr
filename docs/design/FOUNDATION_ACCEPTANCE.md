# Foundation acceptance — ARCHITECTURE §12.3, criterion by criterion

The twenty criteria the foundation PR is measured against, each recorded as
**met** (with the thing that proves it), **partly met** (with what is missing),
or **needs a live account** (a claim no fake Telegram can settle).

Run `make acceptance` for the subset of the suite these entries name; run
`make check` for everything. Nothing below is asserted by hand: every "met"
line points at a test name or a make target that fails when the claim stops
being true.

Re-run against the final tree at 2.0.0 (PR-12, `feat/pr12-settings`):
**13 292 tests**, all passing; **678 operations** in the registry, **951
command paths** and **267 aliases**; coverage **82 %**. The two criteria that
were partly met when the foundation landed — §1 and §17 — are met now, and
the reason is recorded under each.

| # | Criterion | Status | Proof |
|---|---|---|---|
| 1 | suite green, ≥ 80 % coverage, the pre-existing tests still pass | met | `make test` — 13 292 passed, 82 % (see §1) |
| 2 | `mypy --strict` on the v2 set, `ruff check`, `ruff format --check` clean | met | `make lint typecheck` |
| 3 | the ten `message` verbs and three `draft` verbs generated, `--json` compatible with `AGENT.md` | met | `tests/test_agentmd_compat.py` |
| 4 | globals work anywhere on the line | met | `test_registry_contract.py::TestOperationContract::test_globals_attached_after_the_arguments` |
| 5 | a Persian search succeeds against a live daemon (COR-04) | met | `test_ops_message.py::TestListAndSearch::test_a_non_ascii_query_survives_the_wire` |
| 6 | socket `srw-------`, foreign uid refused, audit refuses a 0644 session | met | `test_security.py::TestSocket`, `::TestPermissionAudit` |
| 7 | 20 concurrent spawns produce exactly one daemon | met | `test_daemon_lifecycle.py::TestAutoStart::test_twenty_concurrent_probes_spawn_exactly_one_daemon` |
| 8 | a dropped connection is `degraded`, exit 8 with a hint, recovery runs `catch_up()` | met (simulated) | `test_account_session.py::TestLifecycle::test_a_drop_becomes_degraded_and_then_reconnects`, `::TestRequestGate::test_a_degraded_account_is_retryable_with_a_hint` |
| 9 | a revoked session moves to `needs_login`, status says why, exit 4 | met (simulated) | `test_account_session.py::TestLifecycle::test_an_unauthorised_account_is_terminal`, `::TestRequestGate::test_a_revoked_account_is_session_error` |
| 10 | `message list --all` streams NDJSON, terminates, respects the read rate | met | `test_stream.py::TestWalk`, `::TestFraming`, `::TestOverTheSocket` |
| 11 | `/v1/events`: delivery, 15 s heartbeats, replay from `since`, `gap` | met | `test_events.py::test_the_events_endpoint_delivers_replays_and_heartbeats`, `::TestReplay` |
| 12 | a send through `/v1/op` produces `self_origin: true` | met | `test_events.py::TestSelfOrigin` |
| 13 | webhook delivers a real payload with a valid HMAC, dead-letters 0600 | met | `test_webhook.py::TestDelivery`, `::TestFailure` |
| 14 | every row of the §7.2 error table reproduced end to end | met | `test_errors_map.py::TestTable` (every row) + `test_ops_message.py::TestTheErrorTableEndToEnd` (raise → socket → exit code) |
| 15 | `tlgr schema --json` is draft 2020-12, with request+response+example per op | met | `test_schema.py::TestDocument`, `test_registry_contract.py::TestOperationContract::test_schema_generates` |
| 16 | `make docs` and `make parity` produce no diff | met | `test_docs_fresh.py` |
| 17 | `messages_core` ≥ 95 % with the rest waived and named | met | 167 of 167 covered (see §17) |
| 18 | a policy-blocked op exits 6 from the daemon, alias form included | met | `test_dispatch.py::test_the_policy_is_checked_by_canonical_id_including_aliases`, `test_sandbox.py` |
| 19 | a protocol upgrade triggers exactly one restart; `--no-daemon-restart` is exit 11 | met | `test_daemon_lifecycle.py::TestHandshake` |
| 20 | `daemon stop` drains an in-flight request | met | `test_daemon_lifecycle.py::TestShutdown::test_it_waits_for_an_in_flight_request`, `::test_the_drain_deadline_is_respected` |

**Met: 20. Partly met: 0. Needs a live account: 0** — with the caveat on §8/§9
below, which are met against a fake that simulates the failure rather than a
real network.

---

## §1 — suite, coverage, and the pre-existing tests

Green: **13 292 tests**, coverage **82 %**, over the 80 % gate. When the
foundation landed this was 77 % and recorded as a debt; what closed it was
behavioural tests for the P2/P3 tails, one group at a time, rather than a
lowered number.

Three test modules from before the foundation are gone, and they are the only
ones: `test_media_only_messages.py`, `test_message_reactions.py` and
`test_service_messages.py` drove `ClientWrapper.get_messages`, which PR-12
deleted. That surface is `message list` and `tests/test_ops_message.py` owns
it. The claim those files were really making — that a caption-less media
message is classified by its attributes rather than by its TL class name —
did not go with them: its case table moved onto `media_summary`, in
`tests/test_media_kind.py` and `tests/test_serialize.py`.

What is still uncovered is concentrated and named:

| Module | Cov | Why |
|---|---|---|
| `core/launchd.py`, `core/systemd.py` | 0 % | they write a plist or a unit and then ask the OS to load it; the write is covered by `test_daemon_lifecycle.py`, the load is not testable in CI |
| `__main__.py`, `daemon/main.py`, `core/process.py` | 0–38 % | process entry points: fork, setsid, signal handlers |
| `ops/proxy.py` | 22 % | the proxy group probes real network paths; the request-building half has contract tests, the probing half needs a socket to somewhere |
| `gateway/engine.py`, `filters/message.py` | 43–52 % | the job engine, which no command surface runs through and which keeps its v1 shape |

`tlgr/cli/legacy/*` is no longer omitted from the measurement, because there
is no `tlgr/cli/legacy`.

## §3 — the documented surface

`test_agentmd_compat.py` holds two promises at once:

* every command path v1's `AGENT.md` documents is still invocable *and* still
  resolves to an operation — `tlgr send`, `tlgr msg list`, `tlgr message
  react`, `tlgr profile get` and the rest, after every hand-written module was
  deleted rather than shadowed;
* every JSON key it documents is still in the response model and in the
  published example, unless it is one of the deliberate changes — and each of
  those has to name its operation in `CHANGELOG.md`, so the table and the
  changelog cannot drift.

The additions §12.3 permits are there: `date` is RFC-3339 with a `date_unix`
sibling, and media keys are additionally available under `media`.

## §8, §9 — connection failure

Both are met against `tests/fake_telethon.py`, which can drop a connection
(`world.disconnect_after`) and can refuse authorisation, so the state machine,
the backoff, the `catch_up()` after reconnect, and the exit codes the request
gate hands back are all exercised. What is *not* exercised is a real
60-second network outage against real Telegram — the timing, and whether
Telethon's own reconnect races ours. That is a soak test against a live
account, and it stays outside the suite.

PR-12 moved where the claim is made. `Daemon.status()` — v1's
`/daemon/status` body, and the thing COR-37 was first written against — went
with `ClientWrapper`. The distinction it existed to draw, between a client
object existing and the link being usable, is now `AccountSession.connected`
and the per-account `state` that `daemon status` answers from;
`tests/test_daemon_connection_health.py` makes it there.

## §17 — parity

`tlgr agent parity --json` reports `messages_core` at **100 %**: 167 of 167,
nothing waived. When the foundation landed it was 79.6 %, and the 34 uncovered
ids were catalogued under `messages_core` because they concern messages while
their command home was another noun — `chat`, `poll`, `reaction`, `search`.
Every one of those PRs landed, so the domain boundary artefact resolved
itself, which is what "100 % accounted" was measuring all along.

The whole catalog now stands at **1788 of 1797** (99.5 %) and **all 178 P0**
behaviours, which is ARCHITECTURE §1.3's condition for 2.0.0 final. The nine
that remain are individually waived, each naming the MTProto method this
build has no request class for: seven are layer-229 `ephemeral.*` and
rich-message-keyboard constructors, two are methods the layer has and
Telethon 1.44 does not ship. Every one is registered as a command that exits
13 (`NOT_SUPPORTED`) naming the method, so "unavailable in this build" is a
different answer from "no such command".

The gate that keeps it honest is stronger than the floor it started as.
`tests/test_parity.py` still names every P0 id each group claims and asserts
the named set is *exactly* what the registry claims, so coverage cannot be
silently traded away. PR-12 added four more: no domain may be waived
wholesale, every waiver must give one of four permanent reasons, a
layer/method waiver must name its method, and an id that is covered may not
also be waived — which is the only way a number could have lied about itself.

The restatement recorded in `DECISIONS.md` — "100 % accounted, and every P0
the group owns covered" — is no longer a weaker reading of the criterion. It
and the literal reading now agree.
