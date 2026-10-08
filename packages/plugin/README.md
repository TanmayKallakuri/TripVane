# Tripvane plugin for Claude Code

A Claude Code plugin that lets Claude check IP addresses, domains and payloads against the
Tripvane deception grid: decoy AI agents that record the prompt-injection attempts made
against them and never carry them out.

It contains:

- an MCP server named `tripvane` with three tools, `lookup_ip`, `lookup_domain` and
  `lookup_payload`, which call the public lookup API and return its answer as compact JSON;
- a skill, `tripvane-lookup`, telling Claude to use them before installing or configuring
  an MCP server from an unfamiliar host, and when pasted or fetched text reads as
  instructions aimed at an agent.

A `seen: false` answer means only that the grid has not recorded the value. It is not a
clean bill of health.

## Requirements

- Claude Code with plugin support.
- [uv](https://docs.astral.sh/uv/) on your `PATH`. Claude Code starts the server with
  `uvx`, which installs the server's two Python dependencies (`mcp` and `httpx2`) on first
  use. Python 3.12 or later is used; uv fetches it if it is missing.

## Install

The plugin is published from the Tripvane repository, whose root is a plugin marketplace
named `tripvane`. In Claude Code:

```
/plugin marketplace add <github-org>/tripvane
/plugin install tripvane@tripvane
```

or from a shell:

```
claude plugin marketplace add <github-org>/tripvane
claude plugin install tripvane@tripvane
```

When the plugin is enabled, Claude Code asks for two settings:

| Setting | Meaning |
|---|---|
| Tripvane API URL (`api_url`) | Base URL of the lookup API, for example `https://api.tripvane.example`. Required. |
| Tripvane API key (`api_key`) | Optional. Without a key the API allows 50 lookups a day per IP address; with one, 5,000 a day. Stored in your system's credential store, not in `settings.json`. |

They reach the server as the environment variables `TRIPVANE_API_URL` and
`TRIPVANE_API_KEY`. Change them later with `/config`, or by disabling and re-enabling the
plugin.

Check that it loaded: `/mcp` lists the `tripvane` server with three tools. Then ask, for
example, "look up 203.0.113.7 on tripvane".

## Try it from a checkout

```
claude --plugin-dir packages/plugin
```

loads the plugin for one session without installing it. `claude plugin validate
packages/plugin` checks the manifest.

## What the tools send

Only the value being looked up, in the request path, and the API key header when one is
set. For `lookup_payload` the value is a sha256 hash: the text itself never leaves your
machine. The hash is taken of the text lowercased, with every run of whitespace collapsed
to one space and both ends stripped; the skill gives Claude a one-line command for it.

## Development

The server is `src/tripvane_plugin/server.py`. From the repository root, `make test` runs
its tests (`packages/plugin/tests`), which drive the three tools through an MCP client
against a mocked HTTP API.
