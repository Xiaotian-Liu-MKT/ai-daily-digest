"""Build the model input and the final Obsidian note."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

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
每个项目或主题一个三级标题，下面用短段落或列表写做了什么、结论或产物、状态。这一节会被折叠，标题要让人一眼看出是哪件事。

## 未完成与待跟进
先直接列出最要紧的至多五条（不加小标题），每条带上项目名；再按项目各用一个三级标题列出完整清单，包括明确没做完、被搁置、或者用户说了稍后再做的事。没有就写"无"。

## 值得留意
当天做出的重要决定、发现的问题或风险。没有就写"无"。

以下是材料。

"""


APPENDIX = "> [!info]- "


def fold(title: str, lines: list[str], kind: str = "info") -> list[str]:
    """An Obsidian callout that starts collapsed, so detail stays one click away."""
    return [f"> [!{kind}]- {title}"] + [f"> {line}".rstrip() for line in lines] + [""]


_FOLDED_SECTIONS = ("各项", "未完成")


def disclose(body: str) -> str:
    """Collapse the per-project detail so the report opens as an overview.

    Under the detail sections every `### project` block becomes a folded callout;
    whatever precedes the first block (the short top-priority list) stays visible.
    """
    out: list[str] = []
    folding = False
    block: list[str] | None = None
    title = ""

    def flush():
        nonlocal block
        if block is not None:
            while block and not block[-1].strip():
                block.pop()
            out.extend(fold(title, block, "abstract"))
            block = None

    for line in body.splitlines():
        if line.startswith("## "):
            flush()
            folding = line[3:].strip().startswith(_FOLDED_SECTIONS)
            out.append(line)
        elif folding and line.startswith("### "):
            flush()
            title, block = line[4:].strip(), []
        elif block is not None:
            block.append(line)
        else:
            out.append(line)
    flush()
    return "\n".join(out).strip()


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


_TOOL_FAMILY = {"Claude Code": "claude", "Codex": "codex"}


def _norm(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").lower()


def _same_tree(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def match_worklog(
    sessions: list[Session], entries: list[WorklogEntry]
) -> tuple[dict[str, list[WorklogEntry]], dict[str, list[WorklogEntry]]]:
    """Attach worklog entries to sessions: by session id, else by tool and project folder.

    The recorded session id is typed by an agent, so it is sometimes a slug, a
    subagent's thread or a mistyped uuid. The folder match is weaker: every session
    of that tool in the folder gets the entry.
    """
    by_id: dict[str, list[WorklogEntry]] = {}
    by_path: dict[str, list[WorklogEntry]] = {}
    ids = {alias: s.session_id for s in sessions for alias in (s.session_id, *s.aliases)}
    home = _norm(str(Path.home()))
    for e in entries:
        if e.session_id in ids:
            by_id.setdefault(ids[e.session_id], []).append(e)
            continue
        path = _norm(e.project_path)
        if not path or path == home:
            continue
        for s in sessions:
            cwd = _norm(s.cwd)
            family = _TOOL_FAMILY.get(s.tool)
            if family and e.tool.startswith(family) and cwd and cwd != home and _same_tree(cwd, path):
                by_path.setdefault(s.session_id, []).append(e)
    return by_id, by_path


def _session_block(
    session: Session, logged: list[WorklogEntry], nearby: list[WorklogEntry],
    user_cap: int, reply_cap: int,
) -> str:
    head = f"### [{session.tool}] {_title(session)}"
    meta = [f"目录 {session.cwd or '?'}", f"时间 {_span(session)}", f"{session.human_turns} 条用户输入"]
    if session.subagents:
        meta.append(f"派出约 {session.subagents} 个子 agent")
    if session.continued:
        meta.append("续之前的会话")
    if logged:
        meta.append("已留痕：" + "；".join(e.title for e in logged))
    if nearby:
        meta.append("同目录留痕：" + "；".join(e.title for e in nearby))
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
    by_id, by_path = match_worklog(sessions, entries)
    tz = datetime.now().astimezone().strftime("UTC%z")
    head = INSTRUCTIONS.format(day=window.day.isoformat(), tz=tz) + _worklog_block(entries)
    user_cap, reply_cap = cfg.user_cap, cfg.reply_cap
    while True:
        blocks = [
            _session_block(
                s, by_id.get(s.session_id, []), by_path.get(s.session_id, []), user_cap, reply_cap
            )
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
    extra: list[str] | None = None,
) -> str:
    day = window.day
    by_id, by_path = match_worklog(sessions, entries)

    def _logged(s: Session) -> str:
        return "有" if s.session_id in by_id else "同目录" if s.session_id in by_path else ""

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
    out = front + [disclose(body) if body else "> 本篇未调用模型，只有下面的会话清单。", ""]
    out += extra or []

    table = ["| 工具 | 会话 | 目录 | 时间 | 输入 | 子 agent | 留痕 |", "|---|---|---|---|---|---|---|"]
    for s in sorted(sessions, key=lambda s: s.first_ts or window.start):
        table.append(
            f"| {s.tool} | {_cell(_title(s))} | `{_cell(s.cwd)}` | {_span(s)} | {s.human_turns} "
            f"| {s.subagents or ''} | {_logged(s)} |"
        )
    out += fold(f"会话清单（{len(sessions)} 个）", table)
    out += fold(
        f"当日留痕记录（{len(entries)} 条）",
        [f"- {e.note_link(day)}（{e.tool}，{e.status}）" for e in entries] or ["无"],
    )
    unlogged = [s for s in sessions if not _logged(s)]
    out += fold(
        f"没有对应留痕的会话（{len(unlogged)} 个）",
        [f"- [{s.tool}] {_cell(_title(s))}（`{s.cwd}`，{s.human_turns} 条输入）" for s in unlogged] or ["无"],
    )
    return "\n".join(out) + "\n"
