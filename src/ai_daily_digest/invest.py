"""Daily investment view: re-run the investment-research scan, then ask Codex what is new."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .render import _cell, fold

log = logging.getLogger("ai_daily_digest")
STALE_HOURS = 24 * 7
VERDICTS = {
    "new_opportunity": "新机会",
    "risk_up": "风险上升",
    "no_change": "无变化",
    "insufficient_data": "数据不足",
}
STANCES = {"bullish": "偏多", "bearish": "偏空", "mixed": "分歧", "unknown": "未知"}
DISCLAIMER = "> 研究参考，不构成交易建议。"

Runner = Callable[[Config, str, dict], dict]


class InvestError(Exception):
    pass


def _tail(text: str | None, n: int = 400) -> str:
    return (text or "").strip()[-n:]


def invest_python(cfg: Config) -> Path:
    if cfg.invest_python:
        return Path(cfg.invest_python)
    sub = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    return cfg.invest_repo / ".venv" / sub[0] / sub[1]


def run_scan(cfg: Config) -> dict:
    """Run `market_research daily-scan --json` in the sibling repo and parse its stdout."""
    python = invest_python(cfg)
    if not cfg.invest_repo.is_dir():
        raise InvestError(f"找不到投资研究仓库：{cfg.invest_repo}")
    if not python.exists():
        raise InvestError(f"找不到投资研究的 Python：{python}")
    try:
        proc = subprocess.run(
            [str(python), "-m", "market_research", "daily-scan", "--json"],
            cwd=cfg.invest_repo, capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=cfg.invest_scan_timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise InvestError(f"重跑超过 {cfg.invest_scan_timeout_s} 秒") from exc
    try:
        scan = json.loads(proc.stdout)
    except ValueError as exc:
        raise InvestError(f"退出码 {proc.returncode}，输出不是 JSON：{_tail(proc.stderr)}") from exc
    try:
        _validate_scan(scan)
    except InvestError as exc:
        raise InvestError(f"退出码 {proc.returncode}，{exc}：{_tail(proc.stderr)}") from exc
    return scan


# ------------------------------------------------------------------- codex
def output_schema() -> dict:
    text_or_null = {"type": ["string", "null"]}
    strings = {"type": "array", "items": {"type": "string"}}
    item = {
        "type": "object", "additionalProperties": False,
        "required": ["instrument", "verdict", "headline", "evidence", "conditions_hit", "next_check", "sources"],
        "properties": {
            "instrument": {"type": "string"},
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "headline": {"type": "string"},
            "evidence": {"type": "string"},
            "conditions_hit": strings,
            "next_check": text_or_null,
            "sources": strings,
        },
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["has_new_opportunity", "summary", "items", "gaps"],
        "properties": {
            "has_new_opportunity": {"type": "boolean"},
            "summary": {"type": "string"},
            "items": {"type": "array", "items": item},
            "gaps": strings,
        },
    }


def build_prompt(scan: dict, today: date) -> str:
    data = json.dumps(scan, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"""今天是 {today.isoformat()}。下面的 JSON 是本地投资研究工具刚刚重跑的结果：每个标的的最新行情、决策与估值摘要、近期事件，以及它此前保存的研究结论（conclusion，含 researched_at、立场、升级/下行条件）。

任务：逐个标的判断，自 conclusion.researched_at 以来的新证据（较结论的价格变动 change_since_conclusion_pct、决策信号、临近事件、升级/下行条件是否已满足或接近、以及你用网络搜索找到的新闻）相对原立场，是否构成值得新研究的【新机会】或【实质性风险变化】。
要求：
1. 任何声称的新闻或事实必须附来源链接（sources）；不得编造数字或日期，数字只能来自 JSON 或所引来源。
2. 不给仓位、下单或个性化建议；只是研究参考。
3. 没有变化就如实说没有。“没有新机会”是正常且常见的答案，不要为了有内容而硬凑。
4. 结论过旧（超过 7 天）、review_status 不是 reviewed、或标的重跑失败（ok=false），写入 gaps，并对该标的给 insufficient_data 或按已有证据判断。
5. items 覆盖每个标的各一条；summary 用中文，不超过三句；其余字段也用中文。

数据：
{data}
"""


def codex_command(exe: str, model: str, work: str, schema_path: str, out_path: str) -> list[str]:
    return [exe, "--search", "exec", "-m", model, "--sandbox", "read-only", "--skip-git-repo-check",
            # Keeps this run out of ~/.codex/sessions, so tomorrow's digest does not list it.
            "--ephemeral", "-C", work, "--output-schema", schema_path, "-o", out_path, "-"]


def codex_runner(cfg: Config, prompt: str, schema: dict) -> dict:
    exe = cfg.codex_cmd or shutil.which("codex") or shutil.which("codex.cmd")
    if not exe:
        raise InvestError("未找到 codex 命令行")
    with tempfile.TemporaryDirectory() as work:
        schema_path = os.path.join(work, "schema.json")
        out_path = os.path.join(work, "answer.json")
        with open(schema_path, "w", encoding="utf-8") as fh:
            json.dump(schema, fh)
        try:
            proc = subprocess.run(
                codex_command(exe, cfg.codex_model, work, schema_path, out_path),
                input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=cfg.codex_timeout_s,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise InvestError(f"codex 超过 {cfg.codex_timeout_s} 秒") from exc
        if proc.returncode != 0 or not os.path.exists(out_path):
            raise InvestError(f"codex 退出码 {proc.returncode}：{_tail(proc.stderr)}")
        with open(out_path, encoding="utf-8") as fh:
            return json.loads(fh.read())


def review(cfg: Config, scan: dict, runner: Runner = codex_runner, today: date | None = None) -> dict:
    result = runner(cfg, build_prompt(scan, today or datetime.now().date()), output_schema())
    _validate_review(result, scan)
    return result


def _validate_review(result: dict, scan: dict) -> None:
    """Check semantic guarantees which JSON Schema alone cannot express."""
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise InvestError("codex 返回的格式不对")
    if not isinstance(result.get("has_new_opportunity"), bool) or not isinstance(result.get("summary"), str):
        raise InvestError("codex 返回缺少摘要或机会标记")
    if not isinstance(result.get("gaps"), list) or not all(isinstance(g, str) for g in result["gaps"]):
        raise InvestError("codex 返回的 gaps 格式不对")
    aliases = {
        str(inst.get(key) or "").lower(): str(inst.get("id") or "").lower()
        for inst in scan.get("instruments") or [] for key in ("id", "label")
        if inst.get(key)
    }
    expected_ids = {str(inst.get("id") or "").lower() for inst in scan.get("instruments") or []} - {""}
    seen: set[str] = set()
    for item in result["items"]:
        if not isinstance(item, dict):
            raise InvestError("codex 返回了无效标的项")
        name = item.get("instrument")
        canonical = aliases.get(name.lower()) if isinstance(name, str) else None
        if not canonical or canonical in seen:
            raise InvestError("codex 返回的标的缺失、重复或不属于本次扫描")
        seen.add(canonical)
        if item.get("verdict") not in VERDICTS:
            raise InvestError("codex 返回了无效判断")
        if not all(isinstance(item.get(key), str) for key in ("headline", "evidence")):
            raise InvestError("codex 返回的标的说明格式不对")
        if not isinstance(item.get("conditions_hit"), list) or not all(isinstance(x, str) for x in item["conditions_hit"]):
            raise InvestError("codex 返回的条件格式不对")
        if item.get("next_check") is not None and not isinstance(item["next_check"], str):
            raise InvestError("codex 返回的下次检查格式不对")
        if not isinstance(item.get("sources"), list) or not all(isinstance(x, str) for x in item["sources"]):
            raise InvestError("codex 返回的来源格式不对")
    if seen != expected_ids:
        raise InvestError("codex 没有覆盖本次扫描的全部标的")


def _validate_scan(scan: dict) -> None:
    if not isinstance(scan, dict) or scan.get("schema") != "daily-scan/1":
        raise InvestError("JSON 不是 daily-scan/1")
    instruments = scan.get("instruments")
    if not isinstance(instruments, list) or not all(isinstance(inst, dict) for inst in instruments):
        raise InvestError("JSON 缺少有效的 instruments")
    ids = [inst.get("id") for inst in instruments]
    if not all(isinstance(value, str) and value for value in ids) or len(set(ids)) != len(ids):
        raise InvestError("JSON 的 instrument id 无效或重复")


# ------------------------------------------------------------------ render
def _num(v: Any, fmt: str) -> str:
    return format(v, fmt) if isinstance(v, (int, float)) else "—"


def _row(inst: dict, item: dict | None) -> str:
    c = inst.get("conclusion") or {}
    q = inst.get("quote") or {}
    stance = f"{STANCES.get(c['stance'], c['stance'])}（{str(c.get('researched_at') or '')[:10]}）" if c.get("stance") else "无结论"
    price = _num(q.get("price"), ",.4f" if inst.get("asset_class") == "fx" else ",.2f")
    if inst.get("change_since_conclusion_pct") is not None:
        price += f" / {_num(inst['change_since_conclusion_pct'], '+.1f')}%"
    if not inst.get("ok"):
        verdict, point = "重跑失败", inst.get("error") or ""
    elif item:
        verdict, point = VERDICTS.get(item.get("verdict"), "—"), item.get("headline") or ""
    else:
        verdict, point = "—", ""
    cells = [inst.get("label") or inst.get("id") or "?", stance, price, verdict, point]
    return "| " + " | ".join(_cell(str(x)) for x in cells) + " |"


def _find(items: list[dict], inst: dict) -> dict | None:
    keys = {str(inst.get(k) or "").lower() for k in ("id", "label")} - {""}
    return next((i for i in items if str(i.get("instrument") or "").lower() in keys), None)


def _gaps(result: dict | None, scan: dict) -> list[str]:
    gaps = [str(g) for g in (result or {}).get("gaps") or []]
    for inst in scan.get("instruments") or []:
        name = inst.get("label") or inst.get("id")
        c = inst.get("conclusion") or {}
        age = inst.get("conclusion_age_hours")
        if not inst.get("ok"):
            gaps.append(f"{name}：重跑失败（{inst.get('error') or '未知原因'}）")
        elif c and (
            (age or 0) > STALE_HOURS
            or c.get("status") not in (None, "reviewed")
            or c.get("review_status") not in (None, "reviewed")
        ):
            status = c.get("status") or c.get("review_status") or "未知"
            gaps.append(f"{name}：原结论已过期或需复核（{_num(age and age / 24, '.0f')} 天，{status}）")
        elif not c:
            gaps.append(f"{name}：没有已保存的研究结论")
    return gaps


def render_section(
    result: dict | None, scan: dict, when: datetime, model: str = "gpt-6.1-sol", notes: list[str] | None = None
) -> list[str]:
    lines = [f"## 投资机会（截至 {when:%H:%M}，Codex {model}）", ""]
    items = (result or {}).get("items") or []
    if result:
        flag = "有新机会" if result.get("has_new_opportunity") else "无新机会"
        lines += [f"**{flag}。** {result.get('summary') or ''}".rstrip(), ""]
    lines += [f"> {n}" for n in notes or []] + ([""] if notes else [])
    insts = scan.get("instruments") or []
    if insts:
        lines += ["| 标的 | 原结论 | 今日价格/较结论 | 判断 | 要点 |", "|---|---|---|---|---|"]
        lines += [_row(i, _find(items, i)) for i in insts]
        lines.append("")
    for it in items:
        if it.get("verdict") not in ("new_opportunity", "risk_up"):
            continue
        lines.append(f"- **{it.get('instrument')}（{VERDICTS[it['verdict']]}）**：{it.get('evidence') or it.get('headline')}")
        hit = [str(x) for x in it.get("conditions_hit") or []]
        if hit:
            lines.append(f"  - 触及条件：{'；'.join(hit)}")
        if it.get("next_check"):
            lines.append(f"  - 下次检查：{it['next_check']}")
        lines += [f"  - 来源：[{u}]({u})" for u in it.get("sources") or []]
    if any(i.get("verdict") in ("new_opportunity", "risk_up") for i in items):
        lines.append("")
    gaps = _gaps(result, scan)
    if gaps:
        lines += fold(f"缺口与失败（{len(gaps)} 项）", [f"- {g}" for g in gaps])
    return lines + [DISCLAIMER, ""]


# ----------------------------------------------------------------- section
def cache_path(cfg: Config, today: date) -> Path:
    return cfg.state_dir / "invest" / f"{today.isoformat()}.json"


def _read_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("scan"), dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _scan_time(scan: dict, fallback: datetime) -> datetime:
    """Use the scan's real as-of time, including when today's cache is reused."""
    raw = scan.get("generated_at")
    if not isinstance(raw, str):
        return fallback
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return fallback


def section(cfg: Config, today: date, *, force: bool = False, runner: Runner = codex_runner) -> list[str]:
    """The note section for today; never raises."""
    if not cfg.invest_enabled:
        log.info("invest skipped: disabled")
        return []
    if not cfg.invest_repo.is_dir():
        log.info("invest skipped: %s not found", cfg.invest_repo)
        return []
    now = datetime.now().astimezone()
    when = now
    head = f"## 投资机会（截至 {when:%H:%M}，Codex {cfg.codex_model}）"
    try:
        path = cache_path(cfg, today)
        cached = {} if force else _read_cache(path)
        scan = cached.get("scan")
        if scan is not None:
            try:
                _validate_scan(scan)
            except InvestError as exc:
                log.warning("invest cached scan invalid; rescanning: %s", exc)
                cached, scan = {}, None
        if scan is None:
            scan = run_scan(cfg)
            cached = {"scan": scan}
            _write_cache(path, cached)
        when = _scan_time(scan, now)
    except Exception as exc:  # noqa: BLE001
        log.warning("invest scan failed: %s", exc)
        return [head, "", f"> 投资研究重跑失败：{exc}", ""]
    result = cached.get("review")
    if result:
        try:
            _validate_review(result, scan)
        except InvestError as exc:
            log.warning("invest cached review invalid; retrying: %s", exc)
            result = None
            cached = {"scan": scan}
            try:
                _write_cache(path, cached)
            except OSError as write_exc:
                # Cache cleanup is optional.  A locked cache must not stop
                # the digest from retrying the review or writing the note.
                log.warning("invest cached review cleanup failed: %s", write_exc)
    if not result:
        try:
            result = review(cfg, scan, runner, today)
            _write_cache(path, {"scan": scan, "review": result})
        except Exception as exc:  # noqa: BLE001
            log.warning("invest review failed: %s", exc)
            return render_section(None, scan, when, cfg.codex_model, [f"Codex 判断失败：{exc}"])
    return render_section(result, scan, when, cfg.codex_model)
