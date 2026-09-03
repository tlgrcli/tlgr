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

#: Every `message`/`draft`/`chat` invocation AGENT.md documents, as v1
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
    ("chats",),
    ("inbox",),
    ("catchup",),
]

#: Documented v1 paths that are still hand-written commands rather than
#: registry operations: `chat create` and `chat members` migrate with the
#: groups-and-channels group (PR-7), and must keep working until they do.
V1_HAND_WRITTEN = [("chat", "members"), ("chat", "create")]

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
}

#: The changes CHANGELOG.md lists under "Breaking". Anything not in here has
#: to survive unchanged.
DELIBERATE_CHANGES = {
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
    "chat.poster.list": "`{posters: [...], scanned_messages, distinct_posters}` "
    "keeps its keys, but each poster gained `user_id` beside v1's `id` and "
    "`last_date`/`last_message_id` became `date`/`date_unix`/`last_msg_id`",
}


def _walk(path: tuple[str, ...]) -> Any:
    node: Any = cli
    for token in path:
        node = node.commands.get(token) if hasattr(node, "commands") else None
        if node is None:
            return None
    return node


@pytest.mark.parametrize("path", V1_PATHS + V1_HAND_WRITTEN, ids=lambda p: " ".join(p))
def test_every_documented_v1_path_is_still_invocable(path):
    assert _walk(path) is not None, f"tlgr {' '.join(path)} disappeared"


@pytest.mark.parametrize("path", V1_PATHS, ids=lambda p: " ".join(p))
def test_every_documented_v1_path_resolves_to_an_operation(path):
    assert ALIASES.get(".".join(path)) is not None


def test_the_chat_list_shortcuts_still_resolve():
    """`chats`, `inbox` and `catchup` were top-level v1 commands."""
    assert ALIASES["chats"] == "chat.list"
    assert ALIASES["inbox"] == "chat.list"
    assert ALIASES["catchup"] == "chat.catchup"


def test_the_shortcuts_still_reach_message_send():
    for name in ("send", "msg.send", "message.send"):
        assert ALIASES[name] == "message.send"


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
    def test_whoami_reports_the_output_schema_version(self, tlgr_home, monkeypatch):
        """An agent has to be able to tell v1 output from v2 without probing.

        Runs against the isolated `tlgr_home`: the legacy `whoami` reads the
        account store, and a test must never open the developer's real
        `~/.tlgr` (which is now refused when marked as production).
        """
        from click.testing import CliRunner

        import tlgr.core.config as config

        monkeypatch.setattr(config, "CONFIG_DIR", tlgr_home)
        result = CliRunner().invoke(cli, ["--json", "agent", "whoami"])
        assert result.exit_code == 0, result.output
        assert '"output_schema_version": 2' in result.output
