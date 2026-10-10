"""Paths and tunables. Defaults fit this machine; override via config.toml at the repo root."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
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
