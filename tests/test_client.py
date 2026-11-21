"""`tlgr.Client`, the library face of the daemon.

The point of the client is that a script pays Python's startup once and then
only the round trip, so the first claims here are about what importing it
costs. The rest are the promises it makes to a caller that branches on its
answers: the CLI's envelope, the CLI's typed errors, and the CLI's guards,
none of which a library may quietly drop. Everything goes over a real Unix
socket to a real daemon with a fake Telegram behind it.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tlgr.core.errors import (
    EXIT_NOT_FOUND,
    EXIT_RATE_LIMITED,
    EXIT_SPAM_FLAGGED,
    AccountRequiredError,
    ConfigurationError,
    NotFoundError,
    PermissionError_,
    RateLimitError,
    SpamFlagError,
    UsageError,
)

ALICE = 4242
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tg(tlgr_home: Path):
    """A client for the temp home with auto-start off, as the transport tests use."""
    from tlgr.client import Client

    return Client("work", home=tlgr_home, auto_start=False, timeout=10.0)


@pytest.fixture
def alice(world):
    from fake_telethon import make_user

    world.add_user(make_user(ALICE, username="alice"))
    for index in range(1, 6):
        world.add_message(ALICE, f"message {index}", message_id=100 + index)
    return world


# ---------------------------------------------------------------------------
# What importing it costs
# ---------------------------------------------------------------------------

HEAVY = (
    "click",
    "telethon",
    "aiohttp",
    "tlgr.cli",
    "tlgr.ops",
    "tlgr.registry",
    "tlgr.daemon",
)


def _loaded_after(statement: str) -> list[str]:
    program = textwrap.dedent(
        f"""
        import sys
        {statement}
        heavy = {HEAVY!r}
        print(",".join(sorted(
            m for m in sys.modules if any(m == h or m.startswith(h + ".") for h in heavy)
        )))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [name for name in result.stdout.strip().split(",") if name]


@pytest.mark.parametrize(
    "statement",
    ["import tlgr.client", "from tlgr import Client", "from tlgr.client import Client"],
)
def test_importing_the_client_does_not_import_the_cli(statement: str):
    """The whole reason the module exists: no CLI, no registry, no Telethon."""
    assert _loaded_after(statement) == []


def test_import_tlgr_alone_does_not_build_the_client():
    """`Client` is resolved on first use, so the CLI's `import tlgr` stays bare."""
    program = "import sys, tlgr; print('tlgr.transport' in sys.modules, tlgr.__version__)"
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, cwd=str(ROOT), check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("False ")


def test_an_unknown_attribute_is_still_an_attribute_error():
    import tlgr

    with pytest.raises(AttributeError):
        _ = tlgr.NoSuchThing  # type: ignore[attr-defined]


def test_the_default_timeout_outlasts_every_operation():
    """A client that gives up before the daemon does reports "unknown" for a
    send the daemon then completes. The CLI reads each `timeout_s` from the
    registry; the client cannot afford to load it, so its single default has
    to cover the longest one, and this is what keeps it from drifting.
    """
    import tlgr.ops  # noqa: F401
    from tlgr.client import DEFAULT_TIMEOUT
    from tlgr.registry import REGISTRY

    longest = max(spec.timeout_s for spec in REGISTRY.values())
    assert longest <= DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_a_production_home_is_refused(tlgr_home: Path, monkeypatch: pytest.MonkeyPatch):
    from tlgr.client import Client

    monkeypatch.delenv("TLGR_ALLOW_PRODUCTION_HOME", raising=False)
    (tlgr_home / ".production").touch()
    with pytest.raises(ConfigurationError):
        Client("work", home=tlgr_home)
    with pytest.raises(ConfigurationError):
        Client("work")  # TLGR_HOME points at the same marked home


@pytest.mark.parametrize("alias", ["../etc", "a b", "x" * 65])
def test_a_bad_alias_is_refused_before_anything_connects(tlgr_home: Path, alias: str):
    from tlgr.client import Client

    with pytest.raises(UsageError):
        Client(alias, home=tlgr_home)
    client = Client(home=tlgr_home, auto_start=False)
    with pytest.raises(UsageError):
        client.op("profile.get", account=alias)


async def test_the_account_is_never_guessed(
    live_daemon, alice, tlgr_home: Path, in_thread, monkeypatch: pytest.MonkeyPatch
):
    """`TLGR_ACCOUNT` is set and `work` is the active alias, and still no.

    The CLI would resolve one of those. A script that forgot to name an
    account must not end up sending from whichever identity happened to be
    active: that is `require_account = true`, unconditionally.
    """
    from tlgr.client import Client

    monkeypatch.setenv("TLGR_ACCOUNT", "work")
    client = Client(home=tlgr_home, auto_start=False, timeout=10.0)
    with pytest.raises(AccountRequiredError):
        await in_thread(client.op, "message.send", {"chat": "@alice", "text": "hi"})
    assert alice.called("SendMessageRequest") == []


def test_enable_commands_is_enforced_before_the_request_leaves(
    tlgr_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """No daemon is running: a refusal that reached the socket would say so."""
    from tlgr.client import Client

    client = Client("work", home=tlgr_home, auto_start=False, enable_commands="chat.list")
    with pytest.raises(PermissionError_):
        client.op("message.send", {"chat": "@alice", "text": "hi"})
    with pytest.raises(PermissionError_):
        client.stream("message.list", {"chat": "@alice"})

    monkeypatch.setenv("TLGR_ENABLE_COMMANDS", "chat")
    from_env = Client("work", home=tlgr_home, auto_start=False)
    with pytest.raises(PermissionError_):
        from_env.op("message.send", {"chat": "@alice", "text": "hi"})
    from_env._check_policy("chat.list")  # a group name allows its members


# ---------------------------------------------------------------------------
# The envelope and the errors
# ---------------------------------------------------------------------------


async def test_op_returns_the_cli_envelope_and_the_send_happens(live_daemon, alice, tg, in_thread):
    envelope = await in_thread(tg.op, "message.send", {"chat": "@alice", "text": "hi"})
    assert envelope["ok"] is True
    assert envelope["op"] == "message.send"
    assert envelope["account"] == "work"
    assert envelope["result"]["text"] == "hi"
    assert "request_id" in envelope["meta"]
    assert len(alice.called("SendMessageRequest")) == 1


async def test_result_is_the_results_only_payload(live_daemon, alice, tg, in_thread):
    messages = await in_thread(tg.result, "message.list", {"chat": "@alice"}, limit=2)
    assert [m["id"] for m in messages] == [105, 104]


async def test_a_per_call_account_overrides_the_clients(live_daemon, alice, tg, in_thread):
    with pytest.raises(NotFoundError) as caught:
        await in_thread(tg.op, "profile.get", {}, account="nope")
    assert caught.value.exit_code == EXIT_NOT_FOUND


async def test_dry_run_changes_nothing(live_daemon, alice, tg, in_thread):
    envelope = await in_thread(
        tg.op, "message.send", {"chat": "@alice", "text": "hi"}, dry_run=True
    )
    assert envelope["result"]["dry_run"] is True
    assert envelope["meta"]["dry_run"] is True
    assert alice.called("SendMessageRequest") == []


async def test_a_flood_wait_is_a_rate_limit_error_with_its_wait(live_daemon, alice, tg, in_thread):
    alice.flood("SendMessageRequest", 42)
    with pytest.raises(RateLimitError) as caught:
        await in_thread(tg.op, "message.send", {"chat": "@alice", "text": "hi"}, flood_wait_max=0)
    assert caught.value.exit_code == EXIT_RATE_LIMITED
    assert caught.value.wait_seconds == 42


async def test_a_peer_flood_is_a_spam_flag_error(live_daemon, alice, tg, in_thread):
    import telethon.errors as tl_errors

    alice.fail_next("SendMessageRequest", tl_errors.PeerFloodError(request=None))
    with pytest.raises(SpamFlagError) as caught:
        await in_thread(tg.op, "message.send", {"chat": "@alice", "text": "hi"})
    assert caught.value.exit_code == EXIT_SPAM_FLAGGED
    assert caught.value.code == "PEER_FLOOD"


# ---------------------------------------------------------------------------
# Streams and walks
# ---------------------------------------------------------------------------


async def test_fetch_all_folds_the_walk_into_one_envelope(live_daemon, alice, tg, in_thread):
    envelope = await in_thread(tg.op, "message.list", {"chat": "@alice"}, fetch_all=True)
    assert envelope["ok"] is True
    assert sorted(m["id"] for m in envelope["result"]) == [101, 102, 103, 104, 105]
    assert envelope["meta"]["count"] == 5


async def test_stream_yields_frames_from_meta_to_end(live_daemon, alice, tg, in_thread):
    frames = await in_thread(
        lambda: list(tg.stream("message.list", {"chat": "@alice"}, fetch_all=True))
    )
    assert frames[0]["type"] == "meta"
    assert frames[-1]["type"] == "end" and frames[-1]["ok"] is True
    assert len([f for f in frames if f["type"] == "item"]) == 5


async def test_a_failed_stream_raises_its_classified_error(live_daemon, alice, tg, in_thread):
    """An error `end` frame is an exception, never a short, quiet success."""
    with pytest.raises(NotFoundError) as caught:
        await in_thread(lambda: list(tg.stream("profile.get", {}, account="nope")))
    assert caught.value.exit_code == EXIT_NOT_FOUND


async def test_op_on_a_streaming_operation_still_returns_an_envelope(
    live_daemon, tg, in_thread, tlgr_home: Path
):
    """The daemon streams a streaming op whether or not it was asked to.

    `DaemonClient.op` used to fail on that NDJSON body with "a body that is
    not JSON", so a caller had to know which operations stream. It folds it
    now, the way the CLI does.
    """
    logs = tlgr_home / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "daemon.log").write_text("first line\nsecond line\n", encoding="utf-8")

    envelope = await in_thread(tg.op, "daemon.logs", {"lines": 5})
    assert envelope["ok"] is True
    assert envelope["op"] == "daemon.logs"
    # `daemon.logs` writes its own `log` frames rather than `item` frames;
    # folding them away would answer an empty list for a log that has lines.
    assert [frame["type"] for frame in envelope["result"]] == ["log", "log"]


def test_a_folded_stream_without_an_end_frame_is_retryable():
    from tlgr.core.errors import RetryableError
    from tlgr.transport.client import _fold_ndjson

    with pytest.raises(RetryableError):
        _fold_ndjson(b'{"type":"meta"}\n{"type":"item","data":1}\n', "x.y")
    with pytest.raises(NotFoundError):
        _fold_ndjson(
            b'{"type":"meta"}\n{"type":"end","ok":false,'
            b'"error":{"code":"NOT_FOUND","message":"no","exit_code":5}}\n',
            "x.y",
        )
