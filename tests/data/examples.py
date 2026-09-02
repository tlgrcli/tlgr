"""The JSON examples from ARCHITECTURE §3.8, verbatim, as test fixtures.

They are kept as raw strings rather than dicts so that a change to the wire
format has to change the *document's* text here, not a Python literal that
merely resembles it.
"""

from __future__ import annotations

MESSAGE_GET = """
{
  "ok": true,
  "op": "message.get",
  "account": "work",
  "result": {
    "id": 1042,
    "chat_id": -1001234567890,
    "date": "2026-09-02T09:14:07Z",
    "date_unix": 1788340447,
    "text": "ping \\u2014 did the deploy land?",
    "out": false,
    "kind": "message",
    "sender_id": 777123,
    "sender": {"id": 777123, "raw_id": 777123, "kind": "user", "title": "Sara N",
               "username": "saran"},
    "entities": [{"type": "code", "offset": 5, "length": 4}],
    "reactions": {"counts": {"\\ud83d\\udc4d": 2, "custom:5451234567890": 1},
                  "mine": ["\\ud83d\\udc4d"], "total": 3},
    "reply_to": {"message_id": 1039, "top_message_id": 12, "forum_topic": true},
    "link": "https://t.me/c/1234567890/1042"
  },
  "meta": {"request_id": "01J9Z7", "elapsed_ms": 84, "flood_wait_slept": 0, "warnings": []}
}
"""

MESSAGE_LIST = """
{
  "ok": true, "op": "message.list", "account": "work",
  "result": [
    {"id": 88, "chat_id": -1001111, "date": "2026-09-02T08:00:00Z", "date_unix": 1788336000,
     "text": "", "kind": "message", "sender_id": -1001111,
     "media": {"kind": "voice", "tl_type": "MessageMediaDocument", "mime_type": "audio/ogg",
               "duration": 17, "waveform": true, "size": 41233, "dc_id": 4}},
    {"id": 87, "chat_id": -1001111, "date": "2026-09-02T07:58:12Z", "date_unix": 1788335892,
     "text": "", "kind": "service",
     "action": {"type": "chat_add_user", "tl_type": "MessageActionChatAddUser",
                "user_ids": [777123]}}
  ],
  "page": {"has_more": true, "next_cursor": "eyJ2IjoxLCJvcCI6", "total": 4120},
  "meta": {"request_id": "01J9Z8", "elapsed_ms": 212, "flood_wait_slept": 0, "warnings": []}
}
"""

ERROR = """
{
  "ok": false, "op": "message.send", "account": "work",
  "error": {
    "code": "RATE_LIMITED", "message": "A wait of 42 seconds is required", "exit_code": 7,
    "retryable": true, "wait_seconds": 42,
    "rpc": {"code": 420, "message": "FLOOD_WAIT_42", "method": "messages.sendMessage"},
    "hint": "Retry after 42s, or raise --flood-wait-max to let the daemon sleep it off.",
    "request_id": "01J9Z9"
  }
}
"""

EVENT = """
{"seq":91824,"ts":"2026-09-02T09:14:07Z","account":"work","type":"message_new",
 "chat_id":-1001234567890,"sender_id":777123,"self_origin":false,
 "payload":{"message":{"id":1042}}}
"""
