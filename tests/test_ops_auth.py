"""The auth, account and passport operations, against a world that changes.

Three things this suite is really about.

* **Login is resumable.** `auth send-code` in one request and
  `auth verify-code` in another — with the `phone_code_hash` living in the
  daemon and mirrored to a 0600 file — is what makes an unattended login
  possible at all. If that ever regresses, the two-command flow silently
  becomes "no login is in progress".
* **A secret never widens.** The phone number is masked in every reply, the
  session export refuses to print without being told to, and no model has a
  field an SRP parameter could land in.
* **Terminating something actually terminates it.** `account session
  terminate` is asserted by listing afterwards, not by reading the reply:
  a fake that answered `{"terminated": 1}` while keeping the row would make
  the test pass and the command useless.
"""

from __future__ import annotations

import stat
from typing import Any

import pytest
from click.testing import CliRunner

from tlgr.core.errors import (
    EXIT_AUTH,
    EXIT_INDETERMINATE,
    EXIT_USAGE,
    classify,
)

API_ID = 12345
API_HASH = "0123456789abcdef0123456789abcdef"
PHONE = "+989123456789"


async def call(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("account", "work")
    return await in_thread(client.op, op, request, **kwargs)


async def result(client, in_thread, op: str, request: Any = None, **kwargs: Any) -> Any:
    return (await call(client, in_thread, op, request, **kwargs))["result"]


@pytest.fixture
def sessions(world):
    """Three devices: this one, a second phone, and an unconfirmed new login."""
    from fake_telethon import make_authorization, make_user, make_web_authorization

    world.auth.authorizations = [
        make_authorization(0, device="tlgr@host", app="tlgr", current=True),
        make_authorization(9021045, device="iPhone 15", app="Telegram iOS"),
        make_authorization(7001, device="Unknown", app="Telegram Desktop", unconfirmed=True),
    ]
    world.auth.web_authorizations = [make_web_authorization(770)]
    # The bot behind the web login, so `--block-bot` has something to resolve.
    world.add_user(make_user(4242, username="examplebot"))
    return world


@pytest.fixture
def cli_runner(tlgr_home):
    """A CliRunner pointed at an isolated `$TLGR_HOME`, for the local ops."""
    from tlgr.core.accounts import AccountManager

    manager = AccountManager(tlgr_home)
    manager.add_account("work")
    manager.add_account("spare")
    manager.update_account("work", user_id=4242, username="me", phone=PHONE)
    return CliRunner()


def run(runner: CliRunner, *args: str):
    from tlgr.cli import cli

    return runner.invoke(cli, list(args))


# ---------------------------------------------------------------------------
# auth send-code / verify-code — the resumable login
# ---------------------------------------------------------------------------


class TestSendCode:
    async def test_it_reports_the_code_type_and_the_hash(self, live_daemon, client, in_thread):
        sent = await result(
            client,
            in_thread,
            "auth.send-code",
            {"phone": PHONE, "alias": "newbie", "api_id": API_ID, "api_hash": API_HASH},
            account="",
        )
        assert sent["type"] == "app"
        assert sent["code_hash"] == "hash-abcd"
        assert sent["account"] == "newbie"

    async def test_the_phone_number_is_masked(self, live_daemon, client, in_thread):
        """It is in the reply, the log, and anything that records either."""
        sent = await result(
            client,
            in_thread,
            "auth.send-code",
            {"phone": PHONE, "alias": "newbie", "api_id": API_ID, "api_hash": API_HASH},
            account="",
        )
        assert "9123456789" not in sent["phone"]
        assert "…" in sent["phone"]

    async def test_the_pending_login_is_written_at_0600(
        self, live_daemon, client, in_thread, tlgr_home
    ):
        """The file is what lets `auth verify-code` run in another process."""
        await result(
            client,
            in_thread,
            "auth.send-code",
            {"phone": PHONE, "alias": "newbie", "api_id": API_ID, "api_hash": API_HASH},
            account="",
        )
        state = tlgr_home / "accounts" / "newbie" / "login-state.json"
        assert state.exists()
        assert stat.S_IMODE(state.stat().st_mode) == 0o600
        assert "hash-abcd" in state.read_text()

    async def test_a_recaptcha_demand_is_not_supported_rather_than_solved(
        self, live_daemon, client, in_thread
    ):
        """tlgr never solves a challenge; it says so and names the way round."""
        with pytest.raises(Exception) as caught:
            await result(
                client,
                in_thread,
                "auth.send-code",
                {"phone": PHONE, "alias": "work", "recaptcha_token": "x"},
                account="",
            )
        assert classify(caught.value).code == "NOT_SUPPORTED"


class TestVerifyCode:
    async def _start(self, client, in_thread):
        return await result(
            client,
            in_thread,
            "auth.send-code",
            {"phone": PHONE, "alias": "newbie", "api_id": API_ID, "api_hash": API_HASH},
            account="",
        )

    async def test_a_second_request_finishes_the_login(self, live_daemon, client, in_thread):
        await self._start(client, in_thread)
        done = await result(
            client, in_thread, "auth.verify-code", {"code": "12345", "alias": "newbie"}, account=""
        )
        assert done["status"] == "authorized"
        assert done["user_id"] == 777

    async def test_the_request_carries_the_stored_hash(self, live_daemon, client, in_thread, world):
        await self._start(client, in_thread)
        await result(
            client, in_thread, "auth.verify-code", {"code": "12345", "alias": "newbie"}, account=""
        )
        request = world.called("SignInRequest")[0]
        assert request.phone_code_hash == "hash-abcd"
        assert request.phone_code == "12345"

    async def test_the_pending_state_file_is_cleared(
        self, live_daemon, client, in_thread, tlgr_home
    ):
        await self._start(client, in_thread)
        await result(
            client, in_thread, "auth.verify-code", {"code": "12345", "alias": "newbie"}, account=""
        )
        assert not (tlgr_home / "accounts" / "newbie" / "login-state.json").exists()

    async def test_the_future_auth_token_is_stored_at_0600(
        self, live_daemon, client, in_thread, tlgr_home
    ):
        await self._start(client, in_thread)
        await result(
            client, in_thread, "auth.verify-code", {"code": "12345", "alias": "newbie"}, account=""
        )
        tokens = tlgr_home / "accounts" / "newbie" / "future-auth-tokens"
        assert tokens.exists()
        assert stat.S_IMODE(tokens.stat().st_mode) == 0o600

    async def test_a_login_link_is_accepted_where_a_code_is(
        self, live_daemon, client, in_thread, world
    ):
        await self._start(client, in_thread)
        await result(
            client,
            in_thread,
            "auth.verify-code",
            {"code": "https://t.me/login/54321", "alias": "newbie"},
            account="",
        )
        assert world.called("SignInRequest")[0].phone_code == "54321"

    async def test_a_qr_token_pasted_here_is_refused_with_the_right_command(
        self, live_daemon, client, in_thread
    ):
        await self._start(client, in_thread)
        with pytest.raises(Exception) as caught:
            await result(
                client,
                in_thread,
                "auth.verify-code",
                {"code": "tg://login?token=AQI", "alias": "newbie"},
                account="",
            )
        assert classify(caught.value).exit_code == EXIT_USAGE
        assert "accept-qr" in str(caught.value)

    async def test_a_password_is_asked_for_with_exit_4(self, live_daemon, client, in_thread, world):
        """Not a crash: an agent adds --password-env and re-runs the same line."""
        from telethon.errors import SessionPasswordNeededError

        await self._start(client, in_thread)
        world.auth.password = "hunter2"
        world.auth.hint = "the usual"
        world.fail_next("SignInRequest", SessionPasswordNeededError(None))
        with pytest.raises(Exception) as caught:
            await result(
                client,
                in_thread,
                "auth.verify-code",
                {"code": "12345", "alias": "newbie"},
                account="",
            )
        assert classify(caught.value).exit_code == EXIT_AUTH
        assert "the usual" in str(caught.value)

    async def test_the_password_completes_the_login(self, live_daemon, client, in_thread, world):
        from telethon.errors import SessionPasswordNeededError

        await self._start(client, in_thread)
        world.auth.password = "hunter2"
        world.fail_next("SignInRequest", SessionPasswordNeededError(None))
        done = await result(
            client,
            in_thread,
            "auth.verify-code",
            {"code": "12345", "alias": "newbie", "password": "hunter2"},
            account="",
        )
        assert done["status"] == "authorized"
        assert world.called("CheckPasswordRequest")

    async def test_a_stale_srp_id_is_retried_against_a_fresh_challenge(
        self, live_daemon, client, in_thread, world
    ):
        """An srp_id is single-use; a retry with the old one fails forever."""
        from telethon.errors import SessionPasswordNeededError

        await self._start(client, in_thread)
        world.auth.password = "hunter2"
        world.auth.srp_id_invalid_once = True
        world.fail_next("SignInRequest", SessionPasswordNeededError(None))
        done = await result(
            client,
            in_thread,
            "auth.verify-code",
            {"code": "12345", "alias": "newbie", "password": "hunter2"},
            account="",
        )
        assert done["status"] == "authorized"
        assert len(world.called("GetPasswordRequest")) >= 2

    async def test_a_number_with_no_account_stops_rather_than_signing_up(
        self, live_daemon, client, in_thread, world
    ):
        await self._start(client, in_thread)
        world.auth.signup_required = True
        done = await result(
            client, in_thread, "auth.verify-code", {"code": "12345", "alias": "newbie"}, account=""
        )
        assert done["status"] == "signup_required"
        assert "auth sign-up" in done["hint"]
        assert not world.called("SignUpRequest")

    async def test_verifying_without_a_pending_login_is_a_usage_error(
        self, live_daemon, client, in_thread
    ):
        with pytest.raises(Exception) as caught:
            await result(
                client,
                in_thread,
                "auth.verify-code",
                {"code": "12345", "alias": "spare"},
                account="",
            )
        assert classify(caught.value).exit_code == EXIT_USAGE


class TestResendCode:
    async def test_it_switches_to_the_next_delivery_method(self, live_daemon, client, in_thread):
        await result(
            client,
            in_thread,
            "auth.send-code",
            {"phone": PHONE, "alias": "newbie", "api_id": API_ID, "api_hash": API_HASH},
            account="",
        )
        sent = await result(client, in_thread, "auth.resend-code", {"alias": "newbie"}, account="")
        assert sent["type"] == "sms"

    async def test_cancelling_drops_the_pending_state(
        self, live_daemon, client, in_thread, tlgr_home
    ):
        await result(
            client,
            in_thread,
            "auth.send-code",
            {"phone": PHONE, "alias": "newbie", "api_id": API_ID, "api_hash": API_HASH},
            account="",
        )
        sent = await result(
            client, in_thread, "auth.resend-code", {"alias": "newbie", "cancel": True}, account=""
        )
        assert sent["cancelled"] is True
        assert not (tlgr_home / "accounts" / "newbie" / "login-state.json").exists()

    async def test_reporting_a_missing_code_needs_the_network_code(
        self, live_daemon, client, in_thread
    ):
        """A CLI has no SIM to read an MNC from, so it must be supplied."""
        await result(
            client,
            in_thread,
            "auth.send-code",
            {"phone": PHONE, "alias": "newbie", "api_id": API_ID, "api_hash": API_HASH},
            account="",
        )
        with pytest.raises(Exception) as caught:
            await result(
                client,
                in_thread,
                "auth.resend-code",
                {"alias": "newbie", "report_missing": True},
                account="",
            )
        assert classify(caught.value).exit_code == EXIT_USAGE


class TestQr:
    async def test_it_streams_a_token_then_the_authorization(self, live_daemon, client, in_thread):
        frames = await in_thread(
            lambda: list(
                client.op_stream(
                    "auth.qr",
                    {"alias": "newbie", "api_id": API_ID, "api_hash": API_HASH, "url_only": True},
                    account="",
                )
            )
        )
        items = [f["data"] for f in frames if f.get("type") == "item"]
        assert items[0]["status"] == "pending"
        assert items[0]["url"].startswith("tg://login?token=")
        assert items[-1]["status"] == "authorized"


class TestSignUp:
    async def test_it_refuses_without_an_explicit_terms_acceptance(
        self, live_daemon, client, in_thread
    ):
        """Accepting Terms of Service is a legal act; never a side effect."""
        await result(
            client,
            in_thread,
            "auth.send-code",
            {"phone": PHONE, "alias": "newbie", "api_id": API_ID, "api_hash": API_HASH},
            account="",
        )
        with pytest.raises(Exception) as caught:
            await result(
                client,
                in_thread,
                "auth.sign-up",
                {"alias": "newbie", "first_name": "Ada"},
                account="",
            )
        assert classify(caught.value).exit_code == EXIT_USAGE
        assert "--accept-tos" in str(caught.value)

    async def test_it_registers_when_told_to(self, live_daemon, client, in_thread, world):
        await result(
            client,
            in_thread,
            "auth.send-code",
            {"phone": PHONE, "alias": "newbie", "api_id": API_ID, "api_hash": API_HASH},
            account="",
        )
        done = await result(
            client,
            in_thread,
            "auth.sign-up",
            {"alias": "newbie", "first_name": "Ada", "accept_tos": True},
            account="",
        )
        assert done["status"] == "authorized"
        assert world.called("SignUpRequest")[0].first_name == "Ada"


class TestRecover:
    async def test_asking_for_a_code_reports_the_address(self, live_daemon, client, in_thread):
        answer = await result(client, in_thread, "auth.recover", {"alias": "work"})
        assert answer["status"] == "code_sent"
        assert answer["email_pattern"] == "a**@e*****e.com"

    async def test_a_code_can_be_checked_without_being_spent(
        self, live_daemon, client, in_thread, world
    ):
        answer = await result(
            client, in_thread, "auth.recover", {"alias": "work", "code": "1", "check_only": True}
        )
        assert answer["status"] == "code_valid"
        assert not world.called("RecoverPasswordRequest")


class TestResetAccount:
    async def test_it_refuses_without_the_phone_number_typed_back(
        self, live_daemon, client, in_thread
    ):
        await result(
            client,
            in_thread,
            "auth.send-code",
            {"phone": PHONE, "alias": "newbie", "api_id": API_ID, "api_hash": API_HASH},
            account="",
        )
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "auth.reset-account", {"alias": "newbie"}, account="")
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_status_reports_no_pending_reset(self, live_daemon, client, in_thread):
        answer = await result(
            client, in_thread, "auth.reset-account", {"alias": "newbie", "status": True}, account=""
        )
        assert answer["status"] == "none"


class TestTos:
    async def test_no_pending_update_reads_clean(self, live_daemon, client, in_thread):
        answer = await result(client, in_thread, "auth.tos")
        assert answer.get("update_available", False) is False

    async def test_a_pending_update_can_be_accepted(self, live_daemon, client, in_thread, world):
        from telethon.tl import types

        world.auth.terms = types.help.TermsOfService(
            id=types.DataJSON(data="tos-1"), text="Be nice.", entities=[]
        )
        answer = await result(client, in_thread, "auth.tos", {"accept": True})
        assert answer["accepted"] is True
        assert world.called("AcceptTermsOfServiceRequest")[0].id.data == "tos-1"

    async def test_declining_needs_the_deletion_acknowledged(
        self, live_daemon, client, in_thread, world
    ):
        from telethon.tl import types

        world.auth.terms = types.help.TermsOfService(
            id=types.DataJSON(data="tos-1"), text="Be nice.", entities=[]
        )
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "auth.tos", {"decline": True})
        assert classify(caught.value).exit_code == EXIT_USAGE
        assert not world.called("DeleteAccountRequest")

    async def test_an_age_confirmation_is_enforced(self, live_daemon, client, in_thread, world):
        from telethon.tl import types

        world.auth.terms = types.help.TermsOfService(
            id=types.DataJSON(data="tos-1"), text="Be nice.", entities=[], min_age_confirm=16
        )
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "auth.tos", {"accept": True})
        assert "confirm-age" in str(caught.value)


class TestLoginEmail:
    async def test_it_sends_a_verification_code(self, live_daemon, client, in_thread, world):
        await result(
            client,
            in_thread,
            "auth.send-code",
            {"phone": PHONE, "alias": "newbie", "api_id": API_ID, "api_hash": API_HASH},
            account="",
        )
        answer = await result(
            client,
            in_thread,
            "auth.login-email.set",
            {"alias": "newbie", "email": "ada@example.com"},
            account="",
        )
        assert answer["sent_code"] is True
        purpose = world.called("SendVerifyEmailCodeRequest")[0].purpose
        assert type(purpose).__name__ == "EmailVerifyPurposeLoginSetup"


class TestCodeList:
    async def test_it_reads_the_codes_from_the_service_chat(
        self, live_daemon, client, in_thread, world
    ):
        world.add_message(777000, "Login code: 54321. Do not give this code to anyone.")
        answer = await result(client, in_thread, "auth.code.list")
        assert answer["codes"] == ["54321"]

    async def test_a_leaked_code_can_be_burned(self, live_daemon, client, in_thread, world):
        await result(client, in_thread, "auth.code.list", {"invalidate": ["54321"]})
        assert world.called("InvalidateSignInCodesRequest")[0].codes == ["54321"]


class TestAutologinUrl:
    async def test_a_foreign_domain_is_refused(self, live_daemon, client, in_thread, world):
        world.auth.app_config = {
            "autologin_domains": ["telegram.org"],
            "autologin_token": "tok",
        }
        with pytest.raises(Exception) as caught:
            await result(
                client, in_thread, "auth.autologin-url.get", {"url": "https://evil.example/x"}
            )
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_an_allowed_domain_gets_the_token(self, live_daemon, client, in_thread, world):
        world.auth.app_config = {
            "autologin_domains": ["telegram.org"],
            "autologin_token": "tok",
        }
        answer = await result(
            client, in_thread, "auth.autologin-url.get", {"url": "https://telegram.org/faq"}
        )
        assert answer["url"].endswith("autologin_token=tok")


# ---------------------------------------------------------------------------
# account — the local record
# ---------------------------------------------------------------------------


class TestLocalRecord:
    def test_listing_works_with_no_daemon_running(self, cli_runner):
        """You ask "is my account fine?" exactly when the daemon is not up."""
        out = run(cli_runner, "--json", "account", "list")
        assert out.exit_code == 0, out.output
        assert '"alias": "work"' in out.output

    def test_the_v1_alias_key_survives(self, cli_runner):
        out = run(cli_runner, "--json", "account", "list")
        assert '"name"' in out.output and '"alias"' in out.output

    def test_switching_reports_already_when_nothing_changes(self, cli_runner):
        run(cli_runner, "account", "switch", "spare")
        out = run(cli_runner, "--json", "account", "switch", "spare")
        assert '"already": true' in out.output

    def test_switching_to_an_unknown_alias_is_not_found(self, cli_runner):
        out = run(cli_runner, "--json", "account", "switch", "nope")
        assert out.exit_code == 5, out.output

    def test_renaming_moves_the_record(self, cli_runner):
        out = run(cli_runner, "--json", "account", "rename", "spare", "backup")
        assert out.exit_code == 0, out.output
        listed = run(cli_runner, "--json", "account", "list")
        assert '"backup"' in listed.output

    def test_the_accounts_shortcut_still_resolves(self, cli_runner):
        out = run(cli_runner, "--json", "accounts")
        assert out.exit_code == 0, out.output


class TestAdd:
    async def test_a_bot_token_finishes_in_one_call(self, live_daemon, client, in_thread, world):
        added = await result(
            client,
            in_thread,
            "account.add",
            {
                "bot": True,
                "token": "4242:AA-token",
                "alias": "helper",
                "api_id": API_ID,
                "api_hash": API_HASH,
            },
            account="",
        )
        assert added["kind"] == "bot"
        assert added["authorized"] is True
        assert world.called("ImportBotAuthorizationRequest")[0].bot_auth_token == "4242:AA-token"

    async def test_a_phone_login_hands_back_the_next_command(self, live_daemon, client, in_thread):
        """A daemon cannot prompt, so the two-step flow is stated, not hidden."""
        added = await result(
            client,
            in_thread,
            "account.add",
            {"phone": PHONE, "alias": "newbie", "api_id": API_ID, "api_hash": API_HASH},
            account="",
        )
        assert "auth verify-code" in added["hint"]

    async def test_qr_points_at_its_own_command(self, live_daemon, client, in_thread):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.add", {"use_qr": True}, account="")
        assert "tlgr auth qr" in str(caught.value)

    async def test_a_bot_without_a_token_is_a_usage_error(self, live_daemon, client, in_thread):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.add", {"bot": True}, account="")
        assert classify(caught.value).exit_code == EXIT_USAGE


class TestExport:
    async def test_it_refuses_to_print_a_credential_by_accident(
        self, live_daemon, client, in_thread
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.export", {})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_stdout_is_an_explicit_choice(self, live_daemon, client, in_thread):
        answer = await call(client, in_thread, "account.export", {"stdout": True})
        assert answer["result"]["format"] == "string"
        assert any("full authorization" in w for w in answer["meta"]["warnings"])


class TestLogout:
    async def test_it_revokes_the_authorization_on_the_server(
        self, live_daemon, client, in_thread, world
    ):
        """v1's `account remove` never called this, so the session lived on."""
        answer = await result(client, in_thread, "account.logout", {"alias": "work"}, account="")
        assert answer["logged_out"] is True
        assert world.called("LogOutRequest")

    async def test_the_future_auth_token_is_kept_for_a_codeless_relogin(
        self, live_daemon, client, in_thread, tlgr_home
    ):
        answer = await result(client, in_thread, "account.logout", {"alias": "work"}, account="")
        assert answer["future_auth_token_stored"] is True
        assert (tlgr_home / "accounts" / "work" / "future-auth-tokens").exists()

    async def test_the_alias_survives_a_logout(self, live_daemon, client, in_thread, tlgr_home):
        """Logging out is not removing: `auth send-code` must still find it."""
        from tlgr.core.accounts import AccountManager

        await result(client, in_thread, "account.logout", {"alias": "work"}, account="")
        assert AccountManager(tlgr_home).get_account("work") is not None


class TestRemove:
    async def test_it_says_the_server_authorization_is_still_alive(
        self, live_daemon, client, in_thread, world
    ):
        answer = await result(client, in_thread, "account.remove", {"alias": "work"}, account="")
        assert answer["removed"] is True
        assert answer.get("server_logout", False) is False
        assert "--logout" in answer["hint"]
        assert not world.called("LogOutRequest")

    async def test_logout_revokes_it(self, live_daemon, client, in_thread, world):
        answer = await result(
            client,
            in_thread,
            "account.remove",
            {"alias": "work", "server_logout": True},
            account="",
        )
        assert answer["server_logout"] is True
        assert world.called("LogOutRequest")

    async def test_an_unknown_alias_is_not_found(self, live_daemon, client, in_thread):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.remove", {"alias": "nope"}, account="")
        assert classify(caught.value).exit_code == 5


class TestCheck:
    async def test_a_live_account_reads_authorized(self, live_daemon, client, in_thread):
        page = await call(client, in_thread, "account.check", {"alias": "work"}, account="")
        assert page["result"][0]["state"] == "authorized"

    async def test_a_revoked_key_is_distinguished_from_a_network_failure(
        self, live_daemon, client, in_thread, world
    ):
        """The distinction `daemon status` cannot make, and the reason for the op."""
        world.authorized = False
        page = await call(client, in_thread, "account.check", {"alias": "work"}, account="")
        assert page["result"][0]["state"] == "revoked"
        assert "auth send-code" in page["result"][0]["hint"]


class TestInfoAndSync:
    async def test_info_reports_the_session_file_and_the_dc(self, live_daemon, client, in_thread):
        answer = await result(client, in_thread, "account.info")
        assert answer["user_id"] == 777
        assert answer["session_path"].endswith("session.session")

    async def test_sync_refreshes_the_stored_record(self, live_daemon, client, in_thread, world):
        from fake_telethon import make_channel

        world.add_channel(make_channel(9000, title="News"))
        answer = await result(client, in_thread, "account.sync", {})
        assert answer["ok"] is True
        # The common box the fake session holds; `updates.getState` answers
        # from it rather than from a constant, so `sync status` and this
        # agree about what the account's pts is.
        assert answer["pts"] == 91824


# ---------------------------------------------------------------------------
# account session * — the Devices list
# ---------------------------------------------------------------------------


class TestSessionList:
    async def test_every_device_is_listed(self, live_daemon, client, in_thread, sessions):
        page = await call(client, in_thread, "account.session.list")
        assert {item["hash"] for item in page["result"]} == {"0", "9021045", "7001"}

    async def test_the_current_session_is_marked(self, live_daemon, client, in_thread, sessions):
        page = await call(client, in_thread, "account.session.list", {"hash": "current"})
        assert [item["hash"] for item in page["result"]] == ["0"]

    async def test_unconfirmed_logins_can_be_singled_out(
        self, live_daemon, client, in_thread, sessions
    ):
        page = await call(client, in_thread, "account.session.list", {"unconfirmed": True})
        assert [item["hash"] for item in page["result"]] == ["7001"]

    async def test_an_unconfirmed_login_carries_its_deny_deadline(
        self, live_daemon, client, in_thread, sessions
    ):
        """A cron that runs less often than the window never sees one at all."""
        sessions.auth.app_config = {"authorization_autoconfirm_period": 3600}
        page = await call(client, in_thread, "account.session.list", {"unconfirmed": True})
        assert page["result"][0]["deny_deadline"]

    async def test_the_account_wide_ttl_rides_along(self, live_daemon, client, in_thread, sessions):
        page = await call(client, in_thread, "account.session.list")
        assert page["result"][0]["ttl_days"] == 180

    async def test_the_page_envelope_is_the_normal_one(
        self, live_daemon, client, in_thread, sessions
    ):
        page = await call(client, in_thread, "account.session.list")
        assert page["page"]["has_more"] is False
        assert page["page"]["total"] == 3


class TestSessionTerminate:
    async def test_a_terminated_session_is_actually_gone(
        self, live_daemon, client, in_thread, sessions
    ):
        await result(client, in_thread, "account.session.terminate", {"hash": ["9021045"]})
        page = await call(client, in_thread, "account.session.list")
        assert "9021045" not in {item["hash"] for item in page["result"]}

    async def test_all_others_keeps_this_one(self, live_daemon, client, in_thread, sessions):
        await result(client, in_thread, "account.session.terminate", {"all_others": True})
        page = await call(client, in_thread, "account.session.list")
        assert [item["hash"] for item in page["result"]] == ["0"]

    async def test_denying_a_login_advises_a_password_change(
        self, live_daemon, client, in_thread, sessions
    ):
        answer = await result(
            client, in_thread, "account.session.terminate", {"hash": ["7001"], "deny": True}
        )
        assert "password change" in answer["advice"]

    async def test_terminating_nothing_is_a_usage_error(
        self, live_daemon, client, in_thread, sessions
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.session.terminate", {})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_dry_run_terminates_nothing(self, live_daemon, client, in_thread, sessions):
        answer = await call(
            client,
            in_thread,
            "account.session.terminate",
            {"hash": ["9021045"]},
            dry_run=True,
        )
        assert answer["result"]["dry_run"] is True
        assert not sessions.called("ResetAuthorizationRequest")


class TestSessionSet:
    async def test_calls_can_be_turned_off_for_one_session(
        self, live_daemon, client, in_thread, sessions
    ):
        await result(client, in_thread, "account.session.set", {"hash": "9021045", "calls": "off"})
        request = sessions.called("ChangeAuthorizationSettingsRequest")[0]
        assert request.call_requests_disabled is True

    async def test_the_ttl_is_account_wide(self, live_daemon, client, in_thread, sessions):
        answer = await result(client, in_thread, "account.session.set", {"auto_terminate": "90d"})
        assert answer["authorization_ttl_days"] == 90

    async def test_an_out_of_range_ttl_is_refused_before_the_rpc(
        self, live_daemon, client, in_thread, sessions
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.session.set", {"auto_terminate": "500d"})
        assert classify(caught.value).exit_code == EXIT_USAGE
        assert not sessions.called("SetAuthorizationTTLRequest")

    async def test_changing_nothing_is_a_usage_error(
        self, live_daemon, client, in_thread, sessions
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.session.set", {})
        assert classify(caught.value).exit_code == EXIT_USAGE


class TestSessionConfirmAndQr:
    async def test_confirming_clears_the_unconfirmed_flag(
        self, live_daemon, client, in_thread, sessions
    ):
        await result(client, in_thread, "account.session.confirm", {"hash": "7001"})
        page = await call(client, in_thread, "account.session.list", {"unconfirmed": True})
        assert page["result"] == []

    async def test_confirming_an_already_confirmed_session_is_already(
        self, live_daemon, client, in_thread, sessions
    ):
        answer = await call(client, in_thread, "account.session.confirm", {"hash": "9021045"})
        assert answer["result"]["already"] is True
        assert answer["meta"]["already"] is True

    async def test_accepting_a_qr_login_prints_what_was_created(
        self, live_daemon, client, in_thread, sessions
    ):
        answer = await result(
            client, in_thread, "account.session.accept-qr", {"link": "tg://login?token=ZmFrZQ"}
        )
        assert answer["device_model"] == "Web"

    async def test_a_malformed_token_is_a_usage_error(
        self, live_daemon, client, in_thread, sessions
    ):
        with pytest.raises(Exception) as caught:
            await result(
                client, in_thread, "account.session.accept-qr", {"link": "tg://login?token=!!!!"}
            )
        assert classify(caught.value).exit_code == EXIT_USAGE


class TestWebsites:
    async def test_they_are_a_different_list_from_devices(
        self, live_daemon, client, in_thread, sessions
    ):
        page = await call(client, in_thread, "account.website.list")
        assert [item["domain"] for item in page["result"]] == ["example.com"]

    async def test_revoking_one_removes_it(self, live_daemon, client, in_thread, sessions):
        await result(client, in_thread, "account.website.revoke", {"hash": ["770"]})
        page = await call(client, in_thread, "account.website.list")
        assert page["result"] == []

    async def test_the_bot_behind_a_login_can_be_blocked_too(
        self, live_daemon, client, in_thread, sessions
    ):
        await result(
            client, in_thread, "account.website.revoke", {"hash": ["770"], "block_bot": True}
        )
        assert sessions.called("BlockRequest")

    async def test_revoking_nothing_is_a_usage_error(
        self, live_daemon, client, in_thread, sessions
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.website.revoke", {})
        assert classify(caught.value).exit_code == EXIT_USAGE


class TestPasskeys:
    async def test_they_can_be_audited(self, live_daemon, client, in_thread, world):
        from datetime import datetime, timezone

        from telethon.tl import types

        world.auth.passkeys = [
            types.Passkey(id="pk_1", name="iPhone", date=datetime.now(timezone.utc))
        ]
        page = await call(client, in_thread, "account.passkey.list")
        assert [item["name"] for item in page["result"]] == ["iPhone"]

    async def test_one_can_be_deleted(self, live_daemon, client, in_thread, world):
        from datetime import datetime, timezone

        from telethon.tl import types

        world.auth.passkeys = [
            types.Passkey(id="pk_1", name="iPhone", date=datetime.now(timezone.utc))
        ]
        await result(client, in_thread, "account.passkey.delete", {"id": "pk_1"})
        page = await call(client, in_thread, "account.passkey.list")
        assert page["result"] == []


# ---------------------------------------------------------------------------
# account password *
# ---------------------------------------------------------------------------


class TestPassword:
    async def test_status_never_prints_srp_material(self, live_daemon, client, in_thread, world):
        world.auth.password = "hunter2"
        world.auth.hint = "the usual"
        answer = await result(client, in_thread, "account.password.get")
        assert answer["has_password"] is True
        assert answer["hint"] == "the usual"
        assert not {"srp_B", "srp_id", "secure_random"} & set(answer)

    async def test_verify_needs_a_password_source(self, live_daemon, client, in_thread):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.password.get", {"verify": True})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_the_recovery_address_needs_the_password(
        self, live_daemon, client, in_thread, world
    ):
        world.auth.password = "hunter2"
        world.auth.recovery_email = "ada@example.com"
        answer = await result(client, in_thread, "account.password.get", {"password": "hunter2"})
        assert answer["recovery_email"] == "ada@example.com"
        assert answer["password_ok"] is True

    async def test_a_wrong_password_reports_rather_than_raises_under_verify(
        self, live_daemon, client, in_thread, world
    ):
        from telethon.errors import RPCError

        world.auth.password = "hunter2"
        world.fail_next("GetPasswordSettingsRequest", RPCError("r", "PASSWORD_HASH_INVALID", 400))
        answer = await result(
            client,
            in_thread,
            "account.password.get",
            {"password": "wrong", "verify": True},
        )
        assert answer["password_ok"] is False

    async def test_setting_one_computes_a_digest(self, live_daemon, client, in_thread, world):
        await result(
            client, in_thread, "account.password.set", {"new_password": "hunter2", "hint": "h"}
        )
        request = world.called("UpdatePasswordSettingsRequest")[0]
        assert len(request.new_settings.new_password_hash) == 256
        assert type(request.password).__name__ == "InputCheckPasswordEmpty"

    async def test_setting_one_twice_points_at_change(self, live_daemon, client, in_thread, world):
        world.auth.password = "hunter2"
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.password.set", {"new_password": "x"})
        assert "account password change" in str(caught.value)

    async def test_changing_refuses_to_destroy_passport_data(
        self, live_daemon, client, in_thread, world
    ):
        """Telethon's edit_2fa drops the secure settings; that is the destructive case."""
        world.auth.password = "hunter2"
        world.auth.has_secure_values = True
        with pytest.raises(Exception) as caught:
            await result(
                client,
                in_thread,
                "account.password.change",
                {"password": "hunter2", "new_password": "hunter3"},
            )
        assert classify(caught.value).exit_code == EXIT_USAGE
        assert "--keep-passport" in str(caught.value)
        assert not world.called("UpdatePasswordSettingsRequest")

    async def test_changing_warns_about_the_freshness_cooldown(
        self, live_daemon, client, in_thread, world
    ):
        world.auth.password = "hunter2"
        answer = await call(
            client,
            in_thread,
            "account.password.change",
            {"password": "hunter2", "new_password": "hunter3"},
        )
        assert any("PASSWORD_TOO_FRESH" in w for w in answer["meta"]["warnings"])
        assert answer["result"]["sensitive_actions_eligible_at"]

    async def test_removing_needs_the_current_password(self, live_daemon, client, in_thread):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.password.remove", {})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_removing_turns_it_off(self, live_daemon, client, in_thread, world):
        world.auth.password = "hunter2"
        answer = await result(client, in_thread, "account.password.remove", {"password": "hunter2"})
        assert answer.get("has_password", False) is False

    async def test_reset_reports_the_wait(self, live_daemon, client, in_thread):
        answer = await result(client, in_thread, "account.password.reset", {})
        assert answer["status"] == "wait"
        assert answer["until_date"]

    async def test_a_temporary_password_needs_the_cloud_one(self, live_daemon, client, in_thread):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.password.temp", {})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_a_temporary_password_is_minted(self, live_daemon, client, in_thread, world):
        world.auth.password = "hunter2"
        answer = await result(
            client, in_thread, "account.password.temp", {"password": "hunter2", "period": "30m"}
        )
        assert answer["tmp_password"]
        assert world.called("GetTmpPasswordRequest")[0].period == 1800


class TestEmailAndPhone:
    async def test_the_recovery_address_needs_the_cloud_password(
        self, live_daemon, client, in_thread
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.email.set", {"email": "ada@example.com"})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_a_login_email_goes_through_the_verify_purpose(
        self, live_daemon, client, in_thread, world
    ):
        answer = await result(
            client,
            in_thread,
            "account.email.set",
            {"email": "ada@example.com", "kind": "login"},
        )
        assert answer["email_pattern"] == "a**@e*****e.com"
        purpose = world.called("SendVerifyEmailCodeRequest")[0].purpose
        assert type(purpose).__name__ == "EmailVerifyPurposeLoginChange"

    async def test_changing_the_phone_number_is_two_steps(
        self, live_daemon, client, in_thread, world
    ):
        first = await result(client, in_thread, "account.phone.set", {"phone": "+49123456789"})
        assert first["code_hash"] == "hash-abcd"
        second = await result(
            client, in_thread, "account.phone.set", {"phone": "+49123456789", "code": "12345"}
        )
        assert second["changed"] is True
        assert world.called("ChangePhoneRequest")[0].phone_code_hash == "hash-abcd"

    async def test_a_code_with_nothing_pending_is_a_usage_error(
        self, live_daemon, client, in_thread
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.phone.set", {"code": "12345"})
        assert classify(caught.value).exit_code == EXIT_USAGE


class TestAccountSwitches:
    async def test_the_ttl_round_trips(self, live_daemon, client, in_thread):
        await result(client, in_thread, "account.ttl.set", {"value": "12m"})
        assert (await result(client, in_thread, "account.ttl.get"))["days"] == 365

    async def test_an_out_of_range_ttl_is_refused(self, live_daemon, client, in_thread, world):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.ttl.set", {"value": "10d"})
        assert classify(caught.value).exit_code == EXIT_USAGE
        assert not world.called("SetAccountTTLRequest")

    async def test_device_locked_takes_a_duration(self, live_daemon, client, in_thread, world):
        answer = await result(client, in_thread, "account.device-locked.set", {"period": "1h"})
        assert answer["locked_for"] == 3600
        assert world.called("UpdateDeviceLockedRequest")[0].period == 3600

    async def test_unlocking_is_period_zero(self, live_daemon, client, in_thread, world):
        await result(client, in_thread, "account.device-locked.set", {"unlock": True})
        assert world.called("UpdateDeviceLockedRequest")[0].period == 0

    async def test_deleting_the_account_needs_the_number_typed_back(
        self, live_daemon, client, in_thread, world
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "account.delete", {})
        assert classify(caught.value).exit_code == EXIT_USAGE
        assert not world.called("DeleteAccountRequest")


class TestSmsJobsSuggestionsSupport:
    async def test_smsjobs_reports_status_without_joining(
        self, live_daemon, client, in_thread, world
    ):
        answer = await result(client, in_thread, "account.smsjobs.set", {})
        assert answer["terms_url"].startswith("https://")
        assert not world.called("JoinRequest")

    async def test_joining_is_explicit(self, live_daemon, client, in_thread, world):
        await result(client, in_thread, "account.smsjobs.set", {"join": True})
        assert world.called("JoinRequest")

    async def test_pending_suggestions_are_listed(self, live_daemon, client, in_thread, world):
        world.auth.pending_suggestions = ["VALIDATE_PASSWORD", "SETUP_PASSKEY"]
        page = await call(client, in_thread, "account.suggestion.list", {})
        assert {item["suggestion"] for item in page["result"]} >= {"VALIDATE_PASSWORD"}

    async def test_a_noskip_suggestion_cannot_be_dismissed(
        self, live_daemon, client, in_thread, world
    ):
        world.auth.pending_suggestions = ["SETUP_LOGIN_EMAIL_NOSKIP"]
        with pytest.raises(Exception) as caught:
            await result(
                client,
                in_thread,
                "account.suggestion.list",
                {"dismiss": "SETUP_LOGIN_EMAIL_NOSKIP"},
            )
        assert classify(caught.value).exit_code == EXIT_USAGE
        assert not world.called("DismissSuggestionRequest")

    async def test_support_reports_the_contact_and_the_links(self, live_daemon, client, in_thread):
        answer = await result(client, in_thread, "account.support.get", {})
        assert answer["support_user"] == 333000
        assert answer["faq_url"].startswith("https://")


# ---------------------------------------------------------------------------
# passport
# ---------------------------------------------------------------------------


@pytest.fixture
def passport(world):
    from telethon.tl import types

    world.auth.secure_values = [
        types.SecureValue(
            type=types.SecureValueTypePhone(),
            hash=b"h1",
            plain_data=types.SecurePlainPhone(phone=PHONE),
        ),
        types.SecureValue(type=types.SecureValueTypePassport(), hash=b"h2"),
    ]
    return world


class TestPassport:
    async def test_stored_documents_are_listed_as_metadata(
        self, live_daemon, client, in_thread, passport
    ):
        page = await call(client, in_thread, "passport.list")
        assert {item["type"] for item in page["result"]} == {"phone", "passport"}

    async def test_a_plain_value_comes_back_readable(
        self, live_daemon, client, in_thread, passport
    ):
        page = await call(client, in_thread, "passport.list")
        phone = next(item for item in page["result"] if item["type"] == "phone")
        assert phone["plain_data"] == PHONE

    async def test_decrypting_is_not_supported_rather_than_wrong(
        self, live_daemon, client, in_thread, passport
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "passport.list", {"decrypt": True})
        assert classify(caught.value).exit_code == EXIT_INDETERMINATE
        assert classify(caught.value).code == "NOT_SUPPORTED"

    async def test_an_unknown_document_type_is_a_usage_error(
        self, live_daemon, client, in_thread, passport
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "passport.list", {"type": ["fingerprints"]})
        assert classify(caught.value).exit_code == EXIT_USAGE

    async def test_deleting_needs_no_crypto_and_works(
        self, live_daemon, client, in_thread, passport
    ):
        answer = await result(client, in_thread, "passport.delete", {"type": ["passport"]})
        assert answer["deleted"] == ["passport"]
        page = await call(client, in_thread, "passport.list")
        assert {item["type"] for item in page["result"]} == {"phone"}

    async def test_authorizing_refuses_instead_of_faking_the_crypto(
        self, live_daemon, client, in_thread, passport
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "passport.authorize", {"bot": "@examplebot"})
        assert classify(caught.value).code == "NOT_SUPPORTED"

    async def test_the_country_language_lookup_works(
        self, live_daemon, client, in_thread, passport
    ):
        answer = await result(client, in_thread, "passport.form.get", {"country_language": "de"})
        assert answer["country_language"] == "de"

    async def test_verifying_an_email_sends_a_code(self, live_daemon, client, in_thread, passport):
        answer = await result(client, in_thread, "passport.verify", {"email": "ada@example.com"})
        assert answer["sent"] is True
        assert answer["code_length"] == 6

    async def test_verifying_needs_exactly_one_target(
        self, live_daemon, client, in_thread, passport
    ):
        with pytest.raises(Exception) as caught:
            await result(client, in_thread, "passport.verify", {})
        assert classify(caught.value).exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# What AGENT.md and the README promised
# ---------------------------------------------------------------------------


V1_PATHS = [
    ("account", "add"),
    ("account", "import"),
    ("account", "list"),
    ("account", "switch"),
    ("account", "remove"),
    ("account", "rename"),
    ("account", "info"),
    ("account", "sync"),
    ("login",),
    ("logout",),
    ("completion",),
    ("agent", "whoami"),
]


@pytest.mark.parametrize("path", V1_PATHS, ids=lambda p: " ".join(p))
def test_every_documented_v1_path_is_still_invocable(path):
    from tlgr.cli import cli

    node: Any = cli
    for token in path:
        node = node.commands.get(token) if hasattr(node, "commands") else None
    assert node is not None, f"tlgr {' '.join(path)} disappeared"


class TestV1Compatibility:
    def test_completion_still_prints_the_eval_line(self, cli_runner):
        out = run(cli_runner, "completion", "bash")
        assert out.exit_code == 0, out.output
        assert "_TLGR_COMPLETE=bash_source tlgr" in out.output
        assert "~/.bashrc" in out.output

    def test_an_unknown_shell_is_a_usage_error(self, cli_runner):
        out = run(cli_runner, "completion", "csh")
        assert out.exit_code == EXIT_USAGE

    def test_whoami_still_reports_the_v1_keys(self, cli_runner):
        import json

        out = run(cli_runner, "--json", "--results-only", "agent", "whoami")
        assert out.exit_code == 0, out.output
        body = json.loads(out.output)
        assert body["output_schema_version"] == 2
        assert {"account", "user_id", "username", "daemon_running", "config_dir"} <= set(body)

    def test_whoami_does_not_claim_a_daemon_from_a_stale_pid_file(self, cli_runner, tlgr_home):
        import json

        (tlgr_home / "daemon.pid").write_text("999999")
        out = run(cli_runner, "--json", "--results-only", "agent", "whoami")
        assert json.loads(out.output)["daemon_running"] is False
