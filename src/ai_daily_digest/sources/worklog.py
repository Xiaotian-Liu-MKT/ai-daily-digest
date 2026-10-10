"""Read-only view of ai-worklog records. Never writes; the writer owns that tree."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path


@dataclass
class WorklogEntry:
    entry_id: str
    title: str
    tool: str
    session_id: str
    project: str
    project_path: str
    scope_key: str
    status: str
    task_summary: str
    change_summary: str
    open_todos: list[str] = field(default_factory=list)

    def note_link(self, day: date) -> str:
        # Notes are filed by the month in the entry id, which an upsert never changes.
        stamp = self.entry_id[:6] if self.entry_id[:6].isdigit() else f"{day:%Y%m}"
        path = f"AI 工作留痕/entries/{stamp[:4]}/{stamp[:4]}-{stamp[4:]}/{self.scope_key}/{self.entry_id}"
        return f"[[{path}|{self.title.replace('|', '/').replace(']', ')')}]]"


def collect(vault_dir: Path, day: date) -> list[WorklogEntry]:
    records = vault_dir / ".ai-worklog" / "records"
    entries = []
    # A record stays in the month it was first written, while upserts move its date forward.
    months = {f"{day - timedelta(days=31):%Y-%m}", f"{day:%Y-%m}", f"{day + timedelta(days=3):%Y-%m}"}
    for month in sorted(months):
        for path in sorted((records / month).glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            entry = data.get("entry") or {}
            if entry.get("date") != day.isoformat():
                continue
            producer = data.get("producer") or {}
            entries.append(
                WorklogEntry(
                    entry_id=entry.get("entry_id", path.stem),
                    title=entry.get("title", path.stem),
                    tool=producer.get("tool_name", ""),
                    session_id=producer.get("session_id", ""),
                    project=entry.get("project_name") or entry.get("scope_key", ""),
                    project_path=entry.get("project_path") or "",
                    scope_key=entry.get("scope_key", "_general"),
                    status=entry.get("status", ""),
                    task_summary=entry.get("task_summary", ""),
                    change_summary=entry.get("change_summary", ""),
                    open_todos=list(entry.get("todo_open_texts") or []),
                )
            )
    return entries
