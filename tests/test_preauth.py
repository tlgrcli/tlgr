"""Login, in the daemon, because the daemon owns the session files (§6.8).

Two things forced this out of the CLI. The daemon holds a `flock` on every
session file, so a CLI that opened one to log in would be the second writer —
the exact situation that earns `AUTH_KEY_DUPLICATED`. And Telethon keeps
`_phone_code_hash` in memory on the client, so a CLI that sent the code in one
process and signed in from another had lost it, which is why v1's
`account add` had to hold a process open while a human read their phone.

The CLI operations land in PR-2; this is the service they call.
"""

from __future__ import annotations

import pytest

from tlgr.core.errors import AuthPasswordRequiredError, UsageError
from tlgr.daemon.preauth import PreAuthService

# `asyncio_mode = "auto"` in pyproject collects the async tests; marking
# them explicitly would also mark the synchronous ones in this module.


@pytest.fixture
def preauth(daemon) -> PreAuthService:
    return daemon.preauth


class TestSendCode:
    async def test_it_returns_what_the_caller_needs_to_continue(self, preauth, stub_account):
        result = await preauth.send_code(stub_account, "+989123456789")
        assert result["account"] == stub_account
        assert result["timeout"] == 60
        assert result["expires_in"] == 600

    async def test_the_phone_number_is_masked_in_the_reply(self, preauth, stub_account):
        """It is in the reply, the log and anything that records either."""
        result = await preauth.send_code(stub_account, "+989123456789")
        assert "9123456789" not in result["phone"]
        assert "…" in result["phone"]

    async def test_the_hash_is_kept_server_side(self, preauth, stub_account):
        await preauth.send_code(stub_account, "+989123456789")
        assert preauth._pending[stub_account].phone_code_hash == "hash-6789"

    async def test_a_pending_login_counts_as_activity(self, preauth, stub_account):
        assert preauth.pending_count == 0
        await preauth.send_code(stub_account, "+989123456789")
        assert preauth.pending_count == 1


class TestVerify:
    async def test_verifying_without_sending_is_a_usage_error(self, preauth, stub_account):
        with pytest.raises(UsageError) as caught:
            await preauth.verify_code(stub_account, "12345")
        assert "send-code" in str(caught.value)

    async def test_a_successful_sign_in_records_the_account(self, preauth, stub_account, world):
        await preauth.send_code(stub_account, "+989123456789")
        result = await preauth.verify_code(stub_account, "12345")
        assert result["authorized"] is True
        assert result["user_id"] == world.me.id
        assert preauth.pending_count == 0

        stored = preauth.sessions.accounts.get_account(stub_account)
        assert stored.user_id == world.me.id
        assert stored.username == world.me.username

    async def test_it_releases_the_session_lock_for_the_supervisor(
        self, preauth, stub_account, daemon
    ):
        """The account must end up supervised, not living on the login client."""
        await preauth.send_code(stub_account, "+989123456789")
        await preauth.verify_code(stub_account, "12345")
        assert daemon.sessions.get(stub_account) is not None

    async def test_a_two_factor_account_asks_for_the_password(self, preauth, stub_account, world):
        from telethon.errors import SessionPasswordNeededError

        await preauth.send_code(stub_account, "+989123456789")
        world.fail_next("sign_in", SessionPasswordNeededError(None))
        with pytest.raises(AuthPasswordRequiredError) as caught:
            await preauth.verify_code(stub_account, "12345")
        assert "--password-env" in str(caught.value)

    async def test_a_login_never_creates_an_account(self, preauth, stub_account, world):
        """§1.2: a *login* never signs up; it stops and names the other command."""
        from telethon.errors import PhoneNumberUnoccupiedError

        await preauth.send_code(stub_account, "+989123456789")
        world.fail_next("sign_in", PhoneNumberUnoccupiedError(None))
        with pytest.raises(UsageError) as caught:
            await preauth.verify_code(stub_account, "12345")
        assert "auth sign-up" in str(caught.value)


class TestPassword:
    async def test_it_completes_the_sign_in(self, preauth, stub_account, world):
        await preauth.send_code(stub_account, "+989123456789")
        result = await preauth.password(stub_account, "hunter2")
        assert result["authorized"] is True
        sent = [payload for name, payload in world.calls if name == "sign_in"]
        assert sent[-1]["password"] == "hunter2"


class TestQr:
    async def test_it_streams_a_token_then_finishes(self, preauth, stub_account):
        """The login method that always works for a third-party api_id."""
        frames = [frame async for frame in preauth.qr(stub_account)]
        kinds = [frame["type"] for frame in frames]
        assert kinds[0] == "qr"
        assert frames[0]["url"].startswith("tg://login?token=")
        assert kinds[-1] == "done"

    async def test_a_two_factor_account_is_reported_rather_than_retried(
        self, preauth, stub_account, world
    ):
        from telethon.errors import SessionPasswordNeededError

        world.fail_next("qr_wait", SessionPasswordNeededError(None))
        frames = [frame async for frame in preauth.qr(stub_account)]
        assert frames[-1]["type"] == "password_required"


class TestCancel:
    async def test_an_abandoned_login_can_be_dropped(self, preauth, stub_account):
        await preauth.send_code(stub_account, "+989123456789")
        assert (await preauth.cancel(stub_account))["cancelled"] is True
        assert preauth.pending_count == 0

    async def test_cancelling_nothing_is_not_an_error(self, preauth, stub_account):
        assert (await preauth.cancel(stub_account))["cancelled"] is False

    async def test_an_expired_login_is_swept(self, preauth, stub_account, monkeypatch):
        """A human who walked away must not pin a session file for ever."""
        from tlgr.daemon import preauth as module

        await preauth.send_code(stub_account, "+989123456789")
        monkeypatch.setattr(module, "PENDING_TTL", -1)
        preauth._pending[stub_account].created -= 10000
        assert preauth.pending_count == 0
