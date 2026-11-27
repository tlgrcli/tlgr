# Working on tlgr

## Destructive operations: permanent rules

On 2026-11-27 an agent ran a new regression test for `media storage clear
--chat /` against the unfixed code to watch it fail. The code turned `/` into
the scope, walked the whole disk and unlinked every file the user owned,
with every error suppressed. Most of the machine had to be rebuilt by hand
and some files are gone for good. These rules have no exceptions:

1. Never run code that deletes, moves, truncates or overwrites files against
   an unfixed or unreviewed implementation, not to "see it fail" and not
   "just once". If a destructive bug truly needs reproducing, ask the user
   first and do it in a disposable container.
2. Never check out or test `origin/main` (or any other branch) to confirm a
   failing test for a destructive fix.
3. Every test that touches a delete path runs under the autouse delete guard
   (`tests/delete_guard.py`, installed by `tests/conftest.py`), which makes
   any delete, rename or move outside the run's temp dirs raise before it
   touches the disk. Never disable, bypass or weaken it, and never run tests
   in a checkout that does not have it.
4. Never run `tlgr media storage clear` or any other destructive tlgr command
   on the user's real machine unless the user approved that exact command
   line.
5. Anything that writes or deletes outside the repository or a temp
   directory needs the user's explicit OK first.
6. Never wrap a delete in `contextlib.suppress(OSError)` (or any blanket
   `except`) without recording what failed.
7. Subagents follow these rules too: put them in every subagent's
   instructions.

## Never touch a live installation

`~/.tlgr` (or whatever `TLGR_HOME` a running daemon uses) holds live
accounts. Never point tests, a dev daemon or tlgr commands at it: `export
TLGR_HOME=$(mktemp -d)` first, and never set `TLGR_ALLOW_PRODUCTION_HOME`.
