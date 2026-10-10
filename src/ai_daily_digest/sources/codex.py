"""Codex CLI / desktop: ~/.codex/sessions/**/rollout-*.jsonl plus archived_sessions."""
from __future__ import annotations

import re
from pathlib import Path

from ..model import Session, Window, build_turns, loads, parse_ts, read_jsonl

TOOL = "Codex"
_BROWSER_CONTEXT = re.compile(
    r"<in-app-browser-context[^>]*>.*?</in-app-browser-context>\s*(?:## My request:\s*)?", re.S
)
_QUESTION_REPLY = re.compile(
    r"<send_user_message_question_reply>(.*?)</send_user_message_question_reply>", re.S
)


def clean_user_text(text: str) -> str:
    text = _BROWSER_CONTEXT.sub("", text).strip()
    if text.startswith("<heartbeat>"):
        return ""
    m = _QUESTION_REPLY.search(text)
    if m:
        return "[回答提问卡片] " + m.group(1).strip()
    return text


def _rollouts(codex_dir: Path, window: Window):
    for root in (codex_dir / "sessions", codex_dir / "archived_sessions"):
        if not root.is_dir():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            # A resumed thread appends to its original file, so the dated folder says nothing.
            if path.stat().st_mtime >= window.start_epoch:
                yield path


def _meta(path: Path) -> dict | None:
    for line in read_jsonl(path):
        row = loads(line)
        if isinstance(row, dict) and row.get("type") == "session_meta":
            payload = row.get("payload") or {}
            payload["_ts"] = parse_ts(row.get("timestamp") or payload.get("timestamp"))
            return payload
        return None
    return None


def _parent_of(meta: dict) -> str | None:
    if meta.get("parent_thread_id"):
        return meta["parent_thread_id"]
    source = meta.get("source")
    if isinstance(source, dict):
        return ((source.get("subagent") or {}).get("thread_spawn") or {}).get("parent_thread_id")
    return None


def _titles(codex_dir: Path) -> dict[str, str]:
    titles = {}
    for line in read_jsonl(codex_dir / "session_index.jsonl"):
        row = loads(line)
        if isinstance(row, dict) and row.get("id") and row.get("thread_name"):
            titles[row["id"]] = row["thread_name"]
    return titles


def _read_events(path: Path, window: Window, seen: set, session: Session, events: list) -> None:
    for line in read_jsonl(path):
        head = line[:60]
        if not any(p in head for p in window.utc_prefixes):
            continue
        if '"UserMessage"' not in line and '"task_complete"' not in line:
            continue
        row = loads(line)
        if not isinstance(row, dict) or row.get("type") != "event_msg":
            continue
        ts = parse_ts(row.get("timestamp"))
        if ts is None or ts not in window:
            continue
        # A continuation file replays the tail of its predecessor.
        key = (session.session_id, row.get("ordinal"))
        if row.get("ordinal") is not None:
            if key in seen:
                continue
            seen.add(key)
        payload = row.get("payload") or {}
        if payload.get("type") == "item_completed":
            item = payload.get("item") or {}
            if item.get("type") != "UserMessage":
                continue
            text = "\n".join(
                c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict)
            )
            text = clean_user_text(text)
            if text:
                events.append((ts, "user", text))
        elif payload.get("type") == "task_complete":
            text = (payload.get("last_agent_message") or "").strip()
            if text:
                events.append((ts, "assistant", text))
        session.last_ts = max(session.last_ts or ts, ts)


def collect(codex_dir: Path, window: Window) -> list[Session]:
    titles = _titles(codex_dir)
    sessions: dict[str, Session] = {}
    events: dict[str, list] = {}
    children: dict[str, int] = {}
    parents: dict[str, str] = {}
    seen: set = set()
    for path in sorted(_rollouts(codex_dir, window)):
        meta = _meta(path)
        if not meta:
            continue
        created = meta.get("_ts")
        if meta.get("thread_source") == "subagent" or isinstance(meta.get("source"), dict):
            parent = _parent_of(meta)
            child = meta.get("id")  # session_id is the root thread for subagents
            if parent and child:
                parents[child] = parent
            if parent and created and created < window.end:
                children[parent] = children.get(parent, 0) + 1
            continue
        # codex exec runs are scripted batch calls, not conversations.
        if meta.get("originator") == "codex_exec" or meta.get("source") == "exec":
            continue
        sid = meta.get("session_id") or meta.get("id") or path.stem
        session = sessions.setdefault(
            sid, Session(TOOL, sid, title=titles.get(sid, ""), cwd=meta.get("cwd", ""))
        )
        if created and created < window.start:
            session.continued = True
        _read_events(path, window, seen, session, events.setdefault(sid, []))
    # A worklog written by a subagent may carry that subagent's thread id.
    for child in parents:
        root, hops = child, 0
        while root in parents and hops < 10:
            root, hops = parents[root], hops + 1
        if root in sessions:
            sessions[root].aliases.add(child)
    result = []
    for sid, session in sessions.items():
        session.turns = build_turns(events[sid])
        if not session.human_turns:
            continue
        session.subagents = children.get(sid, 0)
        result.append(session)
    return result
