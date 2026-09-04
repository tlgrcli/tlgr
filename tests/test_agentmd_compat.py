"""What v1's `AGENT.md` promised an agent, still holding in v2.

Two kinds of promise, checked two ways.

* **Paths.** Every command path v1 documented is still invocable. §12.4 makes
  that absolute: `legacy_paths` turns each into an alias, so `tlgr send`,
  `tlgr msg list` and `tlgr message react` keep working after their modules
  were deleted.
* **Keys.** Every field v1's documented JSON carried is either still there
  with the same meaning, or is in the deliberate-change table below — which
  is the same table `CHANGELOG.md` publishes. A key that quietly disappears
  fails here; a key that changes on purpose has to be added to the table, in
  a line a reviewer sees.
"""

from __future__ import annotations

from typing import Any

import pytest

import tlgr.ops  # noqa: F401  — importing it populates the registry
from tlgr.cli import cli
from tlgr.registry import ALIASES

ALICE = 4242

#: Every `message`/`draft`/`chat`/`media` invocation AGENT.md documents, as v1
#: spelled it.
V1_PATHS = [
    ("message", "send"),
    ("message", "list"),
    ("message", "get"),
    ("message", "delete"),
    ("message", "search"),
    ("message", "pin"),
    ("message", "react"),
    ("message", "read"),
    ("message", "edit"),
    ("message", "forward"),
    ("msg", "send"),
    ("msg", "list"),
    ("msg", "get"),
    ("msg", "delete"),
    ("msg", "search"),
    ("send",),
    ("draft", "set"),
    ("draft", "clear"),
    ("draft", "list"),
    ("chat", "list"),
    ("chat", "open"),
    ("chat", "unread"),
    ("chat", "catchup"),
    ("chat", "get"),
    ("chat", "archive"),
    ("chat", "mute"),
    ("chat", "leave"),
    ("chat", "typing"),
    ("chat", "posters"),
    ("chat", "members"),
    ("chat", "create"),
    ("chats",),
    ("inbox",),
    ("catchup",),
    ("media", "download"),
    ("media", "upload"),
    ("dl",),
    ("up",),
    ("contact", "list"),
    ("contact", "add"),
    ("contact", "rename"),
    ("contact", "remove"),
    ("contact", "search"),
    ("contacts",),
    ("user", "get"),
    ("user", "dialog-status"),
    # PR-8: the one story command v1 had. It is `story hide` now, and the old
    # path is a legacy path on it rather than a second implementation.
    ("user", "hide-stories"),
]

#: Nothing documented is hand-written any more inside a migrated group: PR-7
#: moved `chat members` and `chat create` into the registry and deleted
#: `tlgr/cli/legacy/chat.py`. The list stays so a future group can name its
#: own stragglers here rather than inventing a second mechanism.
V1_HAND_WRITTEN: list[tuple[str, ...]] = []

#: The v1 paths PR-4 replaced. Every module behind them is deleted; every one
#: of them still resolves, because §12.4 makes that absolute.
V1_PR4_PATHS = [
    ("watch",),
    ("status",),
    ("schema",),
    ("exit-codes",),
    ("agent", "whoami"),
    ("agent", "exit-codes"),
    ("daemon", "start"),
    ("daemon", "stop"),
    ("daemon", "restart"),
    ("daemon", "status"),
    ("daemon", "install"),
    ("daemon", "uninstall"),
    ("daemon", "logs"),
    ("job", "list"),
    ("job", "add"),
    ("job", "remove"),
    ("job", "enable"),
    ("job", "disable"),
    ("job", "reload"),
    ("config", "init"),
    ("config", "validate"),
    ("config", "path"),
    ("config", "keys"),
    ("config", "list"),
    ("config", "get"),
    ("config", "set"),
    ("config", "unset"),
]

#: op id → the keys v1's AGENT.md showed in the response, minus the ones the
#: deliberate-change table below accounts for.
V1_KEYS: dict[str, set[str]] = {
    "message.send": {"id", "chat_id", "date"},
    "message.get": {"id", "text"},
    "message.delete": {"deleted"},
    "message.pin": {"pinned", "msg_id"},
    "message.react": {"reacted", "msg_id", "emoji"},
    "message.read": {"read", "chat_id"},
    "message.edit": {"edited", "id", "chat_id"},
    "draft.clear": {"cleared", "chat_id"},
    "chat.open": {"chat_id", "marked_read", "messages"},
    "chat.unread": {"unread", "chat_id"},
    "chat.get": {"id", "type", "name"},
    "chat.archive": {"archived", "chat_id"},
    "chat.mute": {"muted", "chat_id"},
    "chat.leave": {"left", "chat_id"},
    "chat.typing": {"typing", "chat_id"},
    "chat.catchup": {"chats"},
    "contact.add": {"added", "user_id"},
    "contact.rename": {"saved", "user_id", "first_name", "last_name"},
    "contact.remove": {"removed"},
    "user.get": {"id", "first_name", "username", "bio", "is_bot", "status", "stories_hidden"},
    "user.dialog-status": {
        "ref",
        "id",
        "username",
        "resolved",
        "has_dialog",
        "message_count",
        "read_outbox_max_id",
        "unread_count",
        "top_message",
        "source",
        "reason",
    },
    "user.hide-stories": {"user_id", "username", "hidden", "already"},
}

#: The changes CHANGELOG.md lists under "Breaking". Anything not in here has
#: to survive unchanged.
DELIBERATE_CHANGES = {
    "events.watch": "`tlgr watch` streams the whole event taxonomy instead of "
    "polling for new messages; `--results-only` keeps v1's line shape",
    "daemon.status": "`{running, accounts, healthy}` gained `ready` and a "
    "per-account state machine; `connections`/`disconnected` are unchanged",
    "job.list": "`{jobs: [...]}` became `Page[JobState]`",
    "config.list": "`{section: {key: value}}` became `Page[ConfigEntry]` with "
    "a `source` per key; secrets are redacted",
    "config.keys": "`{keys: {...}}` became `Page[ConfigKey]` carrying types, "
    "defaults and `requires_restart`",
    "message.list": "`{messages: [...]}` became the `Page[Message]` envelope; "
    "`--results-only` yields `{items, has_more, next_cursor}`",
    "message.search": "same as message.list",
    "message.forward": "`{forwarded, ids}` became `Page[ForwardedMessage]`",
    "message.edit": "`date` became `edit_date`",
    "draft.set": "`{draft: true}` became the saved `Draft` object",
    "draft.list": "`{drafts: [...]}` became `Page[Draft]`; `chat_name`/"
    "`chat_username` moved into `chat`, and `chat_id` is now the marked id",
    "chat.list": "`{chats: [...]}` became the `Page[Dialog]` envelope, and "
    "each row's `id`/`name`/`type`/`username` moved into a nested `chat` "
    "object so a dialog names its peer the same way every other response does",
    "contact.list": "`{contacts: [...]}` became the `Page[Contact]` envelope; "
    "every v1 row key survives and `phone` is normalised to E.164",
    "contact.search": "same as contact.list",
    "chat.poster.list": "`{posters: [...], scanned_messages, distinct_posters}` "
    "keeps its keys, but each poster gained `user_id` beside v1's `id` and "
    "`last_date`/`last_message_id` became `date`/`date_unix`/`last_msg_id`",
    "media.download": "`{path, msg_id}` became `Page[Downloaded]`; both keys "
    "survive on each item, and one invocation can now produce several",
    "media.upload": "`{id, chat_id}` became the `Uploaded` object; `id` is "
    "`msg_id`, beside `msg_ids` for an album",
    "chat.member.list": "`{members: [...]}` became `Page[Participant]`; each row "
    "keeps its ChannelParticipant wrapper (status, rank, date, inviter_id, "
    "promoted_by, kicked_by, both rights masks) and `first_name`/`last_name` "
    "are joined into `name`. `id`, `username` and `is_bot` are unchanged",
    "chat.create": "`{id, name, type}` became "
    "`{id, type, title, username, invite_link, added, missing}`; `name` is now "
    "`title`, and `missing` names every seed member the server refused",
}


def _walk(path: tuple[str, ...]) -> Any:
    node: Any = cli
    for token in path:
        node = node.commands.get(token) if hasattr(node, "commands") else None
        if node is None:
            return None
    return node


@pytest.mark.parametrize(
    "path", V1_PATHS + V1_HAND_WRITTEN + V1_PR4_PATHS, ids=lambda p: " ".join(p)
)
def test_every_documented_v1_path_is_still_invocable(path):
    assert _walk(path) is not None, f"tlgr {' '.join(path)} disappeared"


@pytest.mark.parametrize("path", V1_PATHS + V1_PR4_PATHS, ids=lambda p: " ".join(p))
def test_every_documented_v1_path_resolves_to_an_operation(path):
    assert ALIASES.get(".".join(path)) is not None


def test_the_chat_list_shortcuts_still_resolve():
    """`chats`, `inbox` and `catchup` were top-level v1 commands."""
    assert ALIASES["chats"] == "chat.list"
    assert ALIASES["inbox"] == "chat.list"
    assert ALIASES["catchup"] == "chat.catchup"


def test_the_v1_watch_line_shape_survives_results_only():
    """A script reading `tlgr watch` parses `event_type`, `chat_id`, `data`."""
    import io
    import json

    from tlgr.cli.render import render_stream

    out = io.StringIO()
    render_stream(
        [
            {"type": "meta", "protocol": 2},
            {"type": "message_new", "seq": 3, "chat_id": -100, "payload": {"id": 7}},
            {"type": "heartbeat", "ts": "2026-09-03T09:14:07Z"},
            {"type": "end", "ok": True},
        ],
        results_only=True,
        stream=out,
    )
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines == [
        {
            "event_type": "message_new",
            "chat_id": -100,
            "data": {"id": 7},
            "seq": 3,
            "account": None,
        }
    ]


def test_the_shortcuts_still_reach_message_send():
    for name in ("send", "msg.send", "message.send"):
        assert ALIASES[name] == "message.send"


def test_the_v1_story_command_still_reaches_the_story_group():
    """`user hide-stories` was v1's only story command; `story hide` is it now."""
    assert ALIASES["user.hide-stories"] == "story.hide"


def test_the_media_shortcuts_still_reach_the_media_operations():
    """`tlgr dl` and `tlgr up` were the two shortcuts v1's README taught."""
    assert ALIASES["dl"] == "media.download"
    for name in ("up", "media.send", "media.upload"):
        assert ALIASES[name] == "media.upload"


class TestDocumentedKeys:
    """The example on each spec is the shape the docs publish; check that."""

    @pytest.mark.parametrize("op_id", sorted(V1_KEYS), ids=sorted(V1_KEYS))
    def test_the_v1_keys_are_in_the_published_example(self, op_id):
        from tlgr.registry import get

        example = get(op_id).example
        missing = sorted(V1_KEYS[op_id] - set(example))
        assert missing == [], f"{op_id} lost {missing}, which AGENT.md documents"

    @pytest.mark.parametrize("op_id", sorted(V1_KEYS), ids=sorted(V1_KEYS))
    def test_the_v1_keys_survive_a_real_response(self, op_id):
        """A response model that cannot carry the key is the real regression."""
        import msgspec

        from tlgr.registry import get

        spec = get(op_id)
        fields = {f.name for f in msgspec.inspect.type_info(spec.response).fields}
        missing = sorted(V1_KEYS[op_id] - fields)
        assert missing == [], f"{spec.response.__name__} has no {missing}"


class TestDates:
    def test_a_date_is_rfc_3339_with_a_unix_sibling(self):
        from tlgr.registry import get

        example = get("message.send").example
        assert example["date"].endswith("Z")
        assert isinstance(example["date_unix"], int)


class TestChangeTable:
    def test_every_deliberate_change_names_an_operation(self):
        from tlgr.registry import REGISTRY

        for op_id in DELIBERATE_CHANGES:
            assert op_id in REGISTRY

    def test_the_changelog_publishes_the_same_table(self):
        from pathlib import Path

        changelog = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(
            encoding="utf-8"
        )
        for op_id in DELIBERATE_CHANGES:
            assert op_id in changelog, f"{op_id} changed shape without a CHANGELOG line"


class TestWhoami:
    def test_whoami_reports_the_output_schema_version(self, tlgr_home):
        """An agent has to be able to tell v1 output from v2 without probing.

        Runs against the isolated home the fixture provides: reading the
        developer's real `~/.tlgr` would make the result depend on their
        accounts, and a home marked as production refuses to be read at all.
        """
        from click.testing import CliRunner

        result = CliRunner().invoke(cli, ["--json", "agent", "whoami"])
        assert result.exit_code == 0, result.output
        assert '"output_schema_version": 2' in result.output
