"""Chat folders: title decoding, app-config limits, and the include cap.

The cap test is the point. Telegram bounds a folder's include list (100 free,
200 premium) and a bulk backfill routinely asks for more than that, so the
only two honest behaviours are "refuse" and "apply all of it". Truncating
silently is the one that loses peers while reporting success, so
`folder_edit` raises and writes nothing — and that has to stay true.
"""

from __future__ import annotations

import pytest
from telethon.tl import types as t

from tlgr.core.client import ClientWrapper
from tlgr.core.errors import TlgrError, ChatNotFoundError


def _json(d):
    """Build a Telegram JsonObject from a plain dict of numbers/strings."""
    vals = []
    for k, v in d.items():
        if isinstance(v, str):
            node = t.JsonString(value=v)
        elif isinstance(v, bool):
            node = t.JsonBool(value=v)
        else:
            node = t.JsonNumber(value=float(v))
        vals.append(t.JsonObjectValue(key=k, value=node))
    return t.JsonObject(value=vals)


def test_json_value_decodes_nested_config():
    node = _json({"dialog_filters_chats_limit_default": 100, "name": "x"})
    out = ClientWrapper._json_value(node)
    assert out["dialog_filters_chats_limit_default"] == 100.0
    assert out["name"] == "x"


def test_json_value_handles_arrays_and_null():
    arr = t.JsonArray(value=[t.JsonNumber(value=1.0), t.JsonNull()])
    assert ClientWrapper._json_value(arr) == [1.0, None]


def test_filter_title_reads_textwithentities_and_plain_str():
    """Current layers wrap the title; older ones send a bare string."""
    wrapped = t.TextWithEntities(text="gh", entities=[])

    class F:
        title = wrapped

    class G:
        title = "DM"

    assert ClientWrapper._filter_title(F()) == "gh"
    assert ClientWrapper._filter_title(G()) == "DM"
    assert ClientWrapper._filter_title(object()) == ""


class _FakePeer:
    def __init__(self, uid, username=None):
        self.user_id = uid
        self.username = username


def _wrapper_with(folder, cap=100, premium=False, peers=None):
    """A ClientWrapper whose network calls are stubbed to fixed answers."""
    w = ClientWrapper.__new__(ClientWrapper)
    peers = peers or {}

    async def _get_filters():
        return [t.DialogFilterDefault(), folder]

    async def _limits():
        return {
            "dialog_filters_chats_limit_default": cap,
            "dialog_filters_chats_limit_premium": cap * 2,
        }

    async def _input_peers(refs):
        return {int(r): peers[int(r)] for r in refs if int(r) in peers}, []

    class _Me:
        pass

    me = _Me()
    me.premium = premium

    class _C:
        async def get_me(self_inner):
            return me

    w._get_filters = _get_filters
    w._folder_limits = _limits
    w._input_peers = _input_peers
    w._client = _C()
    return w


def _folder(fid=2, title="gh", include=(), exclude=(), chatlist=False):
    inc = [t.InputPeerUser(user_id=u, access_hash=u) for u in include]
    exc = [t.InputPeerUser(user_id=u, access_hash=u) for u in exclude]
    tt = t.TextWithEntities(text=title, entities=[])
    if chatlist:
        return t.DialogFilterChatlist(id=fid, title=tt, pinned_peers=[], include_peers=inc)
    return t.DialogFilter(
        id=fid, title=tt, pinned_peers=[], include_peers=inc, exclude_peers=exc
    )


@pytest.mark.asyncio
async def test_folder_edit_refuses_over_cap_and_writes_nothing():
    f = _folder(include=range(1, 100))  # 99 already in
    peers = {n: t.InputPeerUser(user_id=n, access_hash=n) for n in (500, 501)}
    w = _wrapper_with(f, cap=100, peers=peers)
    with pytest.raises(TlgrError) as e:
        await w.folder_edit(2, include_add=[500, 501])
    assert "cap of 100" in str(e.value)
    assert "Nothing was changed" in str(e.value)
    # the live object must be untouched, or a caller retrying would double-add
    assert len(f.include_peers) == 99


@pytest.mark.asyncio
async def test_folder_edit_premium_cap_allows_more():
    f = _folder(include=range(1, 100))
    peers = {n: t.InputPeerUser(user_id=n, access_hash=n) for n in (500, 501)}
    w = _wrapper_with(f, cap=100, premium=True, peers=peers)
    out = await w.folder_edit(2, include_add=[500, 501], dry_run=True)
    assert out["cap"] == 200
    assert out["after"]["include"] == 101


@pytest.mark.asyncio
async def test_folder_edit_dry_run_reports_without_updating():
    f = _folder(include=[1])
    peers = {2: t.InputPeerUser(user_id=2, access_hash=2)}
    w = _wrapper_with(f, peers=peers)
    out = await w.folder_edit(2, include_add=[2], dry_run=True)
    assert out["include_added"] == [2]
    assert out["before"]["include"] == 1 and out["after"]["include"] == 2
    assert "updated" not in out
    assert len(f.include_peers) == 1


@pytest.mark.asyncio
async def test_folder_edit_is_idempotent_on_an_existing_member():
    f = _folder(include=[7])
    peers = {7: t.InputPeerUser(user_id=7, access_hash=7)}
    w = _wrapper_with(f, peers=peers)
    out = await w.folder_edit(2, include_add=[7], dry_run=True)
    assert out["include_already"] == [7] and out["include_added"] == []


@pytest.mark.asyncio
async def test_shareable_folder_rejects_exclude():
    """A chatlist folder has no exclude_peers field — say so, don't crash."""
    w = _wrapper_with(_folder(chatlist=True))
    with pytest.raises(TlgrError) as e:
        await w.folder_edit(2, exclude_add=[5])
    assert "shareable" in str(e.value)


@pytest.mark.asyncio
async def test_unknown_folder_id_is_not_found():
    w = _wrapper_with(_folder(fid=2))
    with pytest.raises(ChatNotFoundError):
        await w.folder_edit(99, include_add=[1])


def test_dialog_extras_always_reports_archived():
    """False must be emitted, not omitted: a bulk archiver has to tell
    'already archived' from 'not archived' from 'we never looked'."""
    class D:
        unread_count = 0
        dialog = None
        message = None
        archived = False

    d = D()
    assert ClientWrapper._dialog_extras(d)["archived"] is False
    d.archived = True
    assert ClientWrapper._dialog_extras(d)["archived"] is True
