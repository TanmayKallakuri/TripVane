# Replay fixtures

Synthetic honeypot test data for `make replay`. None of it was captured from a real source.

- `<name>.jsonl`: a recorded session, one event per line, with exactly one `input_received`.
- `<name>.script.json`: the scripted model responses the mock client plays, in order.
- `<name>.expected.jsonl`: the events the decoy agent must emit (compared without `ts` and `session_id`).
- `system.md`: the replay system prompt for the fictional company Quillstone Software.

Addresses use the reserved `.example` domains and documentation IP ranges (RFC 5737). The canary
values in the expected files are fake, use the invented `tvk_live_` prefix, and are not
credentials for anything.

## GitHub webhook deliveries (`github/`)

Recorded webhook bodies for the GitHub triage sensor, replayed by `make replay` through the sensor
app itself: each body is signed with a replay-only secret and posted to `/webhook`.

- `<event>.<action>.json`: the webhook body; the file name gives the `X-GitHub-Event` header.
- `pull_request.opened.diff`: the diff the replay's fake diff source returns instead of GitHub.
- `<name>.script.json` and `<name>.expected.jsonl`: as above.

The repository, logins and ids are invented, the injected instructions are marked as synthetic
honeypot test data, and the sender address is a documentation address (RFC 5737).
