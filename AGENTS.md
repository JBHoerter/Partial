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
  checkpoint_links, pending_paths, reviews, attribution_files,
  attribution_pending, checkpoint_attribution, native_sessions, plus the
  frozen memory schema (`memory_documents`, `memory_fts`,
  `repository_indexes`, `graph_symbols`, `graph_edges`, `decisions`,
  `workflow_runs`) and ancillary `projects`/`project_repos`/
  `memory_settings`. Bundle export/import (schema v1) and per-checkpoint
  `checkpoint_bundle()` transport bundles; attribution reports, native
  registrations, and a top-level `memory` extension (documents, symbols,
  edges, decisions, runs, indexes) ride bundles and are revalidated on
  import in the same transaction.
- `partial/attribution.py` — frozen line-attribution core: hash
  fingerprints, position-aware snapshot diff, `report`/`aggregate`/
  `summarize`; stores hashes only, never file content.
- `partial/provenance.py` — `Provenance` integration: Pre/PostToolUse
  before/after snapshots, pending-call registry, per-file states,
  checkpoint attribution reports (local observation; imported bundles
  are marked `imported-claim` and recomputed).
- `partial/native.py` — native session registry (`register_native`,
  `resume_plan`, `restore_native`, `resume_session`). Formats:
  `claude-jsonl`, `codex-rollout`, `devin-atif`, `native-id`. Codex
  rollouts and Claude transcripts are resumable on-machine; Devin ATIF
  exports are portable context only (restore is rejected); `native-id`
  registrations carry no file.
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
- `partial/auth.py` — bootstrap token (`PARTIAL_TOKEN` env or
  `<home>/server-token`, generated 0600, never rotated silently). Used
  only for first-time `POST /api/setup` once accounts exist.
- `partial/accounts.py` — identity DB at `<home>/accounts.db`
  (users, workspaces, memberships, hashed auth sessions, invites,
  workspace-scoped API tokens, audit log). Each workspace gets its own
  `Store` database — the legacy workspace binds `<home>/partial.db`,
  new workspaces live at `<home>/workspaces/<uuid>/partial.db`; all
  queries/import/export are physically isolated per workspace. Roles:
  owner/admin/member/viewer; PBKDF2-HMAC-SHA256 (600k) passwords;
  only SHA-256 token hashes are persisted.
- `partial/handoff.py` — shared Markdown handoff formatter (recorded
  context only, no generated instructions).
- `partial/server.py` — `http.server.ThreadingHTTPServer` JSON API +
  static file whitelist. Auth: workspace-scoped API-token bearer or
  12h HttpOnly SameSite=Strict account session cookie (persisted
  token hashes, survive restarts). Requests select the workspace via
  `X-Partial-Workspace` (default: first membership; API tokens are
  pinned to their own workspace). Host/Origin allowlists, per-IP
  rate limit on login/setup/invite-accept, 16 MiB body cap, security
  headers, no CORS. Unknown or non-member workspaces return 404;
  insufficient role returns 403; API tokens cannot administer.
- `partial/brain_contract.py` — frozen repository-memory contract:
  schema, FTS5 lexical SQL, RRF fusion (`fts5-cosine-rrf-v1`, k=60),
  chunking (80 lines/10 overlap), context budgets (12 docs/48k chars),
  embedding/answer model names, system prompts, response validation,
  and `usage_totals`. Lead-owned; do not modify.
- `partial/memory.py` — `Memory` index/search/document/context over
  captured sessions, checkpoints, decisions, and committed HEAD code
  (explicit `partial index` only; secrets/symlinks/sensitive paths
  excluded). Python AST symbol/edge graph, lexical inventory for other
  languages, decisions with supersession, deterministic dispatch,
  expert lookup by linked-checkpoint count.
- `partial/ai.py` — `OpenAIProvider`: OpenAI-compatible embeddings and
  chat completions against the frozen model names; API key from
  `PARTIAL_OPENAI_API_KEY` only, base URL `PARTIAL_AI_BASE_URL`
  (https or literal loopback http), no calls unless explicitly
  requested.
- `partial/workflows.py` — ask/review/investigate/dispatch
  orchestration; `run=False` saves a planned evidence packet, `run=True`
  calls the configured provider or explicitly selected native agents;
  every run is persisted in `workflow_runs`.
- `partial/mcp.py` — stdio JSON-RPC 2.0 server exposing read-only
  memory tools (search/context/document/graph); no shell execution,
  stdout is protocol-clean.
- `partial/demo.py` — synthetic demo fixture (temp store, only used by
  `serve --demo`; read-only API). Indexes its synthetic sessions and
  checkpoints plus one clearly-labelled synthetic code document so the
  memory UI is browsable; no network or writes.
- `partial/static/` — landing page (`index.html`) and the workspace SPA
  (`app.html`, `app.js`, `styles.css`, `favicon.svg`); native ES-module
  JS, no framework/build, textContent-only rendering of untrusted text.
- `partial/cli.py` — argparse CLI (entry point `partial`).

## Commands

```
partial enable [--agent devin|claude|codex|chatgpt|all] \
    [--memory-skill|--skill-only]   # --memory-skill also writes
                                    # .devin/skills/partial-memory/;
                                    # --skill-only writes only it
partial disable
partial status [--json]
partial doctor [--json]
partial hook <devin|claude|codex|git> [event]   # reads JSON from stdin
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
partial auth token           # prints the bootstrap token (pre-setup only)
partial account create --email E --name N [--password-stdin]
partial account token --email E --workspace ID --name N \
    [--role member] [--expires-days N] [--password-stdin]
partial upload SERVER_URL [--repo-only] [--workspace ID]   # PARTIAL_SERVER_TOKEN
partial ingest-bundle FILE   # local bundle import
partial capture before|after --session SID --file REL --key KEY
partial native register --agent devin|claude|codex --session-id ID \
    [--file PATH] [--archive]
partial native list [--json]
partial native show ID
partial resume ID [--run] [--worktree NEW_PATH] [--restore-native] \
    [--trust-native-state] [--target-root DIR]
partial stop ID                 # mark session ended (does not kill)
partial attach ID --commit REF  # link a session to a commit checkpoint
partial why FILE [--line N] [--json]
partial blame FILE [--line START-END] [--json]
partial index [--semantic] [--all]       # code index needs an explicit repo
partial search QUERY [--code|--semantic] [--json]
partial context QUERY [--json]
partial ask QUERY [--run]                # --run sends bounded evidence to the provider
partial decision add --title T --body B [--source ID]... [--supersedes ID]
partial decision list [--json]
partial graph search QUERY | neighbors ID | impact ID | capabilities
partial dispatch [--since D --until D --branch B --run]   # recap is an alias
partial review [run] [--base REF] [--query Q] \
    [--agents codex,claude,devin] [--run]
partial review show RUN_ID
partial investigate run QUERY [--seed FILE] [--agents A,B,C] [--run]
partial investigate show RUN_ID
partial experts SCOPE [--json]
partial tokens [--session ID|--checkpoint ID]
partial mcp                              # stdio JSON-RPC, read-only tools
partial project create NAME | list | attach PROJECT_ID
partial plugin register NAME --command ABS --sha256 DIGEST
partial plugin list | run NAME -- ARGS
partial configure --show
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

First run: open the printed URL, which shows a setup form. The
bootstrap token from `partial auth token` is required there once to
create the owner account and bind the legacy workspace; afterwards it
grants no access — sign in with email/password, and mint
workspace-scoped API tokens under Settings. Alternatively create the
owner locally with `partial account create`. Registration beyond the
owner is invitation-only (owner/admin creates invite tokens; invitees
paste them on the sign-in page). Keep the virtual environment
activated when running agents so the installed `partial` hook command
is on PATH. For persistent installation instead, use
`uv tool install .`.

The server targets a single trusted team: workspaces isolate all
captured data per tenant (separate SQLite DBs, never shared
predicates). Bind loopback by default; for remote access put it
behind a TLS reverse proxy and pass `--public-url https://…` with an
explicit `PARTIAL_TOKEN`. Bundles contain recorded session context —
only upload/import them into workspaces you trust; there is no
telemetry.

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
the bootstrap token comes from `partial auth token` or `PARTIAL_TOKEN`
and is consumed by the first-run setup only. Non-loopback binds
require `--public-url https://…` and an explicit `PARTIAL_TOKEN`;
the stdlib server is meant to sit behind a TLS reverse proxy.
`partial upload` posts an exported bundle to `POST /api/bundles` with
a workspace API token via `PARTIAL_SERVER_TOKEN` (minted under
Settings or `partial account token`), refuses redirects, disables
environment proxy handling so the `Authorization` header is never
sent through a proxy, and requires HTTPS off-loopback;
`--workspace ID` pins the target workspace.
`partial serve --demo` uses an isolated temp store with synthetic
data; all mutation endpoints return 403 and no accounts are created.

## Devin hook format

`.devin/hooks.v1.json` is the hook map itself (not wrapped in `hooks`).
Each event maps to a list of matcher/hook groups:

```json
{"SessionStart": [{"matcher": "", "hooks": [{"type": "command",
  "command": "partial hook devin SessionStart", "timeout": 10}]}]}
```

Events: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop,
SessionEnd, PostCompaction. PreToolUse only records a pre-edit
attribution snapshot (no event is stored); PostToolUse ingests the tool
event and closes the attribution window. Hook commands receive a JSON
object on stdin with
`hook_event_name`, `session_id`, `prompt_id`, `tool_name`, `tool_input`,
`tool_response` ({success, output, error}), `prompt`, and (newer) Stop
`last_assistant_message`. `DEVIN_PROJECT_DIR` is the project root.
Claude uses the same group shape nested under a `"hooks"` key inside the
generated `.devin/partial/claude-settings.json`. Merge/remove is by exact
owned command equality only; foreign hooks are never touched.

### Devin sub-agents and attribution

Devin hook payloads and ATIF exports can identify work below the top-level
session. A hook payload with `parent_session_id` creates a child session
row scoped to the same repository and agent. A Devin ATIF import walks
`subagent_trajectories` recursively (bounded at depth 16) and materializes
each trajectory as a `devin` child session with the trajectory id in its
native id. Child sessions keep their own prompts, tool calls, model,
timestamps, and usage events, and are shown as `sub-agent` rows under the
parent session and in a checkpoint's Sessions tab.

Attribution stays evidence-based: checkpoint line reports and the diff
badges carry the scoped session id that supplied the before/after file
evidence. The dashboard resolves that id to the agent/model/session title
when the session is linked to the checkpoint. If a sub-agent only reviewed
or searched without changing files, it appears as session context rather
than as AI-authored lines.

## Source references

- Entire (MIT): https://github.com/entireio/cli pinned at
  https://github.com/entireio/cli/tree/fd26dfc8d0ac5f93744f122b33474d85b91371f8
  (inspiration only; no upstream code or assets are used).
  Sources actually consulted:
  - https://docs.entire.io/guides/search/overview.md
  - https://docs.entire.io/guides/graph/how-graph-works.md
  - https://docs.entire.io/guides/dispatches/overview.md
  - https://docs.entire.io/cli-reference/review.md
  - https://docs.entire.io/cli-reference/investigate.md
  - https://github.com/entireio/cli/tree/fd26dfc8d0ac5f93744f122b33474d85b91371f8/architecture/attribution.md
- Codex `exec --json` events: thread.started, turn.started/completed
  (usage), item.started/completed (item {id,type,text,command,
  aggregated_output,changes:[{path,kind}]}).
- Claude transcript JSONL and `.claude/settings.json` hook schema.
- ChatGPT export: list of conversations with `mapping` tree and
  `current_node`; ancestry traversal preferred, deterministic
  topological order otherwise.
- Harbor sidecar RFC: https://github.com/ente-io/harbor RFC cfc54
  (metadata-branch checkpoint transport inspiration).

## Feature coverage

Implemented: local-first session capture (Devin/Claude/Codex hooks and
Codex exec wrapper; ChatGPT/Claude/Codex/Devin-ATIF importers), git
commit checkpoints with metadata-branch sync, snapshot-based line
attribution (`blame`/`why`), native session register/resume/archive
restore, multi-tenant accounts with per-workspace SQLite isolation,
bundle export/upload/import, authenticated web UI + JSON API, read-only
demo mode, repository memory (FTS5 lexical search over sessions/
checkpoints/decisions/indexed code, optional semantic embeddings,
bounded evidence context, Python AST code graph + lexical inventory for
other languages, decisions with supersession, deterministic dispatch/
recap, expert lookup by linked-checkpoint count, frozen usage totals),
workflows (ask/review/investigate/dispatch with explicit `--run`
provider or selected native agents), MCP stdio server with read-only
memory tools, project groups, SHA-256-verified local plugins, doctor
diagnostics, and `configure --show`.

Missing or reduced features vs Entire-style hosting, stated plainly:
no hosted cloud service, no GitHub OAuth sign-in (local
email/password + workspace API tokens only), no git forging/rewriting,
no plugin marketplace or remote plugin index (local
SHA-256-pinned executables only), no automatic/scheduled code review
agents (review/investigate require explicit `--run` and a configured
provider or native agent), no semantic search without a configured
embedding provider, no per-project ACL inside a workspace (the
workspace is the trust boundary; project groups are organizational
only), and the code graph resolves Python definitions/same-file
calls/imports only — all other languages are lexical inventory with
no call/import edges.

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
- Line attribution is an estimate from file snapshots around mutating
  tool calls (`PreToolUse`/`PostToolUse` pairs) plus observed
  outside-window edits; lines without evidence are `unknown`, never
  defaulted to human. Shell-driven edits can be captured explicitly via
  `partial capture`. Reports are stored per checkpoint and travel in
  bundles as `imported-claim`; import revalidates them against the
  frozen core. `partial blame`/`partial why` join `git blame` with the
  recorded checkpoint reports — evidence lookup only, no generated
  explanations.
- `partial resume` prints a fixed argv plan (`devin --resume`,
  `claude -r`, `codex resume`); `--run` executes it in the repo (or a
  fresh `git worktree add --detach` at the checkpoint via `--worktree`),
  `--restore-native --trust-native-state` writes a sanitized provider
  file under the user home (never overwrites). Native archives are
  redacted and strip reasoning/encrypted blobs and Codex sandbox
  policy. `partial stop` only marks the session ended.
- Repo identity is derived from the normalized remote when one exists,
  else the resolved common git dir; registering a repo before its first
  remote exists yields the gitdir-based id.
- Repository memory indexes committed HEAD blobs only (never worktree
  files); code indexing is capped at 2000 files/1 MiB each and skips
  secrets, symlinks, submodules, sensitive paths, vendor/dependency
  dirs, and binaries. Stale code requires re-running `partial index`.
  Semantic search needs `PARTIAL_OPENAI_API_KEY`; query/model or
  dimension mismatches are skipped, never silently downgraded.
- The code graph resolves Python definitions plus same-file calls and
  import edges only; other languages are lexical inventories marked
  `analysis='lexical'` with no relationship claims. Dynamic dispatch,
  aliases, and cross-file call resolution are reported as limitations.
- AI workflows never run implicitly: `run=False` stores a planned
  evidence packet and nothing calls a provider or native agent
  without an explicit run request. Server-side execution
  (`POST /api/workflows` with `run: true`, plus the semantic
  index/search API paths) is gated by the workspace owner's
  `external_ai_enabled` policy and requires a configured provider;
  native agents cannot be run from the API at all. A local CLI
  `--run` is itself the explicit local opt-in — it does not consult
  the owner policy, but still requires `PARTIAL_OPENAI_API_KEY` or an
  explicit `--agents` native selection, and it still never runs
  silently: every run is persisted in `workflow_runs`.
  Failed/timed-out reviewers produce `partial` runs, never
  fabricated verdicts.
- Plugins execute only via explicit `partial plugin run` after SHA-256
  verification with a minimal env allowlist; no marketplace, download,
  or automatic execution. Plugin names cannot shadow built-ins.
- Redaction is best-effort, not a guarantee.
