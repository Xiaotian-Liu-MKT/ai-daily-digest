"""滴答清单 (Dida365) Open API: read upcoming tasks for the digest, add tasks on request.

Quadrants are not an API field. The app's matrix view derives them from rules,
by default from priority, which is the mapping used here (see Config.dida_quadrants).
"""
from __future__ import annotations

import base64
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from .config import Config

AUTHORIZE_URL = "https://dida365.com/oauth/authorize"
TOKEN_URL = "https://dida365.com/oauth/token"
API = "https://api.dida365.com/open/v1"
SCOPE = "tasks:read tasks:write"
QUADRANT_NAMES = {1: "重要且紧急", 2: "重要不紧急", 3: "不重要但紧急", 4: "不重要不紧急"}


class DidaError(RuntimeError):
    pass


class NotConfigured(DidaError):
    pass


@dataclass
class DueTask:
    title: str
    project: str
    due: date
    quadrant: int
    all_day: bool
    due_time: str = ""


def _token_path(cfg: Config):
    return cfg.state_dir / "dida_token.json"


def _load_token(cfg: Config) -> str:
    path = _token_path(cfg)
    if not path.exists():
        raise NotConfigured("no Dida365 token; run `ai-daily-digest dida auth` first")
    return json.loads(path.read_text(encoding="utf-8"))["access_token"]


def _request(method: str, url: str, *, headers: dict, data: bytes | None = None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 401:
            raise NotConfigured("Dida365 token rejected; run `ai-daily-digest dida auth` again") from exc
        raise DidaError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        # ``resp.read()`` can time out after urlopen has returned, so this
        # must cover the whole request rather than only connection errors.
        detail = getattr(exc, "reason", None) or str(exc) or "timed out"
        raise DidaError(f"{method} {url} failed: {detail}") from exc
    return json.loads(body) if body.strip() else None


def _api(cfg: Config, method: str, path: str, payload: dict | None = None):
    headers = {"Authorization": f"Bearer {_load_token(cfg)}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _request(method, API + path, headers=headers, data=data)


def authorize(cfg: Config) -> None:
    """One-off browser login; stores the access token under state/."""
    if not cfg.dida_client_id or not cfg.dida_client_secret:
        raise NotConfigured("set dida_client_id and dida_client_secret in config.toml first")
    redirect = urllib.parse.urlparse(cfg.dida_redirect_uri)
    state = secrets.token_urlsafe(16)
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            received.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("授权完成，可以关闭这个页面。".encode("utf-8"))

        def log_message(self, *args):
            pass

    server = HTTPServer((redirect.hostname or "127.0.0.1", redirect.port or 80), Handler)
    query = urllib.parse.urlencode({
        "client_id": cfg.dida_client_id, "scope": SCOPE, "state": state,
        "redirect_uri": cfg.dida_redirect_uri, "response_type": "code",
    })
    url = f"{AUTHORIZE_URL}?{query}"
    print("在浏览器里登录并授权；没有自动打开的话，手动访问：\n" + url)
    webbrowser.open(url)
    server.timeout = 300
    while "code" not in received and "error" not in received:
        server.handle_request()
        if not received:
            raise DidaError("no authorization response within 5 minutes")
    server.server_close()
    if received.get("state") != state or "code" not in received:
        raise DidaError(f"authorization failed: {received}")

    basic = base64.b64encode(f"{cfg.dida_client_id}:{cfg.dida_client_secret}".encode()).decode()
    form = urllib.parse.urlencode({
        "code": received["code"], "grant_type": "authorization_code",
        "scope": SCOPE, "redirect_uri": cfg.dida_redirect_uri,
    }).encode()
    token = _request("POST", TOKEN_URL, data=form, headers={
        "Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded",
    })
    if not token or "access_token" not in token:
        raise DidaError(f"token exchange returned no access_token: {token}")
    token["obtained_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    _token_path(cfg).write_text(json.dumps(token), encoding="utf-8")


def parse_due(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except (TypeError, ValueError):
            continue
    return None


def quadrant_of(cfg: Config, priority) -> int:
    return cfg.dida_quadrants.get(str(priority or 0), 4)


def priority_of(cfg: Config, quadrant: int) -> int:
    for priority, q in cfg.dida_quadrants.items():
        if q == quadrant:
            return int(priority)
    raise DidaError(f"no priority maps to quadrant {quadrant}")


def projects(cfg: Config) -> list[dict]:
    listed = [p for p in _api(cfg, "GET", "/project") or [] if not p.get("closed") and p.get("kind", "TASK") == "TASK"]
    # The inbox is a project of its own that the listing leaves out.
    return [{"id": "inbox", "name": "收集箱"}] + listed


def due_tasks(cfg: Config, today: date, days: int | None = None) -> list[DueTask]:
    """Open tasks that are overdue or due within `days` of today."""
    horizon = today + timedelta(days=cfg.dida_due_days if days is None else days)
    found = []
    for project in projects(cfg):
        data = _api(cfg, "GET", f"/project/{project['id']}/data") or {}
        for task in data.get("tasks") or []:
            if task.get("status", 0) != 0 or task.get("kind", "TEXT") != "TEXT":
                continue
            due = parse_due(task.get("dueDate") or "")
            if due is None:
                continue
            local = due.astimezone()
            if local.date() > horizon:
                continue
            all_day = bool(task.get("isAllDay"))
            found.append(DueTask(
                title=task.get("title", ""), project=project["name"], due=local.date(),
                quadrant=quadrant_of(cfg, task.get("priority")), all_day=all_day,
                due_time="" if all_day else local.strftime("%H:%M"),
            ))
    return sorted(found, key=lambda t: (t.quadrant, t.due, t.due_time, t.title))


def add_task(
    cfg: Config, title: str, *, quadrant: int, due: date | None = None,
    due_time: time | None = None, project: str | None = None, note: str = "",
) -> dict:
    payload: dict = {"title": title, "priority": priority_of(cfg, quadrant)}
    if project:
        match = [p for p in projects(cfg) if p["name"] == project]
        if not match:
            raise DidaError(f"no open list named {project!r}")
        if match[0]["id"] != "inbox":
            payload["projectId"] = match[0]["id"]
    if note:
        payload["content"] = note
    if due:
        local = datetime.combine(due, due_time or time()).astimezone()
        stamp = local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
        payload.update(dueDate=stamp, startDate=stamp, isAllDay=due_time is None)
    return _api(cfg, "POST", "/task", payload)


def render_section(tasks: list[DueTask], today: date, days: int) -> list[str]:
    lines = [f"## 滴答清单：{days} 天内到期", ""]
    if not tasks:
        return lines + ["无", ""]
    for quadrant in sorted({t.quadrant for t in tasks}):
        lines += [f"### {QUADRANT_NAMES[quadrant]}", ""]
        for t in (t for t in tasks if t.quadrant == quadrant):
            delta = (t.due - today).days
            when = "今天" if delta == 0 else "明天" if delta == 1 else f"逾期 {-delta} 天" if delta < 0 else f"{delta} 天后"
            stamp = f"{t.due:%m-%d}" + (f" {t.due_time}" if t.due_time else "")
            lines.append(f"- {stamp}（{when}）{t.title} · {t.project}")
        lines.append("")
    return lines
