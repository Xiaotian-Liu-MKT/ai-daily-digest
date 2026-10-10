"""Build the model input and the final Obsidian note."""
from __future__ import annotations

from datetime import datetime

from .config import Config
from .model import Session, Window
from .sources.worklog import WorklogEntry

INSTRUCTIONS = """你在为用户本人写一份 AI 协作工作日报。下面的材料是 {day} 这一天（本地时区 {tz}）用户在 Claude Code、Codex、agy 三个 AI 编码工具里的会话摘录，以及当天已写入的工作留痕记录。

写作要求：
- 只依据材料写，不补充材料里没有的事实、数字或结论；材料不足以判断的地方直接说不清楚。
- 留痕记录是 agent 完成任务后写的总结，可信度高于会话摘录，两者冲突时以留痕为准。
- 按项目或主题归并，不要按工具或按会话逐条复述；同一件事跨多个会话、多个工具时合成一条。
- 写清每件事做了什么、得到什么结论或产物、现在的状态。省略过程性的工具调用细节。
- 用简体中文，句子完整，语气平实，不要客套话，不要用表情符号。
- 文件、命令、路径用反引号。

只输出下面四个二级标题及其内容，不要输出一级标题、frontmatter、前言或结语：

## 概览
三到五句话，说清这一天主要在推进什么。

## 各项工作
每个项目或主题一个三级标题，下面用短段落或列表写做了什么、结论或产物、状态。

## 未完成与待跟进
列出明确没做完、被搁置、或者用户说了稍后再做的事。没有就写"无"。

## 值得留意
当天做出的重要决定、发现的问题或风险。没有就写"无"。

以下是材料。

"""


def _clip(text: str, cap: int) -> str:
    text = text.strip()
    return text if len(text) <= cap else text[:cap].rstrip() + f"…（截断，原文 {len(text)} 字）"


def _local(ts: datetime | None) -> str:
    return ts.astimezone().strftime("%H:%M") if ts else "?"


def _span(session: Session) -> str:
    return f"{_local(session.first_ts)}–{_local(session.last_ts)}"


def _title(session: Session) -> str:
    if session.title:
        return session.title
    first = next((t.user for t in session.turns if t.user), "")
    return _clip(first.splitlines()[0] if first else "(无标题)", 40)


def _session_block(session: Session, logged: list[WorklogEntry], user_cap: int, reply_cap: int) -> str:
    head = f"### [{session.tool}] {_title(session)}"
    meta = [f"目录 {session.cwd or '?'}", f"时间 {_span(session)}", f"{session.human_turns} 条用户输入"]
    if session.subagents:
        meta.append(f"派出约 {session.subagents} 个子 agent")
    if session.continued:
        meta.append("续之前的会话")
    if logged:
        meta.append("已留痕：" + "；".join(e.title for e in logged))
    lines = [head, " | ".join(meta)]
    lines += [f"[进展回顾] {_clip(n, reply_cap)}" for n in session.notes]
    for turn in session.turns:
        if turn.user:
            lines.append(f"用户：{_clip(turn.user, user_cap)}")
        if turn.reply:
            lines.append(f"助手：{_clip(turn.reply, reply_cap)}")
    return "\n".join(lines)


def _worklog_block(entries: list[WorklogEntry]) -> str:
    if not entries:
        return "## 留痕记录\n（当天没有留痕记录）"
    lines = ["## 留痕记录"]
    for e in entries:
        lines.append(f"- [{e.tool}] {e.title} | 项目 {e.project} | 状态 {e.status}")
        if e.task_summary:
            lines.append(f"  任务：{e.task_summary}")
        if e.change_summary:
            lines.append(f"  改动：{e.change_summary}")
        lines += [f"  待办：{t}" for t in e.open_todos]
    return "\n".join(lines)


def build_bundle(
    window: Window, sessions: list[Session], entries: list[WorklogEntry], cfg: Config
) -> str:
    by_session: dict[str, list[WorklogEntry]] = {}
    for e in entries:
        by_session.setdefault(e.session_id, []).append(e)
    tz = datetime.now().astimezone().strftime("UTC%z")
    head = INSTRUCTIONS.format(day=window.day.isoformat(), tz=tz) + _worklog_block(entries)
    user_cap, reply_cap = cfg.user_cap, cfg.reply_cap
    while True:
        blocks = [
            _session_block(s, by_session.get(s.session_id, []), user_cap, reply_cap)
            for s in sessions
        ]
        bundle = head + "\n\n## 会话摘录\n\n" + "\n\n".join(blocks)
        if len(bundle) <= cfg.bundle_budget or reply_cap <= 200:
            return bundle
        # Heavy days: shrink replies first, since prompts carry the intent.
        reply_cap = max(200, int(reply_cap * 0.7))
        user_cap = max(300, int(user_cap * 0.85))


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def render_note(
    window: Window,
    sessions: list[Session],
    entries: list[WorklogEntry],
    body: str | None,
    model: str,
) -> str:
    day = window.day
    logged_ids = {e.session_id for e in entries}
    counts = {tool: sum(1 for s in sessions if s.tool == tool) for tool in ("Claude Code", "Codex", "agy")}
    front = [
        "---",
        "type: ai-daily-digest",
        f"date: {day.isoformat()}",
        f"generated_at: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"model: {model if body else 'none'}",
        f"sessions_claude_code: {counts['Claude Code']}",
        f"sessions_codex: {counts['Codex']}",
        f"sessions_agy: {counts['agy']}",
        f"worklog_entries: {len(entries)}",
        "---",
        "",
        f"# AI 日报 {day.isoformat()}",
        "",
    ]
    out = front + [body.strip() if body else "> 本篇未调用模型，只有下面的会话清单。", ""]

    out += ["## 会话清单", "", "| 工具 | 会话 | 目录 | 时间 | 输入 | 子 agent | 留痕 |", "|---|---|---|---|---|---|---|"]
    for s in sorted(sessions, key=lambda s: s.first_ts or window.start):
        out.append(
            f"| {s.tool} | {_cell(_title(s))} | `{_cell(s.cwd)}` | {_span(s)} | {s.human_turns} "
            f"| {s.subagents or ''} | {'有' if s.session_id in logged_ids else ''} |"
        )
    out += ["", "## 当日留痕记录", ""]
    out += [f"- {e.note_link(day)}（{e.tool}，{e.status}）" for e in entries] or ["无"]
    unlogged = [s for s in sessions if s.session_id not in logged_ids]
    out += ["", "## 没有对应留痕的会话", ""]
    out += [f"- [{s.tool}] {_cell(_title(s))}（`{s.cwd}`，{s.human_turns} 条输入）" for s in unlogged] or ["无"]
    return "\n".join(out) + "\n"
