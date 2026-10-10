"""Claude Code: ~/.claude/projects/<project>/<sessionId>.jsonl (top-level sessions only)."""
from __future__ import annotations

import re
from pathlib import Path

from ..model import Session, Window, build_turns, loads, parse_ts, read_jsonl

TOOL = "Claude Code"
_FIRST_TS = re.compile(r'"timestamp":"(\d{4}-\d{2}-\d{2}T[^"]+)"')
_COMMAND = re.compile(
    r"<command-name>(.*?)</command-name>(?:.*?<command-args>(.*?)</command-args>)?", re.S
)
_NOISE_PREFIXES = (
    "<local-command-",
    "<bash-",
    "<task-notification>",
    "<system-reminder>",
    "[Request interrupted",
    "Caveat:",
)
_PASTE_TAG = re.compile(r"</?pasted_content[^>]*>\s*")
_RECAP_SUFFIX = re.compile(r"\s*\(disable recaps in /config\)\s*$")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _is_human(row: dict) -> bool:
    if row.get("isMeta") or row.get("isCompactSummary") or row.get("isSidechain"):
        return False
    origin = row.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == "human"
    # Older CLI versions carry no origin; tool results are the only other user rows.
    return "toolUseResult" not in row and isinstance(row.get("message", {}).get("content"), str)


def clean_user_text(text: str) -> str:
    text = text.strip()
    m = _COMMAND.search(text)
    if m:
        args = (m.group(2) or "").strip()
        # Bare slash commands (/usage, /clear, ...) say nothing about the work done.
        return f"{m.group(1).strip()} {args}" if args else ""
    if not text or text.startswith(_NOISE_PREFIXES):
        return ""
    return _PASTE_TAG.sub("", text).strip()


def _count_subagents(session_dir: Path, window: Window) -> int:
    sub = session_dir / "subagents"
    if not sub.is_dir():
        return 0
    start, end = window.start.timestamp(), window.end.timestamp()
    return sum(1 for p in sub.rglob("agent-*.jsonl") if start <= p.stat().st_mtime < end)


def _read_session(path: Path, window: Window, seen_uuids: set[str]) -> Session | None:
    session = Session(TOOL, path.stem)
    events = []
    first_ts_seen = False
    stamps = tuple(f'"timestamp":"{p}' for p in window.utc_prefixes)
    for line in read_jsonl(path):
        if not first_ts_seen:
            m = _FIRST_TS.search(line)
            if m:
                first_ts_seen = True
                ts = parse_ts(m.group(1))
                session.continued = bool(ts and ts < window.start)
        if '"type":"ai-title"' in line[:200]:
            row = loads(line)
            if row and row.get("aiTitle"):
                session.title = row["aiTitle"]
            continue
        if not any(s in line for s in stamps):
            continue
        row = loads(line)
        if not isinstance(row, dict):
            continue
        ts = parse_ts(row.get("timestamp"))
        if ts is None or ts not in window or row.get("isSidechain"):
            continue
        uuid = row.get("uuid")
        if uuid:
            if uuid in seen_uuids:
                continue
            seen_uuids.add(uuid)
        kind = row.get("type")
        if kind == "user" and _is_human(row):
            # Headless runs (claude -p, including this tool's own) are not the user's work.
            if row.get("entrypoint") == "sdk-cli":
                return None
            text = clean_user_text(_text_of(row.get("message", {}).get("content")))
            if text:
                events.append((ts, "user", text))
                session.cwd = row.get("cwd") or session.cwd
        elif kind == "assistant":
            text = _text_of(row.get("message", {}).get("content")).strip()
            if text:
                events.append((ts, "assistant", text))
                session.cwd = session.cwd or row.get("cwd", "")
        elif kind == "system" and row.get("subtype") == "away_summary":
            note = _RECAP_SUFFIX.sub("", str(row.get("content", "")).strip())
            if note:
                session.notes.append(note)
        session.last_ts = ts
    session.turns = build_turns(events)
    if not session.human_turns:
        return None
    session.subagents = _count_subagents(path.with_suffix(""), window)
    return session


def collect(claude_dir: Path, window: Window) -> list[Session]:
    projects = claude_dir / "projects"
    if not projects.is_dir():
        return []
    sessions, seen = [], set()
    for path in sorted(projects.glob("*/*.jsonl")):
        if path.stat().st_mtime < window.start_epoch:
            continue
        session = _read_session(path, window, seen)
        if session:
            sessions.append(session)
    return sessions
