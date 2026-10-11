"""Paths and tunables. Defaults fit this machine; override via config.toml at the repo root."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOME = Path.home()


@dataclass
class Config:
    claude_dir: Path = HOME / ".claude"
    codex_dir: Path = HOME / ".codex"
    agy_dir: Path = HOME / ".gemini" / "antigravity-cli"
    vault_dir: Path = Path(r"E:\GitHub\Obsidian\Project Banks")
    # Top-level vault folder; must stay outside "AI 工作留痕" and ".ai-worklog".
    digest_folder: str = "AI 日报"
    state_dir: Path = REPO_ROOT / "state"
    claude_cmd: str = str(HOME / "AppData" / "Roaming" / "npm" / "claude.cmd")
    model: str = "sonnet"
    llm_timeout_s: int = 900
    # Character budget of the material handed to the model in one call.
    bundle_budget: int = 120_000
    user_cap: int = 800
    reply_cap: int = 1500
    backfill_days: int = 7
    # Projects the user has shelved: reports still describe them but list no follow-ups.
    shelved_projects: list = field(default_factory=list)
    # 滴答清单. The client id/secret come from developer.dida365.com and live in config.toml.
    dida_client_id: str = ""
    dida_client_secret: str = ""
    dida_redirect_uri: str = "http://127.0.0.1:8765/callback"
    dida_due_days: int = 7
    # API priority -> quadrant of the app's matrix view (its default rules).
    dida_quadrants: dict = field(default_factory=lambda: {"5": 1, "3": 2, "1": 3, "0": 4})
    # Daily investment scan: re-run the sibling investment-research repo, then ask Codex what changed.
    invest_enabled: bool = True
    invest_repo: Path = REPO_ROOT.parent / "investment-research"
    # Empty: the repo's own .venv interpreter.
    invest_python: str = ""
    # Empty: `codex` / `codex.cmd` from PATH.
    codex_cmd: str = ""
    codex_model: str = "gpt-6.1-sol"
    invest_scan_timeout_s: int = 1200
    codex_timeout_s: int = 900


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    path = path or REPO_ROOT / "config.toml"
    if not path.exists():
        return cfg
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for f in fields(Config):
        if f.name in data:
            value = data[f.name]
            setattr(cfg, f.name, Path(value) if isinstance(getattr(cfg, f.name), Path) else value)
    return cfg
