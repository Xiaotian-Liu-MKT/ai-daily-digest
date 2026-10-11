"""A fixed entry note that always shows the newest daily digest."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .config import Config

_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_WEEK = re.compile(r"\d{4}-W\d{2}")
_MONTH = re.compile(r"\d{4}-\d{2}")
_EARLIER = 7


def note_path(cfg: Config) -> Path:
    return cfg.vault_dir / cfg.digest_folder / "最新日报.md"


def _stems(folder: Path, pattern: re.Pattern) -> list[str]:
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.md") if p.is_file() and pattern.fullmatch(p.stem))


def render(cfg: Config) -> str | None:
    root = cfg.vault_dir / cfg.digest_folder
    days = []  # (stem, vault-relative link target)
    for year in sorted(p for p in root.glob("*") if p.is_dir() and re.fullmatch(r"\d{4}", p.name)):
        days += [(stem, f"{cfg.digest_folder}/{year.name}/{stem}") for stem in _stems(year, _DAY)]
    if not days:
        return None
    days.sort()
    newest = days[-1]
    # Always path-qualified: Obsidian's Daily Notes plugin also makes YYYY-MM-DD notes, so a bare link is ambiguous.
    out = ["---", "type: ai-digest-latest", f"latest: {newest[0]}", "---", "",
           "# 最新日报", "", "> 本页由 ai-daily-digest 自动重写，请勿手改。", ""]
    earlier = [f"[[{target}|{stem[5:]}]]" for stem, target in reversed(days[-1 - _EARLIER:-1])]
    if earlier:
        out += ["**更早**：" + " · ".join(earlier), ""]
    reports = []
    weeks = _stems(root / "周报", _WEEK)
    if weeks:
        reports.append(f"**周报**：[[{cfg.digest_folder}/周报/{weeks[-1]}|{weeks[-1]}]]")
    months = _stems(root / "月报", _MONTH)
    if months:
        reports.append(f"**月报**：[[{cfg.digest_folder}/月报/{months[-1]}|{months[-1]}]]")
    if reports:
        out += [" · ".join(reports), ""]
    out += [f"![[{newest[1]}]]", ""]
    return "\n".join(out)


def refresh(cfg: Config) -> str:
    text = render(cfg)
    if text is None:
        return "empty"
    path = note_path(cfg)
    if path.exists() and path.read_bytes() == text.encode("utf-8"):
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return f"written {path}"
