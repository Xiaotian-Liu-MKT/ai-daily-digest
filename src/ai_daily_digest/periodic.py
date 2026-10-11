"""Weekly and monthly reports, built from the daily digests and the worklog records."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Config
from .render import APPENDIX, disclose, fold
from .sources.worklog import WorklogEntry

_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_COUNT_KEYS = ("sessions_claude_code", "sessions_codex", "sessions_agy", "worklog_entries")
_TODO_CAP = 60

_RULES = """写作要求：
- 只依据材料写，不补充材料里没有的事实、数字或结论；材料不足以判断的地方直接说不清楚。
- 按项目或主题归并，把同一件事在多天里的进展合成一条，写清期初到期末发生了什么变化、现在的状态；不要逐日复述。
- 留痕记录是 agent 完成任务后写的总结，可信度高于日报正文，两者冲突时以留痕为准。
- 用简体中文，句子完整，语气平实，不要客套话，不要用表情符号。
- 文件、命令、路径用反引号。
"""

_WEEK = """你在为用户本人写一份 AI 协作工作周报，覆盖 {label}（{start} 至 {end}）。下面的材料是这一周每天的日报正文，以及这一周写入的工作留痕记录。

{rules}{shelved}
只输出下面五个二级标题及其内容，不要输出一级标题、frontmatter、前言或结语：

## 本周概览
三到五句话，说清这一周主要推进了什么、整体进度如何。

## 一句话进展
每个项目或主题一行，格式为 `- **项目名**：一句话说清这一周的结果和周末时的状态`。读者只看这一节就应当知道每件事到哪了。

## 各项目进展
每个项目或主题一个三级标题（与上一节的项目名一致），写这一周做成了什么、关键结论或产物、周末时的状态。这一节会被折叠，可以写细。

## 未完成与待跟进
先直接列出最要紧的至多五条（不加小标题），每条带上项目名；再按项目各用一个三级标题列出完整清单。没有就写"无"。

## 值得留意
这一周做出的重要决定、反复出现的问题或风险。没有就写"无"。

以下是材料。

"""

_MONTH = """你在为用户本人写一份 AI 协作工作月报，覆盖 {label}（{start} 至 {end}）。下面的材料是与这个月重叠的各周周报、这个月每天的日报概览，以及这个月工作留痕记录的汇总。周报可能包含相邻月份的几天，只写属于这个月的内容。

{rules}{shelved}
只输出下面五个二级标题及其内容，不要输出一级标题、frontmatter、前言或结语：

## 本月概览
四到六句话，说清这个月的主线、完成了哪些有分量的事。

## 一句话进展
每个项目或主题一行，格式为 `- **项目名**：一句话说清这个月的结果和月末时的状态`。读者只看这一节就应当知道每件事到哪了。

## 各项目进展
每个项目或主题一个三级标题（与上一节的项目名一致），写这个月的进展、关键结论或产物、月末时的状态。这一节会被折叠，可以写细。

## 未完成与待跟进
先直接列出最要紧的至多五条（不加小标题），每条带上项目名；再按项目各用一个三级标题列出完整清单。没有就写"无"。

## 值得留意
这个月做出的重要决定、反复出现的问题或风险。没有就写"无"。

以下是材料。

"""


@dataclass
class Period:
    kind: str  # "week" or "month"
    start: date
    end: date  # inclusive

    @classmethod
    def week_of(cls, day: date) -> "Period":
        start = day - timedelta(days=day.weekday())
        return cls("week", start, start + timedelta(days=6))

    @classmethod
    def month_of(cls, day: date) -> "Period":
        start = day.replace(day=1)
        return cls("month", start, (start + timedelta(days=32)).replace(day=1) - timedelta(days=1))

    def previous(self) -> "Period":
        before = self.start - timedelta(days=1)
        return Period.week_of(before) if self.kind == "week" else Period.month_of(before)

    @property
    def label(self) -> str:
        if self.kind == "week":
            year, week, _ = self.start.isocalendar()
            return f"{year}-W{week:02d}"
        return f"{self.start:%Y-%m}"

    @property
    def title(self) -> str:
        return f"AI {'周报' if self.kind == 'week' else '月报'} {self.label}"

    @property
    def marker(self) -> str:
        return "## 本周概览" if self.kind == "week" else "## 本月概览"

    @property
    def days(self) -> list[date]:
        return [self.start + timedelta(days=n) for n in range((self.end - self.start).days + 1)]

    def path(self, cfg: Config) -> Path:
        folder = "周报" if self.kind == "week" else "月报"
        return cfg.vault_dir / cfg.digest_folder / folder / f"{self.label}.md"


def daily_path(cfg: Config, day: date) -> Path:
    return cfg.vault_dir / cfg.digest_folder / f"{day:%Y}" / f"{day.isoformat()}.md"


def _split(text: str) -> tuple[dict[str, str], str]:
    """Frontmatter fields and the model-written body of a digest or report note."""
    text = text.replace("\r\n", "\n")
    front: dict[str, str] = {}
    m = _FRONT.match(text)
    if m:
        for line in m.group(1).splitlines():
            key, _, value = line.partition(":")
            front[key.strip()] = value.strip()
        text = text[m.end():]
    start = text.find("\n## ")
    body = text[start + 1:] if start >= 0 else ""
    # Everything from the first generated appendix on is tables and links, not prose.
    for heading in ("\n## 投资机会", "\n## 滴答清单", "\n## 会话清单", "\n## 每日统计", "\n" + APPENDIX):
        cut = body.find(heading)
        if cut >= 0:
            body = body[:cut]
    return front, body.strip()


def _overview(body: str) -> str:
    m = re.search(r"^## 概览\n+(.*?)(?=^## |\Z)", body, re.S | re.M)
    return m.group(1).strip() if m else ""


def read_dailies(cfg: Config, period: Period) -> dict[date, tuple[dict[str, str], str]]:
    found = {}
    for day in period.days:
        path = daily_path(cfg, day)
        if path.exists():
            found[day] = _split(path.read_text(encoding="utf-8"))
    return found


def _clip(text: str, cap: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= cap else text[:cap].rstrip() + "…"


def _worklog_block(entries: list[WorklogEntry], summary_cap: int) -> str:
    if not entries:
        return "## 留痕记录\n（期内没有留痕记录）"
    lines = ["## 留痕记录（按项目）"]
    by_project: dict[str, list[WorklogEntry]] = {}
    for e in entries:
        by_project.setdefault(e.project or e.scope_key, []).append(e)
    for project, items in sorted(by_project.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"### {project}（{len(items)} 条）")
        for e in sorted(items, key=lambda e: e.day):
            line = f"- {e.day:%m-%d} {e.title}（{e.status}）"
            if summary_cap and e.task_summary:
                line += f"：{_clip(e.task_summary, summary_cap)}"
            lines.append(line)
    return "\n".join(lines)


def build_bundle(
    cfg: Config, period: Period, dailies: dict, entries: list[WorklogEntry], weeklies: dict[str, str]
) -> str:
    shelved = ""
    if cfg.shelved_projects:
        shelved = (
            "- 以下项目已经搁置：" + "、".join(cfg.shelved_projects)
            + "。可以如实写它们在期内做过什么，但要注明已搁置，不要把它们的事项列入待跟进。\n"
        )
    template = _WEEK if period.kind == "week" else _MONTH
    head = template.format(
        label=period.label, start=period.start, end=period.end, rules=_RULES, shelved=shelved
    )
    summary_caps = [160, 80, 0] if period.kind == "week" else [0]
    # Preserve every dated source and every worklog line.  Under pressure,
    # shorten each source evenly instead of slicing the assembled string,
    # which used to silently erase late-week material and the worklog block.
    source_caps: list[int | None] = [None, 6000, 3000, 1200, 500, 0]
    for summary_cap in summary_caps:
        for source_cap in source_caps:
            compressed = summary_cap != summary_caps[0] or source_cap is not None
            def source_block(heading: str, body: str) -> str:
                if source_cap is None or len(body) <= source_cap:
                    shown = body
                elif source_cap == 0:
                    shown = "（正文因输入预算已压缩；详见原日报或周报。）"
                else:
                    shown = _clip(body, source_cap)
                return f"{heading}\n\n{shown}"
            parts = [head]
            if period.kind == "week":
                parts += [source_block(f"## 日报 {day}", body) for day, (_, body) in sorted(dailies.items())]
            else:
                parts += [source_block(f"## 周报 {label}", body) for label, body in sorted(weeklies.items())]
                overviews = [f"- {day}：{_overview(body)}" for day, (_, body) in sorted(dailies.items()) if _overview(body)]
                if overviews:
                    parts.append("## 每日概览\n\n" + "\n".join(overviews))
            if compressed:
                parts.append("## 输入压缩说明\n已保留全期日期覆盖和全部留痕；日报或周报正文已按每项压缩。")
            parts.append(_worklog_block(entries, summary_cap))
            bundle = "\n\n".join(parts)
            if len(bundle) <= cfg.bundle_budget:
                return bundle
    # A worklog-only bundle may itself exceed the budget.  Do not silently
    # discard it: its complete audit trail is more valuable than a hard cap.
    return bundle


def render_note(
    cfg: Config, period: Period, dailies: dict, entries: list[WorklogEntry],
    body: str | None, extra: list[str] | None = None,
) -> str:
    out = [
        "---",
        f"type: ai-{'weekly' if period.kind == 'week' else 'monthly'}-digest",
        f"period: {period.label}",
        f"start: {period.start}",
        f"end: {period.end}",
        f"generated_at: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"model: {cfg.model if body else 'none'}",
        f"daily_digests: {len(dailies)}",
        f"worklog_entries: {len(entries)}",
        "---",
        "",
        f"# {period.title}",
        "",
        disclose(body) if body else "> 本篇未调用模型，只有下面的统计。",
        "",
    ]
    out += extra or []

    table = ["| 日期 | Claude Code | Codex | agy | 留痕 | 日报 |", "|---|---|---|---|---|---|"]
    totals = [0, 0, 0, 0]
    per_day: dict[date, int] = {}
    for e in entries:
        per_day[e.day] = per_day.get(e.day, 0) + 1
    for day in period.days:
        front = dailies.get(day, ({}, ""))[0]
        counts = [int(front.get(k, 0) or 0) for k in _COUNT_KEYS[:3]] + [per_day.get(day, 0)]
        if not any(counts) and day not in dailies:
            continue
        totals = [a + b for a, b in zip(totals, counts)]
        # Inside a table the alias separator must be escaped or it splits the cell.
        link = f"[[{cfg.digest_folder}/{day:%Y}/{day.isoformat()}\\|{day:%m-%d}]]" if day in dailies else ""
        table.append(f"| {day:%m-%d %a} | {counts[0] or ''} | {counts[1] or ''} | {counts[2] or ''} | {counts[3] or ''} | {link} |")
    table.append(f"| 合计 | {totals[0]} | {totals[1]} | {totals[2]} | {totals[3]} | {len(dailies)}/{len(period.days)} 天 |")
    out += fold(f"每日统计（日报 {len(dailies)}/{len(period.days)} 天）", table)

    by_project: dict[str, int] = {}
    for e in entries:
        by_project[e.project or e.scope_key] = by_project.get(e.project or e.scope_key, 0) + 1
    out += fold(
        f"期内留痕按项目（{len(entries)} 条）",
        [f"- {name}：{n} 条" for name, n in sorted(by_project.items(), key=lambda kv: -kv[1])] or ["无"],
    )

    todos = [(e, t) for e in entries for t in e.open_todos]
    lines = [
        f"- {_clip(text, 200)}（{e.project or e.scope_key}，{e.note_link(e.day)}）"
        for e, text in todos[:_TODO_CAP]
    ] or ["无"]
    if len(todos) > _TODO_CAP:
        lines.append(f"- 另有 {len(todos) - _TODO_CAP} 条未列出")
    out += fold(f"留痕里仍未完成的待办（{len(todos)} 条）", lines)
    return "\n".join(out) + "\n"
