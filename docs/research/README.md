# Research notes (2026-09)

Working notes produced while planning tlgr v2 by analysing the official Telegram
clients (tdesktop layer 229, TDLib, Telegram-Android), Telethon 1.44 (layer 227)
and the MTProto/API documentation on core.telegram.org. They are kept verbatim as
contributor reference; the binding design lives in `docs/design/`.

| File | What it is |
|---|---|
| `tlgr_audit.md` | Production-readiness audit of the v1 codebase: 85 findings (4 S0, 20 S1) with `file:line`, and the target architecture that `docs/design/ARCHITECTURE.md` refines |
| `mtproto_protocol_notes.md` | MTProto 2.0 / Telegram API engineering reference: auth keys, sessions, the update system (pts/qts/seq, getDifference), files, entities and access hashes, flood limits, auth flows, `initConnection` identity, takeout, and a 25-item checklist for a production-correct daemon |
| `telethon_capabilities.md` | Telethon as the engine: high-level API map, raw-call cookbook, events, sessions, files, formatting, 22 known pitfalls, the layer 227→229 diff, and recommendations |
| `error_taxonomy.md` | Telegram RPC error taxonomy distilled from the API docs, used to build the error-mapping table |

Paths written as `analysis/...` refer to the (uncommitted) analysis workspace the
notes were produced in; the feature catalog derived from it is published as
`docs/design/parity-catalog.json`.
