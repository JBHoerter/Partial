from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from . import brain_contract as bc
from .memory import Memory
from .models import now_iso, sha256_hex
from .privacy import redact
from .store import RUN_KINDS, Store

WORKFLOW_KINDS = RUN_KINDS
AGENT_NAMES = ("codex", "claude", "devin")
_AGENT_TIMEOUT = 120
_MAX_AGENT_OUT = 8 * 1024 * 1024
# Minimal agent environment: generic session vars plus only the
# auth variables the selected agent actually uses. PARTIAL_* and
# every other variable (including unrelated cloud/secret env) are
# never forwarded to a native agent process.
_AGENT_ENV_BASE = ("PATH", "HOME", "LANG", "TERM", "TMPDIR",
                   "XDG_CONFIG_HOME", "XDG_DATA_HOME")
_AGENT_AUTH_ENV = {
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    "devin": (),
}


def _run_id(kind: str, query: str) -> str:
    return sha256_hex("run/v1\0" + kind + "\0" + query + "\0"
                      + now_iso())


def _agent_env(agent: str) -> dict:
    keys = _AGENT_ENV_BASE + _AGENT_AUTH_ENV.get(agent, ())
    env = {k: os.environ[k] for k in keys if k in os.environ}
    return {k: v for k, v in env.items()
            if not k.startswith("PARTIAL_")}


def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def _communicate(agent: str, proc, stdin_data: bytes | None = None
                 ) -> bytes:
    """Write the prompt, read bounded stdout, enforce a deadline.

    Output is capped while it streams (a runaway agent cannot exhaust
    memory), and on timeout the whole process group is killed.
    """
    deadline = time.monotonic() + _AGENT_TIMEOUT

    def _remaining() -> float:
        return deadline - time.monotonic()

    if stdin_data is not None and proc.stdin is not None:
        fd = proc.stdin.fileno()
        try:
            os.set_blocking(fd, False)
        except OSError:
            pass
        view = memoryview(stdin_data)
        while view:
            remaining = _remaining()
            if remaining <= 0:
                _kill_group(proc)
                raise TimeoutError(f"{agent} timed out")
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                select.select([], [fd], [], min(remaining, 0.5))
                continue
            except OSError:
                break  # child closed stdin early
            if written <= 0:
                select.select([], [fd], [], min(remaining, 0.5))
                continue
            view = view[written:]
        try:
            proc.stdin.close()
        except OSError:
            pass
    fd = proc.stdout.fileno()
    try:
        os.set_blocking(fd, False)
    except OSError:
        pass
    chunks: list[bytes] = []
    total = 0
    eof = False
    try:
        while not eof:
            remaining = _remaining()
            if remaining <= 0:
                _kill_group(proc)
                raise TimeoutError(f"{agent} timed out")
            ready, _, _ = select.select([fd], [], [],
                                        min(remaining, 0.5))
            if ready:
                try:
                    data = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if not data:
                    eof = True
                    continue
                chunks.append(data)
                total += len(data)
                if total > _MAX_AGENT_OUT:
                    _kill_group(proc)
                    raise ValueError(
                        f"{agent} output exceeds 8 MiB")
            elif proc.poll() is not None:
                # Child exited; drain whatever the pipe still holds.
                while True:
                    try:
                        data = os.read(fd, 65536)
                    except (BlockingIOError, OSError):
                        break
                    if not data:
                        break
                    chunks.append(data)
                    total += len(data)
                    if total > _MAX_AGENT_OUT:
                        _kill_group(proc)
                        raise ValueError(
                            f"{agent} output exceeds 8 MiB")
                eof = True
    finally:
        try:
            proc.stdout.close()
        except (OSError, AttributeError):
            pass
    try:
        proc.wait(timeout=max(0.1, _remaining()))
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise TimeoutError(f"{agent} timed out")
    if proc.returncode != 0:
        raise ValueError(f"{agent} exited with {proc.returncode}")
    return b"".join(chunks)


def _spawn_agent(agent: str, prompt: str, cwd: str) -> str:
    if agent == "codex":
        argv = ["codex", "exec", "--sandbox", "read-only",
                "--json", "-"]
        proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_agent_env(agent), start_new_session=True)
        out = _communicate(agent, proc, prompt.encode())
        texts = []
        for line in out.decode("utf-8", "replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and \
                    isinstance(item.get("text"), str):
                texts.append(item["text"])
        if not texts:
            raise ValueError(f"{agent} produced no agent_message")
        return "\n".join(texts)
    if agent == "claude":
        argv = ["claude", "-p", "--output-format", "json",
                "--tools", ""]
        proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_agent_env(agent), start_new_session=True)
        out = _communicate(agent, proc, prompt.encode())
        try:
            obj = json.loads(out.decode("utf-8", "replace"))
            text = obj.get("result")
        except (ValueError, AttributeError):
            text = None
        if not isinstance(text, str) or not text:
            raise ValueError(f"{agent} produced no result")
        return text
    if agent == "devin":
        with tempfile.NamedTemporaryFile(
                "w", suffix=".md", dir=cwd, delete=False) as f:
            f.write(prompt)
            pf = f.name
        try:
            argv = ["devin", "--print", "--prompt-file", pf]
            proc = subprocess.Popen(
                argv, cwd=cwd, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=_agent_env(agent),
                start_new_session=True)
            out = _communicate(agent, proc)
        finally:
            try:
                os.unlink(pf)
            except OSError:
                pass
        text = out.decode("utf-8", "replace").strip()
        if not text:
            raise ValueError(f"{agent} produced no output")
        return text
    raise ValueError(f"unknown agent: {agent}")


def _agent_review(agent: str, system: str, user: dict) -> dict:
    prompt = system + "\n" + json.dumps(user)
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "init", "-q", td], timeout=30,
                       capture_output=True)
        text = _spawn_agent(agent, prompt, td)
    try:
        return json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            return json.loads(text[start:end + 1])
        raise ValueError(f"{agent} returned non-JSON output")


def _docs_by_ids(mem: Memory, ids: list[str],
                 repo_id) -> list[dict]:
    docs = []
    conn = mem.store._connect()
    try:
        for did in ids:
            row = conn.execute(
                "SELECT * FROM memory_documents WHERE id=?",
                (did,)).fetchone()
            if row is None:
                raise ValueError(f"evidence document missing: {did}")
            if repo_id and row["repo_id"] != repo_id:
                raise ValueError("evidence document outside repo scope")
            docs.append(dict(row))
    finally:
        conn.close()
    return docs


def _evidence_for(mem: Memory, kind: str, repo_id, query,
                  evidence_ids, since, until, branch):
    warnings = []
    docs = []
    if evidence_ids:
        docs += _docs_by_ids(mem, list(evidence_ids), repo_id)
    if kind == "dispatch":
        disp = mem.dispatch(repo_id=repo_id, branch=branch,
                            since=since, until=until)
        conn = mem.store._connect()
        try:
            seen = {d["id"] for d in docs}
            for sid in disp["source_ids"][:bc.MAX_CONTEXT_DOCUMENTS]:
                row = conn.execute(
                    "SELECT * FROM memory_documents WHERE kind="
                    "'checkpoint' AND source_id=? AND archived=0",
                    (sid,)).fetchone()
                if row and row["id"] not in seen:
                    seen.add(row["id"])
                    docs.append(dict(row))
        finally:
            conn.close()
        return docs, [disp["markdown"]]
    q = query or "recent changes"
    try:
        docs += mem.search(q, repo_id=repo_id, limit=12)
    except ValueError as exc:
        warnings.append(f"search unavailable: {exc}")
    seen = set()
    unique = []
    for d in docs:
        if d["id"] not in seen:
            seen.add(d["id"])
            unique.append(d)
    return unique, warnings


def _safe_error(exc: Exception) -> str:
    return str(redact(str(exc)))[:1000]


def run_workflow(store: Store, kind: str, *, repo_id=None,
                 query: str = "", run: bool = False, agents=None,
                 base=None, provider=None, run_id=None,
                 allow_external: bool = False, evidence_ids=None,
                 since=None, until=None, branch=None) -> dict:
    if kind not in WORKFLOW_KINDS:
        raise ValueError(f"unknown workflow kind: {kind!r}")
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("workflow query must be a string <=500 chars")
    if type(run) is not bool:
        raise ValueError("run must be a boolean")
    agents = list(agents or [])
    bad = [a for a in agents if a not in AGENT_NAMES]
    if bad:
        raise ValueError(f"unknown agents: {', '.join(bad)}")
    if len(agents) > 3:
        raise ValueError("at most 3 agents may be selected")
    if evidence_ids is not None:
        if not isinstance(evidence_ids, (list, tuple)) or len(
                evidence_ids) > bc.MAX_CONTEXT_DOCUMENTS:
            raise ValueError(
                "evidence_ids must be a list of at most "
                f"{bc.MAX_CONTEXT_DOCUMENTS} document ids")
        if any(not isinstance(e, str) or not e or len(e) > 128
               for e in evidence_ids):
            raise ValueError("invalid evidence document id")
    for name, val in (("since", since), ("until", until),
                      ("branch", branch)):
        if val is not None and (
                not isinstance(val, str) or len(val) > 200):
            raise ValueError(f"{name} must be a string <=200 chars")
    mem = Memory(store)
    docs, warnings = _evidence_for(
        mem, kind, repo_id, query, evidence_ids, since, until, branch)
    packet = bc.evidence_packet(docs)
    source_ids = [d["id"] for d in packet]
    rid = run_id or _run_id(kind, query)
    user = {"question": query or kind, "evidence": packet,
            "previous_reports": []}

    def _save(status, report, details):
        store.save_run(
            rid, kind, repo_id, status, source_ids,
            redact(report), redact(details))

    if not run:
        report = {"planned": True, "question": user["question"],
                  "warnings": warnings}
        _save("planned", report,
              {"evidence": packet, "agents": agents})
        return {"id": rid, "kind": kind, "status": "planned",
                "report": report, "source_ids": source_ids,
                "evidence": packet}

    if not allow_external and store.memory_setting(
            "external_ai_enabled") != "true":
        _save("error", {"error": "external AI disabled"}, {})
        raise ValueError(
            "external AI disabled; an owner must enable it via"
            " POST /api/memory/settings after configuring"
            " PARTIAL_OPENAI_API_KEY")

    if not packet:
        report = {"error": "insufficient evidence in scope"}
        _save("error", report, {"agents": agents})
        return {"id": rid, "kind": kind, "status": "error",
                "report": report, "source_ids": [],
                "evidence": []}

    def _complete(system, u, allow_agent=False):
        if provider.configured:
            return provider.complete(system, u)
        if allow_agent and agents:
            return _agent_review(agents[0], system, u)
        raise ValueError(
            "no AI provider configured and no native agent selected")

    status = "completed"
    report: dict = {}
    details = {"evidence": packet, "agents": agents}
    try:
        if provider is None:
            from .ai import OpenAIProvider
            provider = OpenAIProvider()
        if kind == "ask":
            report = bc.validate_answer(
                _complete(bc.ANSWER_SYSTEM, user), packet)
        elif kind == "investigate":
            reports = []
            if agents:
                with ThreadPoolExecutor(max_workers=3) as ex:
                    futs = {ex.submit(
                        _agent_review, a, bc.INVESTIGATE_SYSTEM,
                        user): a for a in agents}
                    errors = []
                    for f, a in futs.items():
                        try:
                            r = f.result()
                            bc.validate_answer(r, packet)
                            reports.append({"agent": a, "report": r})
                        except Exception as exc:
                            errors.append({"agent": a,
                                           "error": _safe_error(exc)})
                details["reviewer_errors"] = errors
                details["investigation_reports"] = reports
                if not reports:
                    raise ValueError(
                        "all investigators failed; no result")
                if errors:
                    status = "partial"
            else:
                for _ in range(2):
                    u = dict(user)
                    u["previous_reports"] = reports
                    reports.append(bc.validate_answer(
                        _complete(bc.INVESTIGATE_SYSTEM, u), packet))
                details["investigation_reports"] = reports
            u = dict(user)
            u["previous_reports"] = reports
            report = bc.validate_answer(
                _complete(bc.ANSWER_SYSTEM, u, allow_agent=True),
                packet)
        elif kind == "dispatch":
            report = bc.validate_answer(
                _complete(bc.DISPATCH_SYSTEM, user), packet)
        elif kind == "review":
            reports = []
            errors = []
            if agents:
                with ThreadPoolExecutor(max_workers=3) as ex:
                    futs = {ex.submit(
                        _agent_review, a, bc.REVIEW_SYSTEM, user): a
                        for a in agents}
                    for f, a in futs.items():
                        try:
                            r = f.result()
                            bc.validate_answer(r, packet, review=True)
                            reports.append({"agent": a, "report": r})
                        except Exception as exc:
                            errors.append(
                                {"agent": a,
                                 "error": _safe_error(exc)})
            else:
                for _ in range(2):
                    try:
                        r = provider.complete(
                            bc.REVIEW_SYSTEM, user)
                        bc.validate_answer(r, packet, review=True)
                        reports.append({"agent": "openai",
                                        "report": r})
                    except Exception as exc:
                        errors.append({"agent": "openai",
                                       "error": _safe_error(exc)})
            details["reviewer_errors"] = errors
            details["reviewer_reports"] = reports
            if not reports:
                raise ValueError(
                    "all reviewers failed; no verdict produced")
            if errors:
                status = "partial"
            u = dict(user)
            u["previous_reports"] = [
                {"agent": r["agent"], "findings":
                    r["report"].get("findings"),
                 "summary": r["report"].get("summary")}
                for r in reports]
            report = bc.validate_answer(
                _complete(bc.JUDGE_SYSTEM, u, allow_agent=True),
                packet, review=True)
            if errors:
                report = dict(report)
                report["uncertainties"] = list(
                    report.get("uncertainties") or []) + [
                    f"reviewer {e['agent']} failed: {e['error']}"
                    for e in errors]
    except Exception as exc:
        status = "error"
        report = {"error": _safe_error(exc)}
    _save(status, report, details)
    return {"id": rid, "kind": kind, "status": status,
            "report": report, "source_ids": source_ids,
            "evidence": packet}
