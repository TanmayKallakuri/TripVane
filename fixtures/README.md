# Replay fixtures

Synthetic honeypot test data for `make replay`. None of it was captured from a real source.

- `<name>.jsonl`: a recorded session, one event per line, with exactly one `input_received`.
- `<name>.script.json`: the scripted model responses the mock client plays, in order.
- `<name>.expected.jsonl`: the events the decoy agent must emit (compared without `ts` and `session_id`).
- `system.md`: the replay system prompt for the fictional company Quillstone Software.

Addresses use the reserved `.example` domains and documentation IP ranges (RFC 5737). The canary
values in the expected files are fake, use the invented `tvk_live_` prefix, and are not
credentials for anything.
