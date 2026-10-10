import json
import os
from datetime import date, datetime, timedelta, timezone

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
    by_id, by_path = match_worklog([root, other], [
        entry("sub1", "codex-app", "E:/Proj/Paper"),
        entry("made-up-slug", "codex-app", "E:/proj/paper/"),
        entry("typo", "claude-code", "E:/Elsewhere"),
    ])
    assert [e.entry_id for e in by_id["t1"]] == ["sub1"]
    assert [e.entry_id for e in by_path["t1"]] == ["made-up-slug"]
    assert "c1" not in by_id and "c1" not in by_path


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
