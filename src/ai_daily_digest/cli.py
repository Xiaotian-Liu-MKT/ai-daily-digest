from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .config import Config, load_config
from .model import Session, Window
from .render import build_bundle, render_note
from . import dida
from .sources import agy, claude_code, codex, worklog

log = logging.getLogger("ai_daily_digest")
SYSTEM_PROMPT = (
    "You are a careful report writer. Follow the instructions in the user message exactly "
    "and output only the requested markdown."
)


def note_path(cfg: Config, day: date) -> Path:
    return cfg.vault_dir / cfg.digest_folder / f"{day:%Y}" / f"{day.isoformat()}.md"


def marker_path(cfg: Config, day: date) -> Path:
    return cfg.state_dir / "done" / f"{day.isoformat()}.json"


def collect(cfg: Config, window: Window) -> list[Session]:
    sessions: list[Session] = []
    for name, fn, root in (
        ("claude", claude_code.collect, cfg.claude_dir),
        ("codex", codex.collect, cfg.codex_dir),
        ("agy", agy.collect, cfg.agy_dir),
    ):
        try:
            found = fn(root, window)
        except Exception:
            # One tool changing its on-disk format must not cost the other two.
            log.exception("source %s failed", name)
            continue
        log.info("%s: %d sessions", name, len(found))
        sessions += found
    return sessions


def summarize(cfg: Config, bundle: str) -> str:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        cfg.claude_cmd, "-p",
        "--model", cfg.model,
        # Without this the run would show up in tomorrow's digest as a session.
        "--no-session-persistence",
        "--disable-slash-commands",
        "--tools", "",
        "--system-prompt", SYSTEM_PROMPT,
    ]
    proc = subprocess.run(
        cmd, input=bundle, capture_output=True, text=True, encoding="utf-8",
        cwd=cfg.state_dir, timeout=cfg.llm_timeout_s,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    text = proc.stdout.strip()
    if proc.returncode != 0 or "## 概览" not in text:
        raise RuntimeError(f"claude exited {proc.returncode}: {(proc.stderr or text)[:500]}")
    return text[text.index("## 概览"):]


def dida_section(cfg: Config) -> list[str]:
    """Upcoming 滴答清单 tasks, as of the moment the digest is written."""
    today = datetime.now().date()
    try:
        tasks = dida.due_tasks(cfg, today)
    except dida.NotConfigured as exc:
        log.info("dida skipped: %s", exc)
        return []
    except dida.DidaError as exc:
        log.warning("dida failed: %s", exc)
        return [f"## 滴答清单：{cfg.dida_due_days} 天内到期", "", f"> 读取失败：{exc}", ""]
    return dida.render_section(tasks, today, cfg.dida_due_days)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def run_day(cfg: Config, day: date, *, force: bool, use_llm: bool, dry_run: bool) -> str:
    target = note_path(cfg, day)
    if not force and not dry_run and (target.exists() or marker_path(cfg, day).exists()):
        return "skipped"
    window = Window.for_day(day)
    sessions = collect(cfg, window)
    entries = worklog.collect(cfg.vault_dir, day)
    if not sessions and not entries:
        if not dry_run:
            _write_atomic(marker_path(cfg, day), json.dumps({"sessions": 0}))
        return "empty"
    bundle = build_bundle(window, sessions, entries, cfg)
    if dry_run:
        out = cfg.state_dir / "bundles" / f"{day.isoformat()}.md"
        _write_atomic(out, bundle)
        turns = sum(s.human_turns for s in sessions)
        return (
            f"dry-run: {len(sessions)} sessions, {turns} prompts, {len(entries)} worklog "
            f"entries, bundle {len(bundle)} chars -> {out}"
        )
    body = summarize(cfg, bundle) if use_llm else None
    _write_atomic(
        target, render_note(window, sessions, entries, body, cfg.model, dida_section(cfg))
    )
    return f"written {target}"


def run_dida(cfg: Config, args, today: date) -> int:
    try:
        if args.dida_command == "auth":
            dida.authorize(cfg)
            print("已授权。")
        elif args.dida_command == "lists":
            print("\n".join(p["name"] for p in dida.projects(cfg)))
        elif args.dida_command == "due":
            days = args.days if args.days is not None else cfg.dida_due_days
            print("\n".join(dida.render_section(dida.due_tasks(cfg, today, days), today, days)))
        else:
            task = dida.add_task(cfg, args.title, quadrant=args.quadrant, due=args.due,
                                 due_time=args.time, project=args.project, note=args.note)
            print(f"已添加：{task.get('title')}（{dida.QUADRANT_NAMES[args.quadrant]}，id {task.get('id')}）")
    except dida.DidaError as exc:
        print(f"滴答清单：{exc}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-daily-digest")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="write digests for past days that do not have one yet")
    run.add_argument("--date", type=date.fromisoformat, help="only this day (YYYY-MM-DD)")
    run.add_argument("--days", type=int, help="how many past days to check (default from config)")
    run.add_argument("--force", action="store_true", help="overwrite an existing digest")
    run.add_argument("--no-llm", action="store_true", help="session lists only, no model call")
    run.add_argument("--dry-run", action="store_true", help="write the model input to state/ and stop")
    dd = sub.add_parser("dida", help="滴答清单: authorize, list upcoming tasks, add a task")
    dsub = dd.add_subparsers(dest="dida_command", required=True)
    dsub.add_parser("auth", help="log in once in the browser")
    due = dsub.add_parser("due", help="print open tasks due soon")
    due.add_argument("--days", type=int)
    dsub.add_parser("lists", help="print list names")
    add = dsub.add_parser("add", help="add one task")
    add.add_argument("title")
    add.add_argument("--quadrant", type=int, choices=(1, 2, 3, 4), required=True,
                     help="1 重要且紧急, 2 重要不紧急, 3 不重要但紧急, 4 不重要不紧急")
    add.add_argument("--due", type=date.fromisoformat, help="YYYY-MM-DD")
    add.add_argument("--time", type=time.fromisoformat, help="HH:MM; omit for an all-day task")
    add.add_argument("--list", dest="project", help="list name; default is the inbox")
    add.add_argument("--note", default="")
    args = parser.parse_args(argv)

    cfg = load_config()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(cfg.state_dir / "digest.log", encoding="utf-8")]
    if sys.stdout:  # pythonw has no console
        sys.stdout.reconfigure(encoding="utf-8")
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    today = datetime.now().date()
    if args.command == "dida":
        return run_dida(cfg, args, today)
    if args.date:
        days = [args.date]
    else:
        span = args.days or cfg.backfill_days
        days = [today - timedelta(days=n) for n in range(span, 0, -1)]
    failed = False
    for day in days:
        if day >= today:
            log.warning("%s is not over yet, skipping", day)
            continue
        try:
            result = run_day(cfg, day, force=args.force, use_llm=not args.no_llm, dry_run=args.dry_run)
        except Exception:
            # Leave no note and no marker so the next scheduled run retries this day.
            log.exception("%s failed", day)
            failed = True
            continue
        log.info("%s: %s", day, result)
    return 1 if failed else 0
