"""Privacy rules, the global privacy switches, and paid-message revenue.

`account.setPrivacy` **replaces** the whole ordered rule vector, and
`account.setGlobalPrivacySettings` replaces the whole constructor. Both are
therefore read-modify-write operations, and both need a model that survives
the round trip without losing anything:

* `PrivacySettings` splits the vector into the four exception lists a human
  edits *and* keeps `raw_rules` — the constructors in server order — so a
  rule tlgr does not recognise is still sent back unchanged.
* `GlobalPrivacy` names every field of the constructor, so patching one and
  writing the rest back cannot silently clear a switch nobody mentioned.
"""

from __future__ import annotations

from tlgr.models.base import Model
from tlgr.models.peer import Peer

__all__ = [
    "GlobalPrivacy",
    "PaidMessageRevenue",
    "PrivacyExceptions",
    "PrivacyRule",
    "PrivacySettings",
]


class PrivacyRule(Model):
    """One `privacyValue*` constructor, in tlgr's vocabulary."""

    #: allow | disallow
    action: str = "allow"
    #: all | contacts | close-friends | premium | bots | users | chats
    scope: str = "all"
    ids: list[int] = []


class PrivacyExceptions(Model):
    """The four lists the GUI calls "Always allow" / "Never allow"."""

    allow_users: list[int] = []
    deny_users: list[int] = []
    allow_chats: list[int] = []
    deny_chats: list[int] = []


class PrivacySettings(Model):
    """One privacy key, read or written.

    `base` is the headline the GUI shows; the exception lists are what it
    puts under it. `raw_rules` is the server's own ordered vector, kept so a
    write can reproduce it exactly.
    """

    key: str
    #: everybody | contacts | close-friends | premium | bots | nobody
    base: str = "nobody"
    allow_users: list[int] = []
    deny_users: list[int] = []
    allow_chats: list[int] = []
    deny_chats: list[int] = []
    raw_rules: list[PrivacyRule] = []
    #: Resolved names for the exception ids, when `--resolve` was given.
    peers: list[Peer] = []


class GlobalPrivacy(Model):
    """`globalPrivacySettings`, every field named.

    Nothing here defaults to a value the server might disagree with: each
    flag is a tri-state so "the server did not report it" stays different
    from "off", which is what makes the read-modify-write safe.
    """

    hide_read_marks: bool | None = None
    archive_and_mute_new_noncontact_peers: bool | None = None
    new_noncontact_peers_require_premium: bool | None = None
    noncontact_peers_paid_stars: int | None = None
    keep_archived_unmuted: bool | None = None
    keep_archived_folders: bool | None = None
    display_gifts_button: bool | None = None
    #: unlimited | limited | unique | premium | from-channels
    disallowed_gifts: list[str] = []
    changed: list[str] = []
    already: bool = False


class PaidMessageRevenue(Model):
    user_id: int
    stars_amount: int = 0
