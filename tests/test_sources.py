import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from ai_daily_digest.model import Window, build_turns
from ai_daily_digest.sources import agy, claude_code, codex

DAY = date(2026, 10, 9)
WINDOW = Window.for_day(DAY)
NOON = WINDOW.start + timedelta(hours=12)
BEFORE = WINDOW.start - timedelta(hours=2)
AFTER = WINDOW.end + timedelta(hours=1)


def iso(ts):
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def write_jsonl(path, rows, mtime=AFTER):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows) + "\n", encoding="utf-8")
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))


def test_window_is_one_local_day():
    assert WINDOW.end - WINDOW.start == timedelta(days=1)
    assert WINDOW.start.astimezone().date() == DAY
    assert NOON in WINDOW and WINDOW.end not in WINDOW


def test_build_turns_keeps_last_reply_and_orphan_reply():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    turns = build_turns([
        (t, "assistant", "tail of yesterday"),
        (t + timedelta(1), "user", "q"),
        (t + timedelta(2), "assistant", "draft"),
        (t + timedelta(3), "assistant", "final"),
    ])
    assert [(x.user, x.reply) for x in turns] == [("", "tail of yesterday"), ("q", "final")]


def claude_user(ts, text, uuid, **extra):
    row = {"type": "user", "uuid": uuid, "timestamp": iso(ts), "cwd": "E:\\proj", "sessionId": "s1",
           "origin": {"kind": "human"}, "entrypoint": "cli", "message": {"role": "user", "content": text}}
    row.update(extra)
    return row


def claude_reply(ts, text, uuid):
    return {"type": "assistant", "uuid": uuid, "timestamp": iso(ts), "cwd": "E:\\proj",
            "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": text}]}}


def test_claude_keeps_only_human_prompts_inside_the_window(tmp_path):
    rows = [
        claude_user(BEFORE, "yesterday", "u0"),
        {"type": "ai-title", "aiTitle": "Fix parser", "sessionId": "s1"},
        claude_user(NOON, "fix the parser", "u1"),
        claude_reply(NOON + timedelta(minutes=1), "done", "a1"),
        claude_user(NOON + timedelta(minutes=2), "summary of earlier work", "u2", isCompactSummary=True),
        claude_user(NOON + timedelta(minutes=3), "peer says hi", "u3", origin={"kind": "peer"}),
        claude_user(NOON + timedelta(minutes=4), "<command-name>/usage</command-name>", "u4"),
        claude_user(AFTER, "tomorrow", "u5"),
    ]
    write_jsonl(tmp_path / "projects" / "E--proj" / "s1.jsonl", rows)
    (session,) = claude_code.collect(tmp_path, WINDOW)
    assert session.title == "Fix parser" and session.cwd == "E:\\proj" and session.continued
    assert [(t.user, t.reply) for t in session.turns] == [("fix the parser", "done")]


def test_claude_skips_headless_and_stale_sessions(tmp_path):
    write_jsonl(tmp_path / "projects" / "p" / "headless.jsonl",
                [claude_user(NOON, "write the digest", "h1", entrypoint="sdk-cli")])
    write_jsonl(tmp_path / "projects" / "p" / "old.jsonl", [claude_user(NOON, "x", "o1")], mtime=BEFORE)
    assert claude_code.collect(tmp_path, WINDOW) == []


def codex_meta(ts, sid, **extra):
    payload = {"id": sid, "cwd": "E:\\proj", "originator": "Codex Desktop", "source": "vscode",
               "thread_source": "user"}
    payload.update(extra)
    return {"timestamp": iso(ts), "type": "session_meta", "payload": payload}


def codex_user(ts, ordinal, text):
    return {"timestamp": iso(ts), "ordinal": ordinal, "type": "event_msg", "payload": {
        "type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": text}]}}}


def codex_done(ts, ordinal, text):
    return {"timestamp": iso(ts), "ordinal": ordinal, "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": text}}


def test_codex_merges_continuation_and_ignores_subagents_and_exec(tmp_path):
    sessions = tmp_path / "sessions" / "2026" / "10" / "08"
    browser = '<in-app-browser-context source="x">\nnoise\n</in-app-browser-context>\n\n## My request:\nplot it'
    write_jsonl(sessions / "rollout-a-t1.jsonl", [
        codex_meta(BEFORE, "t1"),
        codex_user(NOON, 5, browser),
        codex_done(NOON + timedelta(minutes=1), 6, "plotted"),
    ])
    write_jsonl(sessions / "rollout-b-t1_seg.jsonl", [
        codex_meta(NOON, "t1"),
        codex_done(NOON + timedelta(minutes=1), 6, "plotted"),
        codex_user(NOON + timedelta(minutes=2), 7, "<heartbeat>tick</heartbeat>"),
    ])
    write_jsonl(sessions / "rollout-c-sub.jsonl", [
        codex_meta(NOON, "sub", thread_source="subagent",
                   source={"subagent": {"thread_spawn": {"parent_thread_id": "t1"}}}),
    ])
    write_jsonl(sessions / "rollout-d-exec.jsonl", [
        codex_meta(NOON, "e1", originator="codex_exec", source="exec"),
        codex_user(NOON, 1, "batch prompt"),
    ])
    write_jsonl(tmp_path / "session_index.jsonl", [{"id": "t1", "thread_name": "Plot FX"}])
    (session,) = codex.collect(tmp_path, WINDOW)
    assert (session.title, session.subagents, session.continued) == ("Plot FX", 1, True)
    assert [(t.user, t.reply) for t in session.turns] == [("plot it", "plotted")]


def agy_step(ts, kind, content):
    return {"type": kind, "created_at": iso(ts), "content": content}


def test_agy_reads_only_conversations_the_user_typed_into(tmp_path):
    logs = ".system_generated/logs/transcript_full.jsonl"
    write_jsonl(tmp_path / "history.jsonl", [
        {"display": "audit", "timestamp": 1, "workspace": "C:\\w", "conversationId": "c1"}])
    write_jsonl(tmp_path / "brain" / "c1" / logs, [
        agy_step(NOON, "USER_INPUT", "<USER_REQUEST>audit skills</USER_REQUEST><ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"),
        agy_step(NOON + timedelta(minutes=1), "PLANNER_RESPONSE", ""),
        agy_step(NOON + timedelta(minutes=2), "PLANNER_RESPONSE", "report ready"),
        agy_step(NOON + timedelta(minutes=3), "CHECKPOINT", "Task Overview"),
    ])
    write_jsonl(tmp_path / "brain" / "batch" / logs, [agy_step(NOON, "USER_INPUT", "You are marking ONE")])
    (session,) = agy.collect(tmp_path, WINDOW)
    assert session.session_id == "c1" and session.cwd == "C:\\w"
    assert [(t.user, t.reply) for t in session.turns] == [("audit skills", "report ready")]
    assert session.notes == ["Task Overview"]


def test_worklog_matches_by_alias_then_by_tool_and_folder():
    from ai_daily_digest.model import Session
    from ai_daily_digest.render import match_worklog
    from ai_daily_digest.sources.worklog import WorklogEntry

    def entry(sid, tool, path):
        return WorklogEntry(sid, sid, tool, sid, "p", path, "s", "completed", "", "")

    root = Session("Codex", "t1", cwd="E:\\Proj\\Paper", aliases={"sub1"})
    other = Session("Claude Code", "c1", cwd="E:\\Proj\\Paper")
    third = Session("agy", "a1", cwd="E:\\Proj\\Paper\\draft")
    by_id, by_path = match_worklog([root, other, third], [
        entry("sub1", "codex-app", "E:/Proj/Paper"),
        entry("made-up-slug", "codex-app", "E:/proj/paper/"),
        entry("typo", "claude-code", "E:/Elsewhere"),
        entry("agy-slug", "agy-cli", "E:/Proj/Paper"),
    ])
    assert [e.entry_id for e in by_id["t1"]] == ["sub1"]
    assert [e.entry_id for e in by_path["t1"]] == ["made-up-slug"]
    assert "c1" not in by_id and "c1" not in by_path
    assert [e.entry_id for e in by_path["a1"]] == ["agy-slug"]


def test_dida_due_tasks_filters_by_horizon_and_maps_priority_to_quadrant(monkeypatch):
    from ai_daily_digest import dida
    from ai_daily_digest.config import Config

    today = date(2026, 10, 10)

    def stamp(d, hour=None):
        local = datetime(d.year, d.month, d.day, hour or 0).astimezone()
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")

    data = {
        "/project": [{"id": "p1", "name": "论文"}, {"id": "p2", "name": "旧", "closed": True}],
        "/project/inbox/data": {"tasks": [
            {"title": "逾期", "priority": 5, "isAllDay": True, "dueDate": stamp(date(2026, 10, 8))},
            {"title": "无日期", "priority": 5},
        ]},
        "/project/p1/data": {"tasks": [
            {"title": "第七天", "priority": 3, "isAllDay": False, "dueDate": stamp(date(2026, 10, 17), 15)},
            {"title": "第八天", "priority": 3, "isAllDay": True, "dueDate": stamp(date(2026, 10, 18))},
            {"title": "已完成", "priority": 1, "status": 2, "dueDate": stamp(today)},
        ]},
    }
    monkeypatch.setattr(dida, "_api", lambda cfg, method, path, payload=None: data[path])
    tasks = dida.due_tasks(Config(), today)
    assert [(t.title, t.quadrant, t.project, t.due_time) for t in tasks] == [
        ("逾期", 1, "收集箱", ""), ("第七天", 2, "论文", "15:00")]
    text = "\n".join(dida.render_section(tasks, today, 7))
    assert "### 重要且紧急" in text and "逾期 2 天" in text and "10-17 15:00（7 天后）第七天 · 论文" in text


def test_dida_add_task_sends_quadrant_as_priority_and_all_day_due(monkeypatch):
    from ai_daily_digest import dida
    from ai_daily_digest.config import Config

    sent = {}
    monkeypatch.setattr(dida, "_api", lambda cfg, method, path, payload=None: sent.update(payload) or payload)
    dida.add_task(Config(), "交稿", quadrant=2, due=date(2026, 10, 15))
    assert sent["priority"] == 3 and sent["isAllDay"] is True and "projectId" not in sent
    assert dida.parse_due(sent["dueDate"]).astimezone().date() == date(2026, 10, 15)


def test_dida_timeout_is_a_recoverable_dida_error(monkeypatch):
    from ai_daily_digest import dida

    class TimedOutResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            raise TimeoutError("read timed out")

    monkeypatch.setattr(dida.urllib.request, "urlopen", lambda *args, **kwargs: TimedOutResponse())
    with pytest.raises(dida.DidaError, match="read timed out"):
        dida._request("GET", "https://example.test", headers={})


def test_periods_cover_whole_weeks_and_months():
    from ai_daily_digest.periodic import Period

    week = Period.week_of(date(2026, 10, 10))
    assert (week.start, week.end, week.label) == (date(2026, 10, 5), date(2026, 10, 11), "2026-W41")
    assert week.previous().label == "2026-W40"
    month = Period.month_of(date(2026, 2, 14))
    assert (month.start, month.end, month.label) == (date(2026, 2, 1), date(2026, 2, 28), "2026-02")
    assert month.previous().label == "2026-01" and Period.week_of(date(2027, 1, 1)).label == "2026-W53"


def test_weekly_report_uses_daily_bodies_and_counts(tmp_path):
    from ai_daily_digest import periodic
    from ai_daily_digest.config import Config
    from ai_daily_digest.sources.worklog import WorklogEntry

    cfg = Config(vault_dir=tmp_path, shelved_projects=["旧项目"])
    period = periodic.Period.week_of(date(2026, 10, 9))
    note = periodic.daily_path(cfg, date(2026, 10, 9))
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: ai-daily-digest\nsessions_claude_code: 7\nsessions_codex: 9\nsessions_agy: 1\n---\n\n"
        "# AI 日报 2026-10-09\n\n## 概览\n\n做了甲。\n\n## 各项工作\n\n### 甲\n\n完成。\n\n"
        "## 会话清单\n\n| 工具 |\n", encoding="utf-8")
    dailies = periodic.read_dailies(cfg, period)
    front, body = dailies[date(2026, 10, 9)]
    assert front["sessions_codex"] == "9" and body.startswith("## 概览") and "会话清单" not in body
    entry = WorklogEntry("20261009-x", "写完甲", "codex-app", "s", "甲", "E:/a", "a", "completed",
                         "任务摘要", "", ["补附录"], date(2026, 10, 9))
    bundle = periodic.build_bundle(cfg, period, dailies, [entry], {})
    assert "## 日报 2026-10-09" in bundle and "旧项目" in bundle and "任务摘要" in bundle
    text = periodic.render_note(cfg, period, dailies, [entry], "## 本周概览\n\n好。")
    assert "> | 10-09 Fri | 7 | 9 | 1 | 1 |" in text and "> | 合计 | 7 | 9 | 1 | 1 | 1/7 天 |" in text
    assert "> - 补附录（甲，[[AI 工作留痕/entries/2026/2026-10/a/20261009-x|写完甲]]）" in text
    # A report's own appendix must not leak into the body the monthly report reads.
    assert periodic._split(text)[1] == "## 本周概览\n\n好。"


def test_periodic_bundle_compresses_every_day_without_losing_worklog_or_late_days(tmp_path):
    from ai_daily_digest import periodic
    from ai_daily_digest.config import Config
    from ai_daily_digest.sources.worklog import WorklogEntry

    cfg = Config(bundle_budget=1800)
    period = periodic.Period.week_of(date(2026, 10, 9))
    dailies = {
        day: ({}, "## 概览\n\n" + ("甲" if n == 0 else "乙") * 4000)
        for n, day in enumerate(period.days)
    }
    entry = WorklogEntry("late", "期末留痕", "codex-app", "s", "项目", "E:/a", "a", "completed", "完成", "", [], period.end)
    bundle = periodic.build_bundle(cfg, period, dailies, [entry], {})
    assert len(bundle) <= cfg.bundle_budget
    assert "## 输入压缩说明" in bundle and "期末留痕" in bundle
    assert f"## 日报 {period.start}" in bundle and f"## 日报 {period.end}" in bundle


def test_periodic_split_excludes_the_daily_investment_snapshot():
    from ai_daily_digest import periodic

    _, body = periodic._split("# AI 日报\n\n## 概览\n\n工作。\n\n## 投资机会（截至 08:00）\n\n今日扫描。\n")
    assert body == "## 概览\n\n工作。"


def test_disclose_folds_project_detail_but_keeps_overview_and_top_items():
    from ai_daily_digest.render import disclose

    body = "\n".join([
        "## 本周概览", "", "总述。", "", "## 一句话进展", "", "- **甲**：完成", "",
        "## 各项目进展", "", "### 甲", "", "细节一", "", "- 细节二", "",
        "## 未完成与待跟进", "", "- 甲：最要紧", "", "### 甲", "", "- 全部一", "",
        "## 值得留意", "", "### 不折叠", "", "无",
    ])
    text = disclose(body)
    assert "## 一句话进展\n\n- **甲**：完成" in text and "\n- 甲：最要紧\n" in text
    assert "> [!abstract]- 甲\n>\n> 细节一\n>\n> - 细节二\n" in text
    assert "> [!abstract]- 甲\n>\n> - 全部一\n" in text
    assert "### 不折叠" in text and "### 甲" not in text


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")


def test_latest_embeds_newest_daily_with_qualified_links_and_caps_earlier(tmp_path):
    from ai_daily_digest import latest
    from ai_daily_digest.config import Config

    cfg = Config(vault_dir=tmp_path)
    root = tmp_path / cfg.digest_folder
    assert latest.refresh(cfg) == "empty" and not latest.note_path(cfg).exists()
    for stem in ("2025-12-30", "2025-12-31", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
                 "2026-10-05", "2026-10-06", "2026-10-07"):
        _touch(root / stem[:4] / f"{stem}.md")
    _touch(root / "2026" / "2026-10-08.md.tmp")
    _touch(root / "2026" / "notes.md")
    _touch(root / "2026" / "2026-10-09.txt")
    text = latest.render(cfg)
    assert "latest: 2026-10-07\n" in text and text.endswith("![[AI 日报/2026/2026-10-07]]\n")
    assert "**更早**：[[AI 日报/2026/2026-10-06|10-06]] · [[AI 日报/2026/2026-10-05|10-05]]" in text
    assert text.count("·") == 6 and "|12-31]]" in text and "12-30" not in text
    assert "周报" not in text and "月报" not in text
    _touch(root / "周报" / "2026-W39.md")
    _touch(root / "周报" / "2026-W40.md")
    _touch(root / "月报" / "2026-09.md")
    text = latest.render(cfg)
    assert "**周报**：[[AI 日报/周报/2026-W40|2026-W40]] · **月报**：[[AI 日报/月报/2026-09|2026-09]]\n" in text
    assert latest.refresh(cfg).startswith("written ") and latest.refresh(cfg) == "unchanged"
    assert latest.note_path(cfg).read_text(encoding="utf-8") == text


def test_latest_crosses_year_boundary_and_drops_missing_parts(tmp_path):
    from ai_daily_digest import latest
    from ai_daily_digest.config import Config

    cfg = Config(vault_dir=tmp_path)
    root = tmp_path / cfg.digest_folder
    _touch(root / "2025" / "2025-12-31.md")
    assert "更早" not in latest.render(cfg)
    _touch(root / "2026" / "2026-01-02.md")
    _touch(root / "月报" / "2025-12.md")
    text = latest.render(cfg)
    assert "**更早**：[[AI 日报/2025/2025-12-31|12-31]]\n" in text
    assert "**月报**：[[AI 日报/月报/2025-12|2025-12]]\n" in text and "周报" not in text
