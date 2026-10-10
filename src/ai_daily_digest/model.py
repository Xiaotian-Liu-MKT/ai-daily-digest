from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterator


@dataclass
class Window:
    """One local calendar day, as a half-open UTC interval."""

    day: date
    start: datetime
    end: datetime

    @classmethod
    def for_day(cls, day: date) -> "Window":
        # Naive datetimes are interpreted in the system time zone by astimezone().
        start = datetime.combine(day, time()).astimezone()
        end = datetime.combine(day + timedelta(days=1), time()).astimezone()
        return cls(day, start.astimezone(timezone.utc), end.astimezone(timezone.utc))

    def __contains__(self, ts: datetime) -> bool:
        return self.start <= ts < self.end

    @property
    def utc_prefixes(self) -> tuple[str, ...]:
        """UTC date strings the window touches, for cheap substring prefilters."""
        days, d = [], self.start.date()
        last = (self.end - timedelta(microseconds=1)).date()
        while d <= last:
            days.append(d.isoformat())
            d += timedelta(days=1)
        return tuple(days)

    @property
    def start_epoch(self) -> float:
        return self.start.timestamp()


def parse_ts(value) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def read_jsonl(path: Path) -> Iterator[str]:
    """Yield raw lines; a session being written right now may end in a partial line."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            yield from fh
    except OSError:
        return


def loads(line: str):
    try:
        return json.loads(line)
    except ValueError:
        return None


@dataclass
class Turn:
    ts: datetime
    user: str
    reply: str = ""


@dataclass
class Session:
    tool: str
    session_id: str
    title: str = ""
    cwd: str = ""
    turns: list[Turn] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    subagents: int = 0
    continued: bool = False
    last_ts: datetime | None = None
    # Other ids that mean this session, e.g. the threads of its subagents.
    aliases: set[str] = field(default_factory=set)

    @property
    def first_ts(self) -> datetime | None:
        return self.turns[0].ts if self.turns else None

    @property
    def human_turns(self) -> int:
        return sum(1 for t in self.turns if t.user)


def build_turns(events: list[tuple[datetime, str, str]]) -> list[Turn]:
    """Fold ordered (ts, role, text) events into turns, keeping the last reply of each turn."""
    turns: list[Turn] = []
    for ts, role, text in sorted(events, key=lambda e: e[0]):
        if role == "user":
            turns.append(Turn(ts, text))
        elif turns:
            turns[-1].reply = text
        else:
            # Reply to a prompt typed before the window opened.
            turns.append(Turn(ts, "", text))
    return turns
