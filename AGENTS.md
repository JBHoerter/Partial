# Partial

Local-first AI session tracker with git commit checkpoints. Independently
implemented, inspired by the MIT-licensed Entire project
(https://github.com/entireio/cli); no upstream code or assets are used.
Zero runtime dependencies; Python >= 3.11; stdlib only
(sqlite3, argparse, subprocess, http.server).

## Architecture

- `partial/models.py` — `Event` dataclass, agent/kind constants, scoped
  session ids (`sha256(repo, agent, native_id)`), deterministic import ids.
- `partial/privacy.py` — best-effort recursive redaction of sensitive keys
  and common secret formats; sensitive-path detection for diffs.
- `partial/store.py` — SQLite store (WAL, busy_timeout=5000, state dir
  0700, files 0600). Tables: repositories, sessions, events, checkpoints,
  checkpoint_links, pending_paths, reviews. Bundle export/import
  (schema v1) and per-checkpoint `checkpoint_bundle()` transport bundles.
- `partial/adapters.py` — hook payload normalization (Devin/Claude),
  Codex exec JSONL events, and explicit file importers (canonical,
  Devin ATIF/hooks JSONL, Claude transcript JSONL, Codex exec/rollout
  JSONL, ChatGPT export JSON).
- `partial/git.py` — repo discovery, checkpoint capture (diff capped at
  2 MiB, sensitive paths omitted, first-parent merge diffs), post-commit
  hook install, Devin `.devin/hooks.v1.json` merge, Claude settings file
  merge, metadata branch persistence via plumbing (`read-tree`/
  `update-index`/`write-tree`/`commit-tree`/CAS `update-ref`) on
  `refs/heads/partial/checkpoints/v1`, and push/pull sync.
- `partial/auth.py` — workspace token (`PARTIAL_TOKEN` env or
  `<home>/server-token`, generated 0600, never rotated silently).
- `partial/handoff.py` — shared Markdown handoff formatter (recorded
  context only, no generated instructions).
- `partial/server.py` — `http.server.ThreadingHTTPServer` JSON API +
  static file whitelist. Bearer token or 12h HttpOnly SameSite=Strict
  session cookie (login via `POST /api/login`); Host/Origin allowlists,
  per-IP login rate limit, 16 MiB body cap, security headers, no CORS.
- `partial/demo.py` — synthetic demo fixture (temp store, only used by
  `serve --demo`; read-only API).
- `partial/static/` — landing page (`index.html`) and the workspace SPA
  (`app.html`, `app.js`, `styles.css`, `favicon.svg`); native ES-module
  JS, no framework/build, textContent-only rendering of untrusted text.
- `partial/cli.py` — argparse CLI (entry point `partial`).

## Commands

```
partial enable [--agent devin|claude|codex|chatgpt|all]
partial disable
partial status [--json]
partial doctor [--json]
partial hook <devin|claude|git> [event]     # reads JSON from stdin
partial import --agent AGENT FILE [--session-id ID]
partial sessions [--agent A] [--search Q] [--json]
partial session ID [--json]
partial checkpoint [--session ID]... [--commit REF]
partial checkpoints [--json]
partial export [--repo-only]
partial handoff SESSION
partial sync [--push] [--pull] [--remote NAME]
partial run codex [args...]    # wraps `codex exec --json`
partial run claude [args...]   # wraps `claude --settings <generated>`
partial run devin [args...]
partial serve [--host H] [--port P] [--public-url URL] [--demo]
partial auth token           # prints the workspace access token
partial upload SERVER_URL [--repo-only]   # token via PARTIAL_SERVER_TOKEN
partial ingest-bundle FILE   # local bundle import
```

Global flags: `--home` = partial state DIRECTORY (env PARTIAL_HOME; db
lives at `<home>/partial.db`; default `$XDG_DATA_HOME/partial` or
`~/.local/share/partial`), `--repo` (env DEVIN_PROJECT_DIR), `--version`.

## Install and run

```
uv venv .venv
uv pip install --python .venv/bin/python -e .
source .venv/bin/activate
partial enable --agent all
partial serve
```

In another activated terminal, run `partial auth token` to get the
login token. Keep the virtual environment activated when running
agents so the installed `partial` hook command is on PATH. For
persistent installation instead, use `uv tool install .`.

The server targets a single trusted workspace: every token holder is a
workspace member, not a separate account. Bind loopback by default;
for remote access put it behind a TLS reverse proxy and pass
`--public-url https://…` with an explicit `PARTIAL_TOKEN`. Bundles
contain recorded session context — only upload/import them into
workspaces you trust; there is no telemetry.

## Tests

```
.venv/bin/python -m unittest discover -s tests -v
```

Browser smoke test (optional dev dependency, not a runtime
requirement):

```
uv pip install --python .venv/bin/python -e '.[browser]'
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/browser_smoke.py
```

`partial run *` requires `partial enable` to have been run in the repo;
children execute with cwd = repo root and PARTIAL_HOME set so nested
`partial hook` invocations reach the same store. `partial enable` also
persists the chosen home directory in the repo's local Partial config so
bare hook invocations find the right DB.

## Server

`partial serve` binds loopback by default and prints only the URL —
tokens come from `partial auth token` or `PARTIAL_TOKEN`. Non-loopback
binds require `--public-url https://…` and an explicit `PARTIAL_TOKEN`;
the stdlib server is meant to sit behind a TLS reverse proxy. `partial
upload` posts an exported bundle to `POST /api/bundles` with a bearer
token, refuses redirects, and requires HTTPS off-loopback.
`partial serve --demo` uses an isolated temp store with synthetic data;
all mutation endpoints return 403.

## Devin hook format

`.devin/hooks.v1.json` is the hook map itself (not wrapped in `hooks`).
Each event maps to a list of matcher/hook groups:

```json
{"SessionStart": [{"matcher": "", "hooks": [{"type": "command",
  "command": "partial hook devin SessionStart", "timeout": 10}]}]}
```

Events: SessionStart, UserPromptSubmit, PostToolUse, Stop, SessionEnd,
PostCompaction. Hook commands receive a JSON object on stdin with
`hook_event_name`, `session_id`, `prompt_id`, `tool_name`, `tool_input`,
`tool_response` ({success, output, error}), `prompt`, and (newer) Stop
`last_assistant_message`. `DEVIN_PROJECT_DIR` is the project root.
Claude uses the same group shape nested under a `"hooks"` key inside the
generated `.devin/partial/claude-settings.json`. Merge/remove is by exact
owned command equality only; foreign hooks are never touched.

## Source references

- Entire (MIT): https://github.com/entireio/cli
- Codex `exec --json` events: thread.started, turn.started/completed
  (usage), item.started/completed (item {id,type,text,command,
  aggregated_output,changes:[{path,kind}]}).
- ChatGPT export: list of conversations with `mapping` tree and
  `current_node`; ancestry traversal preferred, deterministic
  topological order otherwise.

## Known scope / gaps

- Review notes are API-local (not synced via the Git metadata branch).
- Auto-linking of sessions to commits is conservative: only file paths
  observed via successful mutating edit/write/patch tool inputs (and
  Codex `changes`) are tracked; pre-existing dirty files and
  shell-driven edits are not claimed. Link method is recorded as
  `observed-worktree-overlap`.
- Sync reconciles divergent metadata branches by importing the union
  of checkpoint bundles and creating a two-parent merge commit on the
  metadata ref; conflicting checkpoint identities fail safely and are
  never overwritten.
- Capture surfaces and limitations: Claude's live response is captured
  at Stop unless explicitly imported; Devin hooks capture final
  messages and tool activity, not every streamed message; shell-only
  path attribution is not captured; ChatGPT is import-only.
- Repo identity is derived from the normalized remote when one exists,
  else the resolved common git dir; registering a repo before its first
  remote exists yields the gitdir-based id.
- Redaction is best-effort, not a guarantee.
