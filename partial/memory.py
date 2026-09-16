from __future__ import annotations

import ast
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import brain_contract as bc
from .models import (
    canonical_json,
    normalize_timestamp,
    now_iso,
    sha256_hex,
)
from .privacy import is_sensitive_path, redact
from .store import Store, _like_esc, repo_id_for

DOC_KINDS = ("code", "session", "checkpoint", "decision")
SESSION_DOC_KINDS = ("prompt", "response", "tool", "compaction",
                     "system", "error")
MAX_CODE_FILES = 2000
MAX_FILE_BYTES = 1024 * 1024
MAX_DOC_TEXT = 100000
IGNORE_DIRS = frozenset(
    {".git", ".devin", ".claude", ".codex", "node_modules", "vendor"})

_DOC_OUT = ("id", "kind", "title", "text", "path", "line_start",
            "line_end", "repo_id", "source_id", "commit_sha",
            "updated_at")

_LANG_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".go": "go",
    ".rs": "rust", ".java": "java",
}
_INVENTORY_RE = {
    "javascript": re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+([A-Za-z_$][\w$]*)"
        r"|class\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
        r"\s*=\s*(?:async\s*)?\()"),
    "typescript": re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+([A-Za-z_$][\w$]*)"
        r"|class\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
        r"\s*=\s*(?:async\s*)?\()"),
    "go": re.compile(
        r"^\s*(?:func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"
        r"|type\s+([A-Za-z_]\w*)\s+(?:struct|interface))"),
    "rust": re.compile(
        r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn\s+([A-Za-z_]\w*)"
        r"|struct\s+([A-Za-z_]\w*)|enum\s+([A-Za-z_]\w*)"
        r"|trait\s+([A-Za-z_]\w*)|impl\s+([A-Za-z_]\w*))"),
    "java": re.compile(
        r"^\s*(?:public|private|protected|static|final|abstract|\s)*"
        r"(?:class|interface|enum)\s+([A-Za-z_]\w*)"),
}


def document_id(repo_id: str, kind: str, source_id: str, path: str | None,
                commit_sha: str | None, line_start, text: str) -> str:
    return sha256_hex(
        "memory/v1\0" + repo_id + "\0" + kind + "\0" + source_id + "\0"
        + (path or "") + "\0" + (commit_sha or "") + "\0"
        + str(line_start or 0) + "\0" + text)


def _doc_out(row) -> dict:
    return {k: row[k] for k in _DOC_OUT}


def _symbol_id(repo_id: str, path: str, qname: str) -> str:
    return sha256_hex(
        "graph/v1\0" + repo_id + "\0" + path + "\0" + qname)


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", root, *args], capture_output=True, timeout=60)


def _head_sha(root: str) -> str | None:
    proc = _git(root, "rev-parse", "--verify", "--end-of-options",
                "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.decode().strip()


def _chunk(lines: list[str]):
    step = bc.CHUNK_LINES - bc.CHUNK_OVERLAP
    i = 0
    while i < len(lines):
        yield i + 1, lines[i:i + bc.CHUNK_LINES]
        i += step


def _code_path_ok(path: str) -> bool:
    parts = path.split("/")
    return not any(p in IGNORE_DIRS for p in parts) \
        and not is_sensitive_path(path)


def _inventory(path: str, language: str, text: str) -> list[dict]:
    rx = _INVENTORY_RE.get(language)
    if rx is None:
        return []
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        m = rx.match(line)
        if not m:
            continue
        name = next((g for g in m.groups() if g), None)
        if name:
            out.append({"name": name, "qualified_name": name,
                        "kind": "definition", "line": no,
                        "end_line": no})
    return out


def _binding_names(node) -> set:
    """Every name bound inside *node*: assignment/loop/with/except/
    comprehension targets, walrus bindings, imports, nested def and
    class declarations, and lambda parameters."""
    names = set()

    def targets(t):
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Starred) and isinstance(
                    n.value, ast.Name):
                names.add(n.value.id)

    def add_args(a):
        for x in (list(a.posonlyargs) + list(a.args)
                  + list(a.kwonlyargs)):
            names.add(x.arg)
        for x in (a.vararg, a.kwarg):
            if x is not None:
                names.add(x.arg)

    for sub in ast.walk(node):
        if sub is not node and isinstance(
                sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                      ast.ClassDef)):
            names.add(sub.name)
        elif isinstance(sub, ast.Assign):
            for t in sub.targets:
                targets(t)
        elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)):
            targets(sub.target)
        elif isinstance(sub, ast.NamedExpr):
            targets(sub.target)
        elif isinstance(sub, (ast.For, ast.AsyncFor)):
            targets(sub.target)
        elif isinstance(sub, ast.comprehension):
            targets(sub.target)
        elif isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                if item.optional_vars is not None:
                    targets(item.optional_vars)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            names.add(sub.name)
        elif isinstance(sub, ast.Lambda):
            add_args(sub.args)
        elif isinstance(sub, ast.Import):
            for al in sub.names:
                names.add((al.asname or al.name).split(".")[0])
        elif isinstance(sub, ast.ImportFrom):
            for al in sub.names:
                if al.name != "*":
                    names.add(al.asname or al.name)
    return names


def _bound_names(fn) -> set:
    """Parameters plus every name bound inside *fn*. Works for
    FunctionDef/AsyncFunctionDef (args + body) and ClassDef (class-body
    bindings) so calls shadowed by local or class-local declarations
    are never resolved to module-level functions."""
    names = _binding_names(fn)
    args = getattr(fn, "args", None)
    if args is not None:
        for a in (list(args.posonlyargs) + list(args.args)
                  + list(args.kwonlyargs)):
            names.add(a.arg)
        for a in (args.vararg, args.kwarg):
            if a is not None:
                names.add(a.arg)
    return names


def _python_symbols(path: str, text: str):
    symbols = []
    calls = []
    imports = []
    diagnostics = []
    mod = ".".join(Path(path).with_suffix("").parts)
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        diagnostics.append(f"{path}: parse failed: {exc}")
        return ([{"name": mod, "qualified_name": mod, "kind": "module",
                  "line": 1, "end_line": len(text.splitlines()) or 1}],
                [], [], diagnostics, False)
    lines_total = len(text.splitlines()) or 1
    symbols.append({"name": mod, "qualified_name": mod, "kind": "module",
                    "line": 1, "end_line": lines_total})

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                qn = f"{prefix}.{child.name}" if prefix else child.name
                symbols.append({
                    "name": child.name,
                    "qualified_name": qn,
                    "kind": "function" if not isinstance(
                        child, ast.ClassDef) else "class",
                    "line": child.lineno,
                    "end_line": getattr(child, "end_lineno",
                                        child.lineno)})
                visit(child, qn)
            else:
                visit(child, prefix)
    visit(tree, "")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    mod_fns = {}
    mod_shadowed = set()
    for child in tree.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mod_fns.setdefault(child.name, []).append(child.name)
        elif isinstance(child, ast.ClassDef):
            mod_shadowed.add(child.name)
        else:
            # Any other top-level statement (assignments, for/with/try,
            # conditionals, imports under `if`, ...) may bind a module
            # name that shadows a module-level function.
            mod_shadowed |= _binding_names(child)

    def scan_calls(node, owner, bound):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                qn = next(
                    (s["qualified_name"] for s in symbols
                     if s["name"] == child.name
                     and s["line"] == child.lineno), child.name)
                scan_calls(child, qn, bound | _bound_names(child))
                continue
            if isinstance(child, ast.Call) \
                    and isinstance(child.func, ast.Name):
                name = child.func.id
                targets = mod_fns.get(name)
                if name in bound:
                    diagnostics.append(
                        f"{path}:{child.lineno}: shadowed call {name}")
                elif name in mod_shadowed and targets:
                    diagnostics.append(
                        f"{path}:{child.lineno}: ambiguous call {name}")
                elif targets is None:
                    diagnostics.append(
                        f"{path}:{child.lineno}: unresolved call {name}")
                elif len(targets) == 1:
                    calls.append((owner, targets[0], child.lineno))
                else:
                    diagnostics.append(
                        f"{path}:{child.lineno}: ambiguous call {name}")
            scan_calls(child, owner, bound)
    scan_calls(tree, mod, set())
    return symbols, calls, imports, diagnostics, True


def _module_for(path: str) -> str:
    return ".".join(Path(path).with_suffix("").parts)


class Memory:
    def __init__(self, store: Store):
        self.store = store

    def _require_fts(self) -> None:
        if not getattr(self.store, "fts_ok", False):
            raise ValueError(
                "memory search requires SQLite FTS5; this build lacks"
                " it (memory documents remain indexed without search)")

    # ---- indexing -------------------------------------------------
    @staticmethod
    def _decision_doc(repo_id: str, d) -> dict:
        title = str(redact(d["title"]))
        body = str(redact(d["body"]))
        text = (f"{title}\n{body}\nstatus: {d['status']}"
                f"\nsources: {d['source_ids']}")
        return {
            "repo_id": repo_id, "kind": "decision",
            "source_id": d["id"], "title": title,
            "text": text[:MAX_DOC_TEXT], "path": None,
            "line_start": None, "line_end": None, "commit_sha": None,
        }

    def _history_docs(self, conn, repo_id: str) -> list[dict]:
        docs = []
        sessions = conn.execute(
            "SELECT * FROM sessions WHERE repo_id=?", (repo_id,)
        ).fetchall()
        for s in sessions:
            title = str(redact(
                s["title"] or s["native_id"] or s["id"][:12]))[:1000]
            model = str(redact(s["model"] or "-"))[:200]
            for e in self.store._session_events(conn, s["id"]):
                if e["kind"] not in SESSION_DOC_KINDS:
                    continue
                meta = f"agent:{s['agent']} model:{model}"
                body = (f"[{e['kind']}] {meta} session:{s['id']}\n"
                        + str(redact(e["text"] or "")))
                if e["data"]:
                    body += "\n" + canonical_json(redact(
                        self.store.strip_paths(
                            e["data"], repo_id)))[:4096]
                docs.append({
                    "repo_id": repo_id, "kind": "session",
                    "source_id": f"{s['id']}:{e['id']}",
                    "title": title, "text": body[:MAX_DOC_TEXT],
                    "path": None, "line_start": None, "line_end": None,
                    "commit_sha": None,
                })
        cps = conn.execute(
            "SELECT * FROM checkpoints WHERE repo_id=?", (repo_id,)
        ).fetchall()
        for c in cps:
            msg = str(redact(c["message"] or ""))
            first = msg.splitlines()
            title = (first[0] if first else c["commit_sha"][:12])
            title = title[:1000]
            text = msg + "\n"
            if c["diff"]:
                text += str(redact(c["diff"]))[:60000]
            for link in conn.execute(
                    "SELECT s.id sid, s.title stitle FROM"
                    " checkpoint_links l JOIN sessions s"
                    " ON s.id=l.session_id WHERE l.checkpoint_id=?",
                    (c["id"],)).fetchall():
                text += (f"\nlinked session {link['sid']}:"
                         f" {redact(link['stitle'] or '')}")
            docs.append({
                "repo_id": repo_id, "kind": "checkpoint",
                "source_id": c["id"], "title": title,
                "text": text[:MAX_DOC_TEXT], "path": None,
                "line_start": None, "line_end": None,
                "commit_sha": c["commit_sha"],
            })
        for d in conn.execute(
                "SELECT * FROM decisions WHERE repo_id=?",
                (repo_id,)).fetchall():
            docs.append(self._decision_doc(repo_id, d))
        return docs

    def _code_docs(self, repo: dict, repo_id: str):
        root = repo.get("root")
        docs, symbols, edges, skipped, diagnostics = [], [], [], [], []
        if not root or not Path(root).is_dir():
            return docs, symbols, edges, skipped, diagnostics, None
        sha = _head_sha(root)
        if sha is None:
            diagnostics.append(f"{root}: no resolvable HEAD")
            return docs, symbols, edges, skipped, diagnostics, None
        proc = _git(root, "ls-tree", "-rz", sha)
        if proc.returncode != 0:
            raise ValueError("git ls-tree failed for indexed commit")
        entries = []
        for raw in proc.stdout.split(b"\0"):
            if not raw:
                continue
            head, _, path_b = raw.partition(b"\t")
            parts = head.split()
            if len(parts) != 3:
                continue
            mode, _type, blob = parts
            try:
                path = path_b.decode("utf-8", "surrogateescape")
            except UnicodeDecodeError:
                continue
            entries.append((mode.decode(), blob.decode(), path))
        seen_files = 0
        for mode, blob, path in entries:
            if not _code_path_ok(path):
                skipped.append({"path": path, "reason": "excluded-path"})
                continue
            if not path or len(path) > 1000 \
                    or path != path.strip() \
                    or ".." in path.split("/"):
                skipped.append({"path": path, "reason": "invalid-path"})
                continue
            if mode not in ("100644", "100755"):
                skipped.append({"path": path, "reason": "not-regular"})
                continue
            if seen_files >= MAX_CODE_FILES:
                skipped.append({"path": path, "reason": "file-budget"})
                continue
            seen_files += 1
            size_s = _git(root, "cat-file", "-s", blob)
            if size_s.returncode != 0 or not size_s.stdout.strip().isdigit():
                skipped.append({"path": path, "reason": "unreadable"})
                continue
            if int(size_s.stdout.strip()) > MAX_FILE_BYTES:
                skipped.append({"path": path, "reason": "oversize"})
                continue
            proc = _git(root, "cat-file", "blob", blob)
            if proc.returncode != 0:
                skipped.append({"path": path, "reason": "unreadable"})
                continue
            data = proc.stdout
            if b"\0" in data:
                skipped.append({"path": path, "reason": "binary"})
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append({"path": path, "reason": "binary"})
                continue
            text = str(redact(text))
            lang = _LANG_EXT.get(Path(path).suffix.lower())
            oversize = False
            for line_start, chunk in _chunk(text.splitlines()):
                chunk_text = "\n".join(chunk)
                if len(chunk_text) > MAX_DOC_TEXT:
                    oversize = True
                    continue
                docs.append({
                    "repo_id": repo_id, "kind": "code",
                    "source_id": blob,
                    "title": str(redact(path))[:1000],
                    "text": chunk_text,
                    "path": path, "line_start": line_start,
                    "line_end": line_start + len(chunk) - 1,
                    "commit_sha": sha,
                })
            if oversize:
                skipped.append({"path": path,
                                "reason": "oversize-chunk"})
            if lang == "python":
                syms, calls, imports, diag, parsed = \
                    _python_symbols(path, text)
                diagnostics += diag
                for s in syms:
                    qn = str(redact(s["qualified_name"]))[:500]
                    symbols.append({
                        "id": _symbol_id(repo_id, path, qn),
                        "repo_id": repo_id, "path": path,
                        "name": str(redact(s["name"]))[:200],
                        "qualified_name": qn,
                        "kind": s["kind"], "line": s["line"],
                        "end_line": s["end_line"], "language": lang,
                        "analysis": "ast" if parsed else "lexical",
                        "commit_sha": sha,
                    })
                mod_sym = _symbol_id(
                    repo_id, path, str(redact(_module_for(path))))
                for caller_qn, callee_qn, lineno in calls[:2000]:
                    edges.append({
                        "repo_id": repo_id,
                        "source_id": _symbol_id(
                            repo_id, path, str(redact(caller_qn))),
                        "target_id": _symbol_id(
                            repo_id, path, str(redact(callee_qn))),
                        "kind": "calls",
                    })
                for imp in imports[:500]:
                    edges.append({
                        "repo_id": repo_id,
                        "source_id": mod_sym,
                        "target_id": f"import:{str(redact(imp))}",
                        "kind": "imports",
                    })
            elif lang:
                for s in _inventory(path, lang, text)[:1000]:
                    qn = str(redact(s["qualified_name"]))[:500]
                    symbols.append({
                        "id": _symbol_id(repo_id, path, qn),
                        "repo_id": repo_id, "path": path,
                        "name": str(redact(s["name"]))[:200],
                        "qualified_name": qn,
                        "kind": s["kind"], "line": s["line"],
                        "end_line": s["end_line"], "language": lang,
                        "analysis": "lexical", "commit_sha": sha,
                    })
        return docs, symbols, edges, skipped, diagnostics, sha

    def _resolve_import_edges(self, repo_id: str, files_by_mod: dict,
                            edges: list) -> list:
        out = []
        for e in edges:
            t = e["target_id"]
            if t.startswith("import:"):
                mod = t[7:]
                target = files_by_mod.get(mod)
                if target is None:
                    continue
                out.append({
                    "repo_id": e["repo_id"], "source_id": e["source_id"],
                    "target_id": target, "kind": "imports"})
            else:
                out.append(e)
        return out

    def index(self, repo: dict | None = None, *,
              semantic: bool = False) -> dict:
        if repo is not None and not repo.get("id"):
            repo = {**repo, "id": repo_id_for(
                repo["root"], repo.get("common_dir") or "",
                repo.get("remote") or "")}
        repos = [repo] if repo is not None else [
            {"id": r["id"], "root": None} for r in
            self.store.list_repos()]
        result = {"indexed_repositories": [], "skipped": [],
                  "diagnostics": [], "semantic": False}
        for r in repos:
            repo_id = r["id"]
            code_docs, symbols, edges, skipped, diagnostics, sha = (
                [], [], [], [], [], None)
            do_code = bool(r.get("root"))
            if do_code:
                code_docs, symbols, edges, skipped, diagnostics, sha = \
                    self._code_docs(r, repo_id)
            files_by_mod = {}
            for s in symbols:
                if s["kind"] == "module":
                    files_by_mod[s["qualified_name"]] = s["id"]
            edges = self._resolve_import_edges(
                repo_id, files_by_mod, edges)
            conn = self.store._connect()
            try:
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    history = self._history_docs(conn, repo_id)
                    all_docs = history + code_docs
                    fts_ok = getattr(self.store, "fts_ok", True)
                    for d in all_docs:
                        d["id"] = document_id(
                            repo_id, d["kind"], d["source_id"],
                            d["path"], d["commit_sha"],
                            d["line_start"], d["text"])
                    # Derived docs are rebuilt below; standalone
                    # evidence docs (investigate seeds, review
                    # snapshots) are never derived and must survive.
                    keep_user_docs = (
                        " AND source_id NOT LIKE 'seed:%'"
                        " AND source_id NOT LIKE 'review:%'")
                    if do_code:
                        conn.execute(
                            "UPDATE memory_documents SET archived=1"
                            " WHERE repo_id=? AND archived=0"
                            + keep_user_docs,
                            (repo_id,))
                        conn.execute(
                            "DELETE FROM graph_symbols WHERE repo_id=?",
                            (repo_id,))
                        conn.execute(
                            "DELETE FROM graph_edges WHERE repo_id=?",
                            (repo_id,))
                    else:
                        conn.execute(
                            "UPDATE memory_documents SET archived=1"
                            " WHERE repo_id=? AND archived=0 AND kind"
                            " IN ('session','checkpoint','decision')"
                            + keep_user_docs,
                            (repo_id,))
                    for d in all_docs:
                        prior = conn.execute(
                            "SELECT embedding,embedding_model FROM"
                            " memory_documents WHERE id=?",
                            (d["id"],)).fetchone()
                        conn.execute(
                            "INSERT OR REPLACE INTO memory_documents("
                            "id,repo_id,kind,source_id,title,text,path,"
                            "line_start,line_end,commit_sha,updated_at,"
                            "embedding,embedding_model,archived)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                            (d["id"], repo_id, d["kind"],
                             d["source_id"], d["title"], d["text"],
                             d["path"], d["line_start"],
                             d["line_end"], d["commit_sha"], now_iso(),
                             prior["embedding"] if prior else None,
                             prior["embedding_model"]
                             if prior else None))
                        if fts_ok:
                            conn.execute(
                                "DELETE FROM memory_fts WHERE id=?",
                                (d["id"],))
                            conn.execute(
                                "INSERT INTO memory_fts(id,repo_id,"
                                "kind,title,text) VALUES(?,?,?,?,?)",
                                (d["id"], repo_id, d["kind"],
                                 d["title"], d["text"]))
                    for s in symbols:
                        conn.execute(
                            "INSERT OR REPLACE INTO graph_symbols(id,"
                            "repo_id,path,name,qualified_name,kind,"
                            "line,end_line,language,analysis,"
                            "commit_sha) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (s["id"], repo_id, s["path"], s["name"],
                             s["qualified_name"], s["kind"], s["line"],
                             s["end_line"], s["language"],
                             s["analysis"], s["commit_sha"]))
                    for e in edges:
                        conn.execute(
                            "INSERT OR IGNORE INTO graph_edges("
                            "repo_id,source_id,target_id,kind)"
                            " VALUES(?,?,?,?)",
                            (repo_id, e["source_id"], e["target_id"],
                             e["kind"]))
                    if do_code and sha:
                        conn.execute(
                            "INSERT OR REPLACE INTO"
                            " repository_indexes(repo_id,commit_sha,"
                            "indexed_at) VALUES(?,?,?)",
                            (repo_id, sha, now_iso()))
                    conn.commit()
            finally:
                conn.close()
            result["indexed_repositories"].append({
                "id": repo_id, "commit_sha": sha or "",
                "documents": len(history) + len(code_docs)})
            result["skipped"] += skipped
            result["diagnostics"] += diagnostics[:200]
        if semantic:
            from .ai import OpenAIProvider
            if OpenAIProvider().configured:
                self._embed_missing(
                    repo_ids=[r["id"] for r in repos],
                    kinds=None if repo is not None else
                    ("session", "checkpoint", "decision"))
                result["semantic"] = True
        return result

    def _embed_missing(self, repo_ids=None, kinds=None) -> None:
        from .ai import OpenAIProvider
        provider = OpenAIProvider()
        if not provider.configured:
            raise ValueError(
                "semantic indexing requires a configured provider;"
                " set PARTIAL_OPENAI_API_KEY")
        conn = self.store._connect()
        try:
            sql = ("SELECT id,text FROM memory_documents WHERE"
                   " embedding IS NULL AND archived=0")
            params: list = []
            if repo_ids is not None:
                sql += (" AND repo_id IN ("
                        + ",".join("?" for _ in repo_ids) + ")")
                params += list(repo_ids)
            if kinds is not None:
                sql += (" AND kind IN ("
                        + ",".join("?" for _ in kinds) + ")")
                params += list(kinds)
            rows = conn.execute(sql, params).fetchall()
            todo = [(r["id"], r["text"][:8000]) for r in rows]
        finally:
            conn.close()
        for i in range(0, len(todo), 32):
            batch = todo[i:i + 32]
            vectors = provider.embed([t for _, t in batch])
            conn = self.store._connect()
            try:
                with conn:
                    for (did, _), vec in zip(batch, vectors):
                        conn.execute(
                            "UPDATE memory_documents SET embedding=?,"
                            " embedding_model=? WHERE id=?",
                            (json.dumps(vec), bc.EMBEDDING_MODEL, did))
                    conn.commit()
            finally:
                conn.close()

    # ---- search ---------------------------------------------------
    def search(self, query: str, *, repo_id=None, kind=None,
               semantic: bool = False, limit: int = 12) -> list[dict]:
        self._require_fts()
        match = bc.fts_query(query)
        sql = bc.LEXICAL_SQL
        params: list = [match]
        if repo_id:
            sql += " AND d.repo_id=?"
            params.append(repo_id)
        if kind:
            if kind not in DOC_KINDS:
                raise ValueError(f"invalid document kind: {kind!r}")
            sql += " AND d.kind=?"
            params.append(kind)
        sql += bc.LEXICAL_ORDER
        params.append(100 if semantic else max(1, min(limit, 100)))
        conn = self.store._connect()
        try:
            lex = [_doc_out(dict(r)) for r in
                   conn.execute(sql, params).fetchall()]
            sem = []
            if semantic:
                sem = self._semantic_rows(
                    conn, query, repo_id, kind)
        finally:
            conn.close()
        if semantic:
            return bc.fuse(lex, sem, limit=max(1, min(limit, 100)))
        return lex

    def _semantic_rows(self, conn, query, repo_id, kind) -> list[dict]:
        from .ai import OpenAIProvider
        provider = OpenAIProvider()
        if not provider.configured:
            raise ValueError(
                "semantic search requires a configured provider; set"
                " PARTIAL_OPENAI_API_KEY")
        qv = provider.embed([query])[0]
        sql = ("SELECT * FROM memory_documents WHERE embedding"
               " IS NOT NULL AND archived=0 AND embedding_model=?")
        params: list = [bc.EMBEDDING_MODEL]
        if repo_id:
            sql += " AND repo_id=?"
            params.append(repo_id)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            raise ValueError(
                "no embedded documents in scope; run"
                " 'partial index --semantic' first")
        scored = []
        skipped = 0
        for r in rows:
            try:
                vec = json.loads(r["embedding"])
                score = bc.cosine(qv, vec)
            except (ValueError, TypeError):
                skipped += 1
                continue
            scored.append((score, _doc_out(dict(r))))
        if not scored:
            raise ValueError(
                f"no compatible embeddings ({skipped} skipped for"
                " dimension/model mismatch)")
        scored.sort(key=lambda x: (-x[0], x[1]["id"]))
        return [d for _, d in scored[:100]]

    def document(self, document_id: str) -> dict | None:
        if not isinstance(document_id, str) \
                or len(document_id) > 128:
            raise ValueError("invalid document id")
        conn = self.store._connect()
        try:
            row = conn.execute(
                "SELECT * FROM memory_documents WHERE id=?",
                (document_id,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            out = _doc_out(d)
            out["has_embedding"] = d.get("embedding") is not None
            return out
        finally:
            conn.close()

    def context(self, query: str, *, repo_id=None,
                semantic: bool = False) -> dict:
        docs = self.search(query, repo_id=repo_id, semantic=semantic)
        conn = self.store._connect()
        try:
            idx = [dict(r) for r in conn.execute(
                "SELECT repo_id,commit_sha,indexed_at"
                " FROM repository_indexes" +
                (" WHERE repo_id=?" if repo_id else ""),
                (repo_id,) if repo_id else ()).fetchall()]
        finally:
            conn.close()
        limitations = [
            "index reflects recorded history and the indexed commit;"
            " the working tree may differ",
            "lexical ranking only; enable semantic search explicitly"
            " for embeddings",
        ]
        if semantic:
            limitations = [
                "hybrid lexical+semantic ranking; embeddings may be"
                " stale for recently indexed content",
            ]
        return {
            "query": query,
            "documents": bc.evidence_packet(docs),
            "mode": "hybrid" if semantic else "lexical",
            "indexed_repositories": idx,
            "limitations": limitations,
        }

    # ---- decisions -------------------------------------------------
    def add_decision(self, repo_id: str, title: str, body: str,
                     source_ids: list[str], *, author: str,
                     supersedes=None) -> dict:
        if not isinstance(title, str) or not title.strip() \
                or len(title) > 500:
            raise ValueError("decision title must be 1..500 chars")
        if not isinstance(body, str) or not body.strip() \
                or len(body) > 50000:
            raise ValueError("decision body must be 1..50000 chars")
        if not isinstance(author, str) or not author.strip() \
                or len(author) > 200:
            raise ValueError("decision author must be 1..200 chars")
        if source_ids is not None and (
                not isinstance(source_ids, (list, tuple))
                or len(source_ids) > 200):
            raise ValueError(
                "decision source_ids must be a list of at most 200"
                " document ids")
        title = str(redact(title.strip()))
        body = str(redact(body.strip()))
        author = str(redact(author.strip()))
        fts_ok = getattr(self.store, "fts_ok", True)
        conn = self.store._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute(
                        "SELECT 1 FROM repositories WHERE id=?",
                        (repo_id,)).fetchone() is None:
                    raise ValueError("unknown repo")
                for sid in source_ids or []:
                    if not isinstance(sid, str) or len(sid) > 128:
                        raise ValueError("invalid source id")
                    row = conn.execute(
                        "SELECT repo_id FROM memory_documents"
                        " WHERE id=?", (sid,)).fetchone()
                    if row is None or row["repo_id"] != repo_id:
                        raise ValueError(
                            "decision source id not in repository")
                did = sha256_hex(
                    "decision/v1\0" + repo_id + "\0" + title
                    + "\0" + body + "\0" + now_iso())
                if supersedes:
                    old = conn.execute(
                        "SELECT * FROM decisions WHERE id=?",
                        (supersedes,)).fetchone()
                    if old is None or old["repo_id"] != repo_id:
                        raise ValueError(
                            "superseded decision not in repository")
                    conn.execute(
                        "UPDATE decisions SET status='superseded'"
                        " WHERE id=?", (supersedes,))
                    conn.execute(
                        "UPDATE memory_documents SET archived=1"
                        " WHERE kind='decision' AND source_id=?"
                        " AND archived=0", (supersedes,))
                    old = dict(old)
                    old["status"] = "superseded"
                    odoc = self._decision_doc(repo_id, old)
                    odoc_id = document_id(
                        repo_id, "decision", supersedes, None, None,
                        None, odoc["text"])
                    conn.execute(
                        "INSERT OR REPLACE INTO memory_documents(id,"
                        "repo_id,kind,source_id,title,text,path,"
                        "line_start,line_end,commit_sha,updated_at,"
                        "archived) VALUES(?,?,?,?,?,?,NULL,NULL,NULL,"
                        "NULL,?,0)",
                        (odoc_id, repo_id, "decision", supersedes,
                         odoc["title"], odoc["text"], now_iso()))
                    if fts_ok:
                        conn.execute(
                            "DELETE FROM memory_fts WHERE id=?",
                            (odoc_id,))
                        conn.execute(
                            "INSERT INTO memory_fts(id,repo_id,kind,"
                            "title,text) VALUES(?,?,?,?,?)",
                            (odoc_id, repo_id, "decision",
                             odoc["title"], odoc["text"]))
                conn.execute(
                    "INSERT INTO decisions(id,repo_id,title,body,"
                    "status,source_ids,author,created_at,supersedes)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (did, repo_id, title, body, "active",
                     canonical_json(list(source_ids or [])),
                     author, now_iso(), supersedes))
                row = {"id": did, "title": title, "body": body,
                       "status": "active",
                       "source_ids": canonical_json(
                           list(source_ids or []))}
                doc = self._decision_doc(repo_id, row)
                doc_id = document_id(
                    repo_id, "decision", did, None, None, None,
                    doc["text"])
                conn.execute(
                    "INSERT INTO memory_documents(id,repo_id,kind,"
                    "source_id,title,text,path,line_start,line_end,"
                    "commit_sha,updated_at,archived)"
                    " VALUES(?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,0)",
                    (doc_id, repo_id, "decision", did, doc["title"],
                     doc["text"], now_iso()))
                if fts_ok:
                    conn.execute(
                        "INSERT INTO memory_fts(id,repo_id,kind,title,"
                        "text) VALUES(?,?,?,?,?)",
                        (doc_id, repo_id, "decision", doc["title"],
                         doc["text"]))
                conn.commit()
            return next(
                d for d in self.decisions(repo_id) if d["id"] == did)
        finally:
            conn.close()

    def decisions(self, repo_id=None) -> list[dict]:
        conn = self.store._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM decisions" +
                (" WHERE repo_id=?" if repo_id else "") +
                " ORDER BY created_at,id",
                (repo_id,) if repo_id else ()).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["source_ids"] = json.loads(d["source_ids"])
                out.append(d)
            return out
        finally:
            conn.close()

    # ---- dispatch --------------------------------------------------
    def dispatch(self, *, repo_id=None, branch=None, since=None,
                 until=None) -> dict:
        def _ts(v):
            if isinstance(v, str) and re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}", v.strip()):
                v = v.strip() + "T00:00:00Z"
            return datetime.fromisoformat(normalize_timestamp(v))
        now = datetime.now(timezone.utc)
        until_dt = now if until is None else _ts(until)
        since_dt = until_dt - timedelta(days=7) \
            if since is None else _ts(since)
        if since_dt >= until_dt:
            raise ValueError("since must be before until")
        # Stored created_at values are ISO8601 but may lack
        # microseconds or carry a non-UTC offset, so exact lexical
        # bounds would misorder them ("+00:00" sorts before
        # ".ffffff+00:00"). Pre-filter on the YYYY-MM-DD prefix widened
        # by a day (covers any UTC offset), then compare exactly on
        # normalized aware-UTC datetimes.
        lo = (since_dt - timedelta(days=1)).date().isoformat()
        hi = (until_dt + timedelta(days=1)).date().isoformat()
        sql = ("SELECT * FROM checkpoints WHERE"
               " substr(created_at,1,10)>=?"
               " AND substr(created_at,1,10)<=?")
        params: list = [lo, hi]
        if repo_id:
            sql += " AND repo_id=?"
            params.append(repo_id)
        if branch:
            sql += " AND branch=?"
            params.append(branch)
        sql += " ORDER BY created_at,id LIMIT 2001"
        conn = self.store._connect()
        try:
            fetched = conn.execute(sql, params).fetchall()
            truncated = len(fetched) > 2000
            kept = []
            for r in fetched[:2000]:
                try:
                    t = datetime.fromisoformat(
                        normalize_timestamp(r["created_at"]))
                except (ValueError, TypeError):
                    continue
                if since_dt <= t < until_dt:
                    kept.append((t, r))
            kept.sort(key=lambda tr: (tr[0], tr[1]["id"]))
            truncated = truncated or len(kept) > 2000
            rows = [r for _, r in kept[:2000]]
            titles = {}
            if rows:
                marks = ",".join("?" for _ in rows)
                for r in conn.execute(
                        f"SELECT l.checkpoint_id cid, s.title t,"
                        f" s.id sid FROM checkpoint_links l"
                        f" JOIN sessions s ON s.id=l.session_id"
                        f" WHERE l.checkpoint_id IN ({marks})",
                        [r["id"] for r in rows]).fetchall():
                    titles.setdefault(r["cid"], []).append(
                        str(redact(r["t"] or r["sid"][:12])))
        finally:
            conn.close()
        lines = [
            f"# Dispatch {since_dt.date()}..{until_dt.date()}"]
        source_ids = []
        for r in rows:
            msg_lines = (r["message"] or "").splitlines()
            msg = str(redact(msg_lines[0])).strip() \
                if msg_lines else ""
            if not msg:
                msg = "(no message)"
            row_branch = str(redact(r["branch"] or "detached"))
            linked = "; linked: " + ", ".join(
                titles.get(r["id"], [])) if titles.get(r["id"]) else ""
            lines.append(
                f"- {r['commit_sha'][:10]} {msg}"
                f" [{row_branch}] cp:{r['id'][:12]}"
                f"{linked}")
            source_ids.append(r["id"])
        if truncated:
            lines.append("- (truncated at 2000 checkpoints)")
        return {
            "markdown": "\n".join(lines),
            "source_ids": source_ids,
            "scope": {
                "repo_id": repo_id, "branch": branch,
                "since": since_dt.isoformat(),
                "until": until_dt.isoformat()},
            "truncated": truncated,
        }

    # ---- graph -----------------------------------------------------
    def graph_search(self, query: str, *, repo_id=None) -> list[dict]:
        if not isinstance(query, str) or not query.strip() \
                or len(query) > 200:
            raise ValueError("invalid graph query")
        sql = ("SELECT * FROM graph_symbols WHERE"
               " (name LIKE ? ESCAPE '\\'"
               " OR qualified_name LIKE ? ESCAPE '\\')")
        like = f"%{_like_esc(query.strip())}%"
        params: list = [like, like]
        if repo_id:
            sql += " AND repo_id=?"
            params.append(repo_id)
        sql += " ORDER BY repo_id,path,line LIMIT 200"
        conn = self.store._connect()
        try:
            return [dict(r) for r in
                    conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _graph_node(self, conn, symbol_id: str, repo_id=None):
        sql = "SELECT * FROM graph_symbols WHERE id=?"
        params = [symbol_id]
        if repo_id:
            sql += " AND repo_id=?"
            params.append(repo_id)
        return conn.execute(sql, params).fetchone()

    def graph_neighbors(self, symbol_id: str, *,
                        repo_id=None) -> dict:
        if not isinstance(symbol_id, str) or len(symbol_id) > 200:
            raise ValueError("invalid symbol id")
        conn = self.store._connect()
        try:
            node = self._graph_node(conn, symbol_id, repo_id)
            if node is None:
                raise ValueError("unknown symbol id")
            node = dict(node)
            rid = node["repo_id"]
            out_edges = conn.execute(
                "SELECT * FROM graph_edges WHERE repo_id=?"
                " AND source_id=?", (rid, symbol_id)).fetchall()
            in_edges = conn.execute(
                "SELECT * FROM graph_edges WHERE repo_id=?"
                " AND target_id=?", (rid, symbol_id)).fetchall()
            nodes = {symbol_id: node}
            for e in [*out_edges, *in_edges]:
                for sid in (e["source_id"], e["target_id"]):
                    if sid in nodes or sid.startswith("import:"):
                        continue
                    r = self._graph_node(conn, sid, rid)
                    if r is not None:
                        nodes[sid] = dict(r)
            return {
                "symbol": node,
                "nodes": list(nodes.values()),
                "edges": [dict(e) for e in [*out_edges, *in_edges]],
                "analysis": node["analysis"],
                "limitations": [
                    "static analysis only; dynamic dispatch and"
                    " reflection are not resolved",
                ] + ([] if node["analysis"] == "ast" else [
                    "lexical inventory only for this language;"
                    " no call/import relationships recorded"]),
            }
        finally:
            conn.close()

    def graph_impact(self, symbol_id: str, *, repo_id=None) -> dict:
        if not isinstance(symbol_id, str) or len(symbol_id) > 200:
            raise ValueError("invalid symbol id")
        conn = self.store._connect()
        try:
            node = self._graph_node(conn, symbol_id, repo_id)
            if node is None:
                raise ValueError("unknown symbol id")
            node = dict(node)
            rid = node["repo_id"]
            seen = {symbol_id}
            frontier = [symbol_id]
            truncated = False
            for _depth in range(3):
                nxt = []
                for sid in frontier:
                    rows = conn.execute(
                        "SELECT source_id FROM graph_edges"
                        " WHERE repo_id=? AND target_id=?",
                        (rid, sid)).fetchall()
                    for r in rows:
                        if r["source_id"] not in seen:
                            if len(seen) >= 200:
                                truncated = True
                                break
                            seen.add(r["source_id"])
                            nxt.append(r["source_id"])
                    if truncated:
                        break
                if truncated:
                    break
                frontier = nxt
            nodes = []
            for sid in sorted(seen):
                if sid.startswith("import:"):
                    continue
                r = self._graph_node(conn, sid, rid)
                if r is not None:
                    nodes.append(dict(r))
            return {
                "symbol": node, "impacted": nodes,
                "truncated": truncated,
                "limitations": [
                    "incoming edges only, depth 3, 200 nodes max"],
            }
        finally:
            conn.close()

    def graph_capabilities(self) -> dict:
        return {
            "languages": {
                "python": "ast definitions, same-file calls, import"
                          " links",
                "javascript": "lexical inventory only",
                "typescript": "lexical inventory only",
                "go": "lexical inventory only",
                "rust": "lexical inventory only",
                "java": "lexical inventory only",
            },
            "analysis": ["ast", "lexical"],
            "limitations": [
                "dynamic dispatch, imports through aliases, and"
                " reflection are not resolved",
            ],
        }

    # ---- experts ---------------------------------------------------
    def experts(self, scope: str, *, repo_id: str) -> list[dict]:
        if not isinstance(scope, str) or not scope.strip() \
                or len(scope) > 500:
            raise ValueError("invalid experts scope")
        conn = self.store._connect()
        try:
            rows = conn.execute(bc.EXPERTS_SQL, (repo_id,)).fetchall()
            candidates: set[str] = set()
            if "/" in scope or "." in scope:
                prefix = scope.rstrip("/") + "/"
                for r in rows:
                    files = json.loads(r["files"])
                    if any(f == scope or f.startswith(prefix)
                           for f in files):
                        candidates.add(r["checkpoint_id"])
            else:
                for d in self.search(scope, repo_id=repo_id,
                                     limit=50):
                    if d.get("path"):
                        p = d["path"]
                        for r in rows:
                            if p in json.loads(r["files"]):
                                candidates.add(r["checkpoint_id"])
            by_session: dict[str, dict] = {}
            for r in rows:
                if r["checkpoint_id"] not in candidates:
                    continue
                e = by_session.setdefault(r["session_id"], {
                    "session_id": r["session_id"],
                    "title": str(redact(r["title"])) if r["title"]
                    else None,
                    "agent": r["agent"], "checkpoint_ids": set()})
                e["checkpoint_ids"].add(r["checkpoint_id"])
            out = []
            for sid, e in sorted(
                    by_session.items(),
                    key=lambda kv: (-len(kv[1]["checkpoint_ids"]),
                                    kv[0])):
                out.append({
                    "session_id": sid, "title": e["title"],
                    "agent": e["agent"],
                    "checkpoint_ids": sorted(e["checkpoint_ids"]),
                    "contributions": len(e["checkpoint_ids"]),
                    "method": "linked-checkpoint-count"})
            return out
        finally:
            conn.close()
