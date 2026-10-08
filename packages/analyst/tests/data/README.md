# Analyst test data

Synthetic honeypot test data for the analyst tests. Nothing here was captured from a real source.

- `payloads.json`: short captured inputs as a decoy would record them. The instructions in them
  are labelled as honeypot test data, addresses use the reserved `.example` domain and nothing in
  them is a working credential.
- `responses/`: Messages API responses in the shape the API returns them, played back by the fake
  model client in place of claude-haiku-5-5 (gate), claude-sonnet-5-5 (tagger) and
  claude-opus-5-5 (novel). `batch_*.json` and `batch_results.jsonl` stand in for the Message
  Batches API.
