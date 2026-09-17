# Partial

**Partial** is a self-hosted, local-first tracker for AI-assisted software
work. It records agent sessions, links them to Git commits as checkpoints,
keeps per-line attribution evidence, preserves native agent resume state, and
turns captured history into a searchable repository memory.

It is an independent implementation inspired by the MIT-licensed Entire
project. It uses no upstream Entire code or assets and is not affiliated with
Entire.

> Partial does **not** provide Git hosting. It works with the repositories and
> remotes you already have.

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Agent integrations](#agent-integrations)
- [Checkpoints and Git metadata](#checkpoints-and-git-metadata)
- [Line attribution](#line-attribution)
- [Native sessions and resume](#native-sessions-and-resume)
- [Repository memory](#repository-memory)
- [Workflows](#workflows)
- [Web dashboard](#web-dashboard)
- [Accounts and workspaces](#accounts-and-workspaces)
- [Bundles, sync, and upload](#bundles-sync-and-upload)
- [MCP server](#mcp-server)
- [CLI reference](#cli-reference)
- [Configuration](#configuration)
- [Security and privacy](#security-and-privacy)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Scope and limitations](#scope-and-limitations)
- [License](#license)

## What it does

Partial gives each repository a durable activity record:

- Captures prompts, responses, tool calls, errors, compaction events, and
  reported token usage from supported agents.
- Groups events into sessions, including nested Devin sub-agent sessions.
- Creates a checkpoint when relevant commits land, storing the commit SHA,
  branch, message, author, changed files, diff, linked sessions, token totals,
  and attribution summary.
- Keeps checkpoint metadata on a separate Git metadata ref instead of adding
  commits to your code branch.
- Provides evidence-based line attribution for lines observed through
  supported tool hooks or explicit capture commands.
- Registers native session state so supported agents can be resumed later.
- Indexes captured sessions, checkpoints, decisions, and committed code into
  a searchable repository memory.
- Exposes deterministic and optional AI-assisted workflows for questions,
  reviews, investigations, dispatches, and expert lookup.
- Provides a multi-tenant web UI and JSON API with isolated workspaces.
- Exposes read-only repository memory through an MCP stdio server.

## Architecture

Partial is deliberately dependency-light:

- **CLI:** Python stdlib CLI (`partial`) for capture, imports, checkpoints,
  memory, workflows, sync, and administration.
- **Storage:** SQLite databases with WAL mode and restrictive file
  permissions.
- **Git transport:** checkpoint metadata lives on
  `refs/heads/partial/checkpoints/v1` and can be pushed/pulled separately from
  code history.
- **Server:** `http.server.ThreadingHTTPServer` JSON API plus static web app.
- **Accounts:** separate account database plus one isolated workspace database
  per tenant.
- **Dashboard:** dependency-free HTML/CSS/JavaScript SPA served by the same
  process.
- **MCP:** line-delimited JSON-RPC 2.0 over stdio with read-only tools.
- **Optional AI provider:** OpenAI-compatible embeddings/chat are only used
  when explicitly requested and configured.

At a high level:

```text
Devin / Claude / Codex / ChatGPT
        | hooks, wrappers, or imports
        v
 normalized events -> local SQLite store
        |                    |
        |                    +--> sessions, sub-agents, usage, native state
        v
 Git commit -> checkpoint -> linked sessions + diff + attribution
        |
        +--> repository memory: documents, code graph, decisions
        |
        +--> web dashboard, API, CLI, workflows, MCP tools
```

## Requirements

- Python **3.11 or newer**
- Git
- No runtime Python dependencies
- Optional browser-test dependency: Playwright
- Optional external AI features: an OpenAI-compatible API endpoint and API key
- Supported agent CLIs only when you want live capture:
  - `devin`
  - `claude`
  - `codex`

Partial uses SQLite FTS5 for lexical memory search when available. If your
SQLite build lacks FTS5, the rest of Partial still works, but memory search is
limited/unavailable; `partial doctor` reports this.

## Install

### Development install

```bash
git clone <your-partial-repo-url>
cd Partial

uv venv .venv
uv pip install --python .venv/bin/python -e .
source .venv/bin/activate

partial --version
partial doctor
```

### Persistent tool install

```bash
uv tool install .
partial --version
```

### Build and install a wheel

```bash
uv build
uv pip install dist/partial-*.whl
```

The package has no runtime dependencies.

## Quick start

### 1. Try the read-only demo

```bash
partial serve --demo
```

The command prints the root local URL, normally:

```text
http://127.0.0.1:4310
```

The landing page there links to the dashboard at `/app`.

Demo mode uses an isolated temporary store with synthetic repositories,
sessions, a Devin sub-agent, checkpoints, attribution, memory, graph data, and
a decision. It creates no accounts and rejects all mutations.

### 2. Enable Partial in a real repository

Run from the repository you want to track:

```bash
cd /path/to/your/repo
partial enable --agent all
```

This installs only Partial-owned hook entries:

- a Git `post-commit` hook
- Devin lifecycle hooks in `.devin/hooks.v1.json`
- Claude hook settings under `.devin/partial/claude-settings.json`

Existing foreign hooks are preserved. Codex and ChatGPT do not use the same
hook file; use the wrapper/import flows described below.

`partial enable` preflights every file it would write and aborts without
changing anything when it finds:

- an unmanaged foreign `.git/hooks/post-commit`
- malformed `.devin/hooks.v1.json` or Claude settings JSON
- a foreign `.devin/skills/partial-memory/SKILL.md` when using
  `--memory-skill` or `--skill-only`

Move or merge the conflicting file and re-run `partial enable`; for a
foreign post-commit hook you can instead chain `partial hook git
post-commit` from it manually. Foreign hooks are otherwise left untouched.

To install only the optional repository-memory skill for Devin:

```bash
partial enable --skill-only
```

To install hooks plus that skill:

```bash
partial enable --agent all --memory-skill
```

### 3. Capture sessions

```bash
# Devin: hooks capture lifecycle/tool events when enabled.
partial run devin

# Codex: streams `codex exec --json` events into Partial.
partial run codex "your task"

# Claude: runs with generated Partial hook settings.
partial run claude

# Import an existing transcript/export.
partial import --agent chatgpt export.json
partial import --agent claude transcript.jsonl
partial import --agent codex codex-events.jsonl
partial import --agent devin trajectory.atif.json
```

### 4. Commit normally

```bash
git add .
git commit -m "Implement feature"
```

The post-commit hook creates a checkpoint only when the commit overlaps
file paths observed through successful mutating tool calls in observed
sessions; a commit with no linkable sessions produces no checkpoint. When
created, the checkpoint contains the commit metadata, diff, linked
sessions, and available attribution/token summaries.

You can also checkpoint a commit explicitly:

```bash
partial checkpoint --commit HEAD --session SESSION_ID
```

### 5. Start the workspace server

```bash
partial serve
```

The command prints the root local URL, normally:

```text
http://127.0.0.1:4310
```

The landing page there links to the dashboard at `/app`.

On first run, either:

- use the setup form with the bootstrap token printed by
  `partial auth token`, or
- create the owner locally:

```bash
partial account create --email you@example.com --name "You"
```

After setup, sign in with email/password. Create additional workspaces,
invites, and API tokens from Settings.

## Agent integrations

| Agent | Capture path | Transcript | Usage | Sub-agents | Native state | Notes |
|---|---|---:|---:|---:|---:|---|
| Devin | Native lifecycle hooks or ATIF import | Yes | Yes, when reported | Yes | `native-id`; ATIF context archive | Strongest integration |
| Claude | Hook wrapper or transcript import | Yes | Yes, when reported | Where present in transcript | `claude-jsonl` | Resume uses `claude -r` |
| Codex | `partial run codex`, exec JSONL, rollout import | Yes | Yes | No; activity is namespaced into one session | `codex-rollout`/`native-id` | Resume uses `codex resume` |
| ChatGPT | Explicit export import | Yes | Usually absent | Conversation structure only | No | No live ChatGPT hooks |

### Devin

`partial enable --agent devin` merges Partial commands into
`.devin/hooks.v1.json` without wrapping the map in an extra `hooks` key and
without changing unrelated hooks.

Handled events include:

- `SessionStart`
- `UserPromptSubmit`
- `PreToolUse`
- `PostToolUse`
- `Stop`
- `SessionEnd`
- `PostCompaction`

`PreToolUse` records a pre-edit attribution snapshot but does not store a
timeline event. `PostToolUse` records the tool event and closes the
attribution window.

Devin payloads and ATIF exports can identify sub-agent trajectories. Partial
materializes them as child sessions linked to the parent session. A sub-agent
that only reviewed or searched appears as session context; it is only marked
as contributing lines when file evidence exists.

### Claude

```bash
partial run claude
```

This invokes `claude --settings .devin/partial/claude-settings.json`. The
settings file contains Partial-owned hook entries and can coexist with other
settings. Claude transcript JSONL can also be imported.

All `partial run` wrappers require `partial enable` in the repository
first; the agent runs with the repository root as its working directory
and `PARTIAL_HOME` set so nested `partial hook` calls reach the same
store.

### Codex

```bash
partial run codex "explain this module"
```

Partial invokes:

```bash
codex exec --json "explain this module"
```

and parses the bounded JSON event stream while the command runs. Thread IDs
are registered as native session IDs when available.

`partial run codex` likewise requires `partial enable` first and runs
`codex` with the repository root as its working directory and
`PARTIAL_HOME` set.

### ChatGPT

ChatGPT is supported through explicit export import:

```bash
partial import --agent chatgpt conversations.json
```

The importer understands the exported `mapping`/`current_node` conversation
tree. It records the transcript history but does not claim live tool usage or
native state.

## Checkpoints and Git metadata

A checkpoint is Partial's record for a commit or commit-like change:

- checkpoint ID
- commit SHA
- branch
- commit message
- author
- capture timestamp
- changed files
- unified diff, capped and sanitized
- linked session IDs and link methods
- agent list
- additions/deletions
- AI/coverage percentages when attribution exists
- token usage totals when usage events exist
- reviews and attribution reports

Checkpoint metadata is persisted on the dedicated metadata ref:

```text
refs/heads/partial/checkpoints/v1
```

That means code branches stay clean while checkpoint history can still be
shared through a normal Git remote.

Useful commands:

```bash
partial checkpoint --commit HEAD
partial checkpoint --commit HEAD --session SESSION_ID
partial checkpoints
partial attach SESSION_ID --commit HEAD
partial sync --push
partial sync --pull
```

`partial sync` pushes/pulls only the metadata ref. It does not create or host
Git repositories and does not rewrite code history.

## Line attribution

Partial's attribution is intentionally evidence-based.

Devin/Claude hook flows capture pre-tool and post-tool file snapshots for
supported edit-like operations. The attribution engine compares those
snapshots with the final checkpoint diff and labels changed lines as:

- agent-authored
- human-side/inferred
- unknown/unobserved

The dashboard shows the attribution summary on checkpoint pages and badges on
individual diff lines where evidence exists. CLI access:

```bash
partial blame path/to/file.py
partial blame path/to/file.py --line 20-45
partial why path/to/file.py --line 42
partial why path/to/file.py --line 42 --json
```

For edits outside automatic hooks, manually mark a file state before and
after an edit:

```bash
partial capture before --session SESSION_ID --file src/app.py --key change-1
# make the edit
partial capture after --session SESSION_ID --file src/app.py --key change-1
```

Attribution percentages are estimates over measured changed lines. They are
not keystroke telemetry and should not be treated as proof of intent. Unknown
and unobserved lines remain explicit.

## Native sessions and resume

Partial can register a session's native identifier or state file separately
from its normalized event history.

```bash
partial native register --agent devin --session-id SESSION_ID
partial native register --agent claude --session-id SESSION_ID \
    --file ~/.claude/projects/example/session.jsonl --archive
partial native list
partial native show SESSION_ID
partial resume SESSION_ID
```

`partial resume` prints a deterministic resume plan. Add `--run` to execute
it:

```bash
partial resume SESSION_ID --run
```

Current resume forms:

- Devin: `devin --resume <native-id>`
- Claude: `claude -r <native-id>`
- Codex: `codex resume <native-id>`

Archived Claude/Codex state files can be restored explicitly:

```bash
partial resume SESSION_ID --restore-native --trust-native-state
```

Restore refuses to overwrite existing files unless you explicitly choose a
safe target and trust the archived state.

Devin ATIF exports are portable context, not a documented Devin session-state
restore format. Partial can import and browse them, but does not claim to
reinsert them into Devin's private native store.

## Repository memory

The "repository with a brain" layer turns captured history and committed code
into bounded evidence.

Build or refresh the index:

```bash
partial index
partial index --all
```

Semantic embeddings are opt-in and may call a paid external API:

```bash
export PARTIAL_OPENAI_API_KEY=...
export PARTIAL_AI_BASE_URL=https://your-compatible-endpoint
partial index --semantic
```

Memory documents can include:

- session prompts/responses/tool activity
- checkpoint metadata and diffs
- decisions
- committed code chunks
- code symbols and relationships

Search and retrieve evidence:

```bash
partial search "cursor pagination"
partial search "activity_feed" --code
partial context "why did we choose cursor pagination?"
```

Ask a question:

```bash
# deterministic evidence packet only
partial ask "why did we choose cursor pagination?"

# send bounded evidence to the configured provider
partial ask "why did we choose cursor pagination?" --run
```

Record a decision with source citations:

```bash
partial decision add \
  --title "Use cursor pagination" \
  --body "Offsets become unstable when rows are inserted during pagination." \
  --source DOCUMENT_ID
partial decision list
```

Inspect the code graph:

```bash
partial graph capabilities
partial graph search activity_feed
partial graph neighbors SYMBOL_ID
partial graph impact SYMBOL_ID
```

Find sessions/checkpoints associated with a path or topic:

```bash
partial experts src/routes/activity.py
```

## Workflows

Workflows are persisted as runs, so planned and executed work remains visible
in the dashboard.

### Dispatch / recap

Create a deterministic recap of recorded checkpoint activity:

```bash
partial dispatch
partial dispatch --since 2026-09-01 --until 2026-09-17
partial dispatch --branch main
partial recap --since 2026-09-01
```

`--run` sends the bounded recap context to the configured provider instead of
only returning the deterministic local output.

### Review

```bash
partial review run --base main
partial review run --base main --agents codex,claude
partial review run --base main --run
partial review show RUN_ID
```

Without `--run`, Partial records a planned evidence-backed review. With
`--run`, it invokes the configured provider or explicitly selected installed
native agents under bounded output/time limits.

### Investigate

```bash
partial investigate run "why did login latency increase?"
partial investigate run "why did login latency increase?" \
  --seed src/auth/login.py --agents codex,claude --run
partial investigate show RUN_ID
```

## Web dashboard

`partial serve` starts the API and dashboard together:

```bash
partial serve --host 127.0.0.1 --port 4310
```

Main pages:

- **Overview:** repository/session/checkpoint totals and quick links
- **Repositories:** agents, counts, branches, latest activity, memory state
- **Repository detail:** stats, branch/search filters, Sessions and
  Checkpoints tabs
- **Sessions:** agent/model, role, status, checkpoint and event counts
- **Session detail:** prompt/response/tool timeline, event filters, token
  usage, native resume state, child sessions, linked checkpoints
- **Checkpoints:** commit/message/branch, agents, AI estimate, diff delta,
  file/session counts, author
- **Checkpoint detail:** metadata, token usage, attribution summary,
  Changes/Sessions tabs, review notes
- **Search:** transcript/event search
- **Memory:** indexed documents, lexical/semantic controls, status
- **Code graph:** symbol search, neighbors, impact
- **Decisions:** active/superseded records with source citations
- **Dispatches:** deterministic recaps and downloadable Markdown
- **Workflows:** planned/running/completed/failed runs
- **Integrations:** setup commands and capability details for each agent
- **Settings:** workspaces, members, roles, invites, API tokens, audit log,
  bundle tools

Dashboard/API actions that would call an external AI provider — semantic
indexing/search and workflow runs submitted with `run: true` — are rejected
unless an owner enables external AI under Memory settings. Native agents
cannot be executed through the server API; select them with the CLI
(`--agents`, `--run`) instead.

The dashboard renders untrusted session text through DOM text APIs, not HTML
injection.

## Accounts and workspaces

Partial supports multiple users and isolated workspaces on one server.

- First account is the owner.
- Additional users join by invitation.
- Roles: `owner`, `admin`, `member`, `viewer`.
- Each workspace gets its own SQLite store.
- API tokens are workspace-scoped and can be expiration-limited.
- Browser sessions are HttpOnly, SameSite=Strict, and expire.
- The `X-Partial-Workspace` header selects a workspace for API calls; tokens
  are pinned to their own workspace.

Create the owner locally:

```bash
partial account create --email owner@example.com --name "Owner"
```

Mint a workspace API token:

```bash
partial account token \
  --email owner@example.com \
  --workspace WORKSPACE_ID \
  --name ci-upload \
  --role member \
  --expires-days 90
```

Use it for uploads:

```bash
export PARTIAL_SERVER_TOKEN=...
partial upload https://partial.example.com --workspace WORKSPACE_ID
```

## Bundles, sync, and upload

Export all captured workspace data or only the current repository:

```bash
partial export > partial-bundle.json
partial export --repo-only > repo-bundle.json
```

Import locally:

```bash
partial ingest-bundle partial-bundle.json
```

Upload to another Partial server:

```bash
PARTIAL_SERVER_TOKEN=... partial upload https://partial.example.com
```

`partial upload` posts the bundle to `POST /api/bundles` with the
workspace API token from `PARTIAL_SERVER_TOKEN`. It requires HTTPS for
non-loopback servers, refuses redirects, disables environment proxies so
the `Authorization` header is never sent through one, and caps the
request at 16 MiB.

Bundles can carry repositories, sessions, events, checkpoints, links,
attribution reports, native registrations, memory documents, graph data,
decisions, workflow runs, and project metadata. Import validates identities,
cross-repository references, timestamps, sizes, and workspace isolation.

Checkpoint metadata can also travel through Git:

```bash
partial sync --push
partial sync --pull
partial sync --remote upstream
```

This uses the separate metadata ref; it is not Git hosting.

## MCP server

Start a read-only MCP stdio server:

```bash
partial mcp
```

Tools:

- `partial_search` — search memory documents
- `partial_context` — retrieve a bounded evidence packet
- `partial_document` — fetch a document by ID
- `partial_graph` — symbol search or neighbor lookup

The MCP server writes only protocol responses to stdout and exposes no shell,
write, or admin tools. Repository scoping is supported internally when the
server is launched for a specific repository context.

## CLI reference

Global flags:

```text
--home DIR     Partial state directory (or PARTIAL_HOME)
--repo PATH    Repository path (or DEVIN_PROJECT_DIR)
--version      Print version
```

Core capture:

```text
partial enable [--agent devin|claude|codex|chatgpt|all]
               [--memory-skill|--skill-only]
partial disable
partial status [--json]
partial doctor [--json]
partial hook <devin|claude|codex|git> [event]
partial import --agent AGENT FILE [--session-id ID]
partial run codex [args...]
partial run claude [args...]
partial run devin [args...]
partial stop SESSION_ID
partial attach SESSION_ID --commit REF
```

Sessions and checkpoints:

```text
partial sessions [--agent A] [--search Q] [--limit N] [--json]
partial session SESSION_ID [--json]
partial checkpoint [--session ID]... [--commit REF] [--json]
partial checkpoints [--branch B] [--limit N] [--json]
partial handoff SESSION_ID
partial tokens [--session ID|--checkpoint ID]
```

Attribution and native state:

```text
partial capture before|after --session ID --file REL --key KEY
partial blame FILE [--line N|START-END] [--json]
partial why FILE [--line N] [--json]
partial native register --agent devin|claude|codex --session-id ID
    [--file PATH] [--archive]
partial native list [--json]
partial native show SESSION_ID
partial resume SESSION_ID [--run] [--worktree PATH]
    [--restore-native] [--trust-native-state] [--target-root DIR]
```

Memory and workflows:

```text
partial index [--all] [--semantic]
partial search QUERY [--code] [--semantic] [--all-repos] [--json]
partial context QUERY [--semantic] [--all-repos] [--json]
partial ask QUERY [--all-repos] [--run]
partial decision add --title T --body B [--source ID]... [--supersedes ID]
partial decision list [--json]
partial graph capabilities
partial graph search QUERY
partial graph neighbors SYMBOL_ID
partial graph impact SYMBOL_ID
partial dispatch [--since D] [--until D] [--branch B] [--run]
partial recap [--since D] [--until D] [--branch B] [--run]
partial review run [--base REF] [--query Q] [--agents A,B] [--run]
partial review show RUN_ID
partial investigate run QUERY [--seed FILE] [--agents A,B] [--run]
partial investigate show RUN_ID
partial experts SCOPE [--json]
```

Server, accounts, and transport:

```text
partial serve [--host H] [--port P] [--public-url URL] [--demo]
partial auth token
partial account create --email E --name N [--password-stdin]
partial account token --email E --workspace ID --name N
    [--role member|viewer] [--expires-days N] [--password-stdin]
partial export [--repo-only]
partial ingest-bundle FILE
partial upload SERVER_URL [--repo-only] [--workspace ID]
partial sync [--push] [--pull] [--remote NAME]
partial mcp
partial project create NAME
partial project list
partial project attach PROJECT_ID [--repo-id ID]
partial plugin register NAME --command ABS --sha256 DIGEST
partial plugin list
partial plugin run NAME -- ARGS
partial configure --show
```

## Configuration

### State location

Default state directory:

```text
$XDG_DATA_HOME/partial
```

or, when `XDG_DATA_HOME` is unset:

```text
~/.local/share/partial
```

Override it globally:

```bash
export PARTIAL_HOME=/secure/partial-home
partial status
```

or per invocation:

```bash
partial --home /secure/partial-home status
```

A repository enabled with `partial enable` records the chosen home directory
in its local Partial config so bare hook invocations find the right store.

### Environment variables

| Variable | Purpose |
|---|---|
| `PARTIAL_HOME` | State directory containing account/workspace data |
| `DEVIN_PROJECT_DIR` | Repository root when invoked from Devin hooks |
| `PARTIAL_TOKEN` | Explicit bootstrap token for non-demo first setup |
| `PARTIAL_SERVER_TOKEN` | Workspace API token used by `partial upload` |
| `PARTIAL_OPENAI_API_KEY` | API key for optional embeddings/chat calls |
| `PARTIAL_AI_BASE_URL` | OpenAI-compatible base URL; HTTPS or loopback HTTP |

### Remote server exposure

The built-in server is intentionally small and should sit behind a TLS
reverse proxy for non-loopback use:

```bash
PARTIAL_TOKEN="$(openssl rand -hex 32)" \
partial serve --host 0.0.0.0 --port 4310 \
  --public-url https://partial.example.com
```

Non-loopback serving requires an explicit bootstrap token and HTTPS public
URL. Put authentication, TLS termination, logging, and request limits at the
proxy layer appropriate for your deployment.

## Security and privacy

- State directories are created `0700`; sensitive state files are `0600`.
- Passwords use PBKDF2-HMAC-SHA256.
- Only token hashes are persisted.
- Sessions use HttpOnly, SameSite=Strict cookies.
- Workspace databases are physically separate.
- API tokens cannot perform account administration.
- Login/setup/invite endpoints are rate-limited.
- Request bodies are capped and Host/Origin allowlists are enforced.
- Unknown workspaces return 404; insufficient roles return 403.
- Event text and structured fields pass through best-effort secret
  redaction.
- Sensitive paths are excluded from diffs and memory indexing.
- Local repository roots and worktree paths are stripped from public API
  responses and exports where possible.
- Imported attribution is marked as an imported claim rather than verified
  local observation.
- The demo server is read-only and loopback-oriented.
- The web app uses a restrictive CSP and text-based DOM rendering.

Redaction is best-effort, not a guarantee. Do not place secrets in prompts,
file contents, commit messages, transcripts, or bundles you intend to share.

## Troubleshooting

### Check installation and repository state

```bash
partial doctor
partial status
partial configure --show
```

### Hooks did not capture anything

- Run commands from the intended repository.
- Ensure `partial enable --agent all` completed.
- Ensure the `partial` executable is on PATH for hooks.
- For Codex, use `partial run codex` or import a JSONL stream.
- For ChatGPT, use `partial import --agent chatgpt`.
- Inspect `partial doctor --json`.

### No checkpoint appeared after a commit

- Confirm the repository has Partial's Git post-commit hook installed.
- The hook only creates a checkpoint when the commit overlaps file paths
  observed through successful mutating tool calls in observed sessions; a
  commit with no linkable sessions is skipped by design.
- Run `partial checkpoint --commit HEAD` to create a checkpoint manually.
- Link a known session with `partial attach SESSION_ID --commit HEAD`.
- Check `partial checkpoints` and `partial status`.

### Memory search returns nothing

Run an explicit index first:

```bash
partial index
# or
partial index --all
```

Semantic search additionally requires external AI configuration and an
explicit semantic request.

### Resume fails

Use:

```bash
partial native show SESSION_ID
partial resume SESSION_ID
```

A native resume requires the referenced agent CLI and, for file-backed
formats, local state or an explicitly restored archive. Devin ATIF imports
are context records and do not provide native Devin state restoration.

### Browser tests cannot find Chromium

Install the optional browser dependencies and browser binary:

```bash
uv pip install --python .venv/bin/python -e '.[browser]'
.venv/bin/python -m playwright install chromium
```

## Development

Run the full unit suite:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Run browser smoke tests:

```bash
.venv/bin/python tests/browser_smoke.py
```

Useful checks:

```bash
python3 -m compileall -q partial/ tests/
node --check partial/static/app.js
git diff --check
uv build
```

Repository layout:

```text
partial/
  __init__.py       package version
  __main__.py       python -m partial entry point
  accounts.py       users, workspaces, roles, invites, tokens
  adapters.py       agent hook/importer normalization
  ai.py             optional OpenAI-compatible provider
  attribution.py    line-attribution core
  auth.py           first-run bootstrap token
  brain_contract.py frozen memory/search contract
  cli.py            command-line interface
  demo.py           synthetic read-only demo store
  git.py            repo discovery, hooks, checkpoint metadata sync
  handoff.py        Markdown session handoff
  memory.py         memory index/search/graph/decisions/dispatch
  mcp.py            read-only MCP stdio server
  models.py         event model and scoped IDs
  native.py         native state registration/resume
  privacy.py        redaction and sensitive-path checks
  provenance.py     pre/post tool evidence capture
  server.py         HTTP API and static dashboard server
  store.py          SQLite persistence and bundles
  workflows.py      ask/review/investigate/dispatch orchestration
  static/           landing page and dashboard assets
tests/              unit and browser suites
AGENTS.md           contributor/architecture notes
```

## Scope and limitations

- No Git hosting is implemented or intended.
- Live capture depends on what each agent exposes. ChatGPT has import-only
  support.
- Attribution is evidence-based and may be incomplete; unknown lines are
  preserved as unknown.
- Token totals depend on agents reporting usage.
- Semantic search and AI-generated answers are optional and disabled unless
  explicitly configured and requested.
- Native resume is machine- and agent-specific. Portable bundles preserve
  context, but they do not guarantee restoration into another vendor's
  private session store.
- The server targets a trusted team behind appropriate TLS/proxy controls;
  it is not presented as a hardened public SaaS boundary.
- Demo data is synthetic and read-only.

## License

MIT. See [LICENSE](LICENSE).
