"""Antigravity CLI (agy): ~/.gemini/antigravity-cli/brain/<convId>/.system_generated/logs.

The transcript is an undocumented internal format, so every field access is tolerant.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from ..model import Session, Window, build_turns, loads, parse_ts, read_jsonl

TOOL = "agy"
_TRANSCRIPT = Path(".system_generated") / "logs" / "transcript_full.jsonl"
_USER_REQUEST = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.S)
_CHECKPOINT_CAP = 2000


def clean_user_text(content: str) -> str:
    m = _USER_REQUEST.search(content)
    if m:
        return m.group(1).strip()
    return content.split("<ADDITIONAL_METADATA>")[0].strip()


def _typed_conversations(agy_dir: Path) -> dict[str, str]:
    """Conversations the user typed into, mapped to their workspace.

    Headless `agy -p` batch calls never reach history.jsonl, which is the only
    signal separating them from interactive sessions.
    """
    typed = {}
    for line in read_jsonl(agy_dir / "history.jsonl"):
        row = loads(line)
        if isinstance(row, dict) and row.get("conversationId"):
            typed[row["conversationId"]] = row.get("workspace", "")
    return typed


def _summaries(agy_dir: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    titles, children = {}, {}
    db = agy_dir / "conversation_summaries.db"
    if not db.exists():
        return titles, children
    try:
        # immutable=1: never create -wal/-shm files next to a database agy has open.
        con = sqlite3.connect(f"file:{db.as_posix()}?immutable=1", uri=True)
        rows = con.execute(
            "select conversation_id, title, parent_conversation_id from conversation_summaries"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return titles, children
    for cid, title, parent in rows:
        if title:
            titles[cid] = title
        if parent:
            children.setdefault(parent, []).append(cid)
    return titles, children


def _active_in(path: Path, window: Window) -> bool:
    if not path.exists() or path.stat().st_mtime < window.start_epoch:
        return False
    for line in read_jsonl(path):
        row = loads(line)
        ts = parse_ts(row.get("created_at")) if isinstance(row, dict) else None
        return bool(ts and ts < window.end)
    return False


def _read_session(cid: str, path: Path, window: Window) -> Session:
    session = Session(TOOL, cid)
    events = []
    first = True
    for line in read_jsonl(path):
        row = loads(line)
        if not isinstance(row, dict):
            continue
        ts = parse_ts(row.get("created_at"))
        if ts is None:
            continue
        if first:
            first = False
            session.continued = ts < window.start
        if ts not in window:
            continue
        kind, content = row.get("type"), row.get("content") or ""
        if kind == "USER_INPUT":
            text = clean_user_text(content)
            if text:
                events.append((ts, "user", text))
        elif kind == "PLANNER_RESPONSE" and content.strip():
            events.append((ts, "assistant", content.strip()))
        elif kind == "CHECKPOINT" and content.strip():
            session.notes.append(content.strip()[:_CHECKPOINT_CAP])
        session.last_ts = ts
    session.turns = build_turns(events)
    return session


def collect(agy_dir: Path, window: Window) -> list[Session]:
    brain = agy_dir / "brain"
    if not brain.is_dir():
        return []
    typed = _typed_conversations(agy_dir)
    titles, children = _summaries(agy_dir)
    sessions = []
    for cid, workspace in typed.items():
        path = brain / cid / _TRANSCRIPT
        if not path.exists() or path.stat().st_mtime < window.start_epoch:
            continue
        session = _read_session(cid, path, window)
        if not session.human_turns:
            continue
        session.title = titles.get(cid, "")
        session.cwd = workspace
        session.subagents = sum(
            1 for child in children.get(cid, []) if _active_in(brain / child / _TRANSCRIPT, window)
        )
        sessions.append(session)
    return sessions
