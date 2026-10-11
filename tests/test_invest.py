import json
import subprocess
from datetime import date, datetime, timedelta

import pytest

from ai_daily_digest import cli, invest
from ai_daily_digest.config import Config

TODAY = date(2026, 10, 10)

SCAN = {
    "schema": "daily-scan/1", "generated_at": "2026-10-10T01:00:00Z", "refreshed": True, "weeks": 2,
    "instruments": [
        {"id": "googl", "label": "Alphabet", "asset_class": "equity", "ok": True, "error": None,
         "quote": {"price": 190.5}, "change_since_conclusion_pct": 6.25, "events": [], "warnings": [],
         "conclusion": {"stance": "观望", "headline": "h", "researched_at": "2026-10-08T00:00:00Z",
                        "review_status": "reviewed"}, "conclusion_age_hours": 48.0},
        {"id": "audusd", "label": "澳元", "asset_class": "fx", "ok": False, "error": "行情超时",
         "quote": None, "change_since_conclusion_pct": None, "events": [], "warnings": [],
         "conclusion": None, "conclusion_age_hours": None},
        {"id": "unh", "label": "UNH", "asset_class": "equity", "ok": True, "error": None,
         "quote": {"price": 300.0}, "change_since_conclusion_pct": -1.0, "events": [], "warnings": [],
         "conclusion": {"stance": "谨慎", "headline": "h", "researched_at": "2026-09-01T00:00:00Z",
                        "review_status": "reviewed"}, "conclusion_age_hours": 900.0},
    ],
}
RESULT = {
    "has_new_opportunity": True, "summary": "Alphabet 出现新的研究机会。",
    "items": [
        {"instrument": "googl", "verdict": "new_opportunity", "headline": "云业务指引上调",
         "evidence": "财报后上调指引，股价较结论上涨 6.25%。", "conditions_hit": ["上调指引"],
         "next_check": "下周财报电话会", "sources": ["https://example.com/a"]},
        {"instrument": "UNH", "verdict": "no_change", "headline": "无新增证据", "evidence": "",
         "conditions_hit": [], "next_check": None, "sources": []},
        {"instrument": "audusd", "verdict": "insufficient_data", "headline": "行情超时", "evidence": "",
         "conditions_hit": [], "next_check": None, "sources": []},
    ],
    "gaps": ["未找到澳联储最新纪要"],
}


def make_cfg(tmp_path, **kw):
    repo = tmp_path / "investment-research"
    repo.mkdir(exist_ok=True)
    return Config(state_dir=tmp_path / "state", invest_repo=repo, **kw)


class Proc:
    def __init__(self, out, rc=0, err=""):
        self.stdout, self.returncode, self.stderr = out, rc, err


def fake_python(cfg):
    py = invest.invest_python(cfg)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("")


def test_run_scan_parses_json(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    fake_python(cfg)
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return Proc(json.dumps(SCAN), rc=1)  # all instruments failed still returns JSON

    monkeypatch.setattr(subprocess, "run", run)
    assert invest.run_scan(cfg)["schema"] == "daily-scan/1"
    assert seen["cmd"][1:] == ["-m", "market_research", "daily-scan", "--json"]
    assert seen["cwd"] == cfg.invest_repo and seen["env"]["PYTHONIOENCODING"] == "utf-8"


def test_run_scan_errors(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    with pytest.raises(invest.InvestError, match="Python"):
        invest.run_scan(cfg)
    fake_python(cfg)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Proc("oops", rc=2, err="x" * 1000 + "BOOM"))
    with pytest.raises(invest.InvestError) as exc:
        invest.run_scan(cfg)
    assert "BOOM" in str(exc.value) and len(str(exc.value)) < 500


def test_run_scan_rejects_an_unrecognised_document(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    fake_python(cfg)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Proc('{"instruments": []}'))
    with pytest.raises(invest.InvestError, match="daily-scan/1"):
        invest.run_scan(cfg)


def test_section_missing_repo_or_disabled(tmp_path):
    cfg = Config(state_dir=tmp_path / "s", invest_repo=tmp_path / "nope")
    assert invest.section(cfg, TODAY) == []
    assert invest.section(make_cfg(tmp_path, invest_enabled=False), TODAY) == []


def test_section_caches_and_reuses(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    calls = {"scan": 0, "codex": 0}

    def scan(c):
        calls["scan"] += 1
        return SCAN

    def runner(c, prompt, schema):
        calls["codex"] += 1
        return RESULT

    monkeypatch.setattr(invest, "run_scan", scan)
    first = invest.section(cfg, TODAY, runner=runner)
    second = invest.section(cfg, TODAY, runner=runner)
    assert calls == {"scan": 1, "codex": 1}
    assert first[1:] == second[1:] or first[2:] == second[2:]
    invest.section(cfg, TODAY, runner=runner, force=True)
    assert calls == {"scan": 2, "codex": 2}


def test_codex_failure_falls_back_and_is_not_cached(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(invest, "run_scan", lambda c: SCAN)

    def boom(c, p, s):
        raise invest.InvestError("codex 退出码 1")

    out = "\n".join(invest.section(cfg, TODAY, runner=boom))
    assert "Codex 判断失败：codex 退出码 1" in out and "| Alphabet |" in out
    cached = json.loads(invest.cache_path(cfg, TODAY).read_text(encoding="utf-8"))
    assert "review" not in cached and "scan" in cached
    # Next run reuses the scan but retries Codex.
    monkeypatch.setattr(invest, "run_scan", lambda c: pytest.fail("rescan"))
    assert "有新机会" in "\n".join(invest.section(cfg, TODAY, runner=lambda c, p, s: RESULT))
    assert "review" in json.loads(invest.cache_path(cfg, TODAY).read_text(encoding="utf-8"))


def test_scan_failure_section(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)

    def bad(c):
        raise invest.InvestError("退出码 1")

    monkeypatch.setattr(invest, "run_scan", bad)
    out = invest.section(cfg, TODAY, runner=lambda *a: RESULT)
    assert out[0].startswith("## 投资机会") and "> 投资研究重跑失败：退出码 1" in out
    assert not invest.cache_path(cfg, TODAY).exists()


def test_render_section():
    lines = invest.render_section(RESULT, SCAN, datetime(2026, 10, 10, 8, 5))
    text = "\n".join(lines)
    assert lines[0] == "## 投资机会（截至 08:05，Codex gpt-6.1-sol）"
    assert "**有新机会。**" in text
    assert "| 标的 | 原结论 | 今日价格/较结论 | 判断 | 要点 |" in text
    assert "| Alphabet | 观望（2026-10-08） | 190.50 / +6.2% | 新机会 | 云业务指引上调 |" in text
    assert "重跑失败" in text and "[https://example.com/a](https://example.com/a)" in text
    assert "> [!info]- 缺口与失败" in text and "UNH：原结论已过期或需复核" in text
    assert lines[-2] == "> 研究参考，不构成交易建议。"


def test_codex_command_line(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, codex_cmd="codex-bin")
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(RESULT, fh)
        return Proc("")

    monkeypatch.setattr(subprocess, "run", run)
    assert invest.review(cfg, SCAN, invest.codex_runner, TODAY) == RESULT
    cmd = seen["cmd"]
    assert cmd[0] == "codex-bin" and "--ephemeral" in cmd and "--search" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only" and cmd[cmd.index("-m") + 1] == "gpt-6.1-sol"
    assert "2026-10-10" in seen["input"]


def test_review_requires_every_scanned_instrument(tmp_path):
    cfg = make_cfg(tmp_path)
    incomplete = {**RESULT, "items": RESULT["items"][:-1]}
    with pytest.raises(invest.InvestError, match="全部标的"):
        invest.review(cfg, SCAN, lambda *args: incomplete, TODAY)


def test_cached_scan_uses_its_actual_as_of_time(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(invest, "run_scan", lambda c: SCAN)
    first = invest.section(cfg, TODAY, runner=lambda *args: RESULT)
    second = invest.section(cfg, TODAY, runner=lambda *args: pytest.fail("cached review expected"))
    assert first[0] == second[0]


def test_invalid_cached_review_is_replaced_without_breaking_the_section(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    path = invest.cache_path(cfg, TODAY)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"scan": SCAN, "review": {"items": []}}), encoding="utf-8")
    monkeypatch.setattr(invest, "run_scan", lambda c: pytest.fail("valid cached scan should be reused"))
    out = invest.section(cfg, TODAY, runner=lambda *args: RESULT)
    assert "有新机会" in "\n".join(out)
    assert json.loads(path.read_text(encoding="utf-8"))["review"] == RESULT


def test_invalid_cached_scan_is_replaced(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    path = invest.cache_path(cfg, TODAY)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"scan": {"schema": "wrong", "instruments": [1]}}), encoding="utf-8")
    monkeypatch.setattr(invest, "run_scan", lambda c: SCAN)
    assert "有新机会" in "\n".join(invest.section(cfg, TODAY, runner=lambda *args: RESULT))


def test_fx_price_keeps_precision():
    inst = {"id": "audusd", "asset_class": "fx", "quote": {"price": 0.65713}, "ok": True}
    assert "0.6571" in invest._row(inst, None)


def test_dynamic_conclusion_review_status_is_reported_as_a_gap():
    scan = json.loads(json.dumps(SCAN))
    scan["instruments"][0]["conclusion"]["status"] = "needs_review"
    assert any("needs_review" in gap for gap in invest._gaps(RESULT, scan))


def test_run_day_adds_section_only_for_yesterday(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, vault_dir=tmp_path / "vault")
    monkeypatch.setattr(cli, "collect", lambda c, w: [])
    monkeypatch.setattr(cli.worklog, "collect", lambda v, d: [])
    monkeypatch.setattr(cli, "dida_section", lambda c: [])
    monkeypatch.setattr(invest, "section", lambda c, t: ["## 投资机会（测试）", ""])
    yesterday = TODAY - timedelta(days=1)
    kw = dict(force=False, use_llm=True, dry_run=False, today=TODAY)
    assert cli.run_day(cfg, yesterday - timedelta(days=1), **kw) == "empty"
    assert cli.run_day(cfg, yesterday, **kw).startswith("written")
    text = cli.note_path(cfg, yesterday).read_text(encoding="utf-8")
    assert "## 投资机会（测试）" in text and "当天没有 AI 会话" in text
    no = cli.run_day(cfg, yesterday, force=True, use_llm=True, dry_run=False, today=TODAY, use_invest=False)
    assert no == "empty"


def test_run_day_skips_scan_when_note_exists(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, vault_dir=tmp_path / "vault")
    yesterday = TODAY - timedelta(days=1)
    cli.note_path(cfg, yesterday).parent.mkdir(parents=True)
    cli.note_path(cfg, yesterday).write_text("x", encoding="utf-8")

    def boom(c, t):
        raise AssertionError("scan must not run for a day that is already written")

    monkeypatch.setattr(invest, "section", boom)
    assert cli.run_day(cfg, yesterday, force=False, use_llm=True, dry_run=False, today=TODAY) == "skipped"


@pytest.mark.parametrize("command", ["week", "month"])
def test_period_cli_no_llm_passes_no_invest_to_daily_backfill(tmp_path, monkeypatch, command):
    cfg = make_cfg(tmp_path, vault_dir=tmp_path / "vault")
    seen = {}

    def run_period(*args, **kwargs):
        seen.update(kwargs)
        return "empty"

    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "run_period", run_period)
    monkeypatch.setattr(cli, "refresh_latest", lambda c: None)
    monkeypatch.setattr(cli.sys, "stdout", None)
    assert cli.main([command, "--no-llm"]) == 0
    assert seen["use_llm"] is False and seen["use_invest"] is False
