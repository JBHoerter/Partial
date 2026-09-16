# Partial

Local-first AI session tracker with git commit checkpoints. Independently
implemented, inspired by the MIT-licensed Entire project
(https://github.com/entireio/cli); no upstream code or assets are used.
Zero runtime dependencies; Python >= 3.11; stdlib only
(sqlite3, argparse, subprocess, http reserved for later milestones).

## Architecture

- `partial/models.py` — `Event` dataclass, agent/kind constants, scoped
  session ids (`sha256(repo, agent, native_id)`), deterministic import ids.
- `partial/privacy.py` — best-effort recursive redaction of sensitive keys
  and common secret formats; sensitive-path detection for diffs.
- `partial/store.py` — SQLite store (WAL, busy_timeout=5000, state dir
  0700, files 0600). Tables: repositories, sessions, events, checkpoints,
  checkpoint_links, pending_paths. Bundle export/import (schema v1) and
  per-checkpoint `checkpoint_bundle()` transport bundles.
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
```

Global flags: `--home` = partial state DIRECTORY (env PARTIAL_HOME; db
lives at `<home>/partial.db`; default `$XDG_DATA_HOME/partial` or
`~/.local/share/partial`), `--repo` (env DEVIN_PROJECT_DIR), `--version`.

`partial run *` requires `partial enable` to have been run in the repo;
children execute with cwd = repo root and PARTIAL_HOME set so nested
`partial hook` invocations reach the same store.

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

## Known scope / gaps (milestone 1)

- CLI + core only; HTTP server and web UI come later.
- Auto-linking of sessions to commits is conservative: only file paths
  observed via successful mutating edit/write/patch tool inputs (and
  Codex `changes`) are tracked; pre-existing dirty files and
  shell-driven edits are not claimed. Link method is recorded as
  `observed-worktree-overlap`.
- Sync divergence between local and remote metadata branches is
  reported as an explicit error; bundle contents are still imported
  (union) but refs are left untouched. Tree-level merge with two
  parents is future work.
- Repo identity is derived from the normalized remote when one exists,
  else the resolved common git dir; registering a repo before its first
  remote exists yields the gitdir-based id.
- Redaction is best-effort, not a guarantee.
