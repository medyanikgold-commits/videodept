"""Учёт работы помощников видеоотдела и веб-панель, где это видно в реальном времени."""
import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

AGENTS = {
    "ideas":       {"emoji": "💡", "name": "Отдел идей",  "role": "Придумывает темы роликов"},
    "writer":      {"emoji": "✍️", "name": "Сценарист",   "role": "Пишет сценарии, заголовки и посты"},
    "voice":       {"emoji": "🎙", "name": "Диктор",      "role": "Озвучивает текст"},
    "artist":      {"emoji": "🎨", "name": "Художник",    "role": "Рисует картинки к сценам"},
    "transcriber": {"emoji": "📝", "name": "Стенограф",   "role": "Расшифровывает твою речь"},
    "editor":      {"emoji": "🎬", "name": "Монтажёр",    "role": "Режет, склеивает, делает субтитры"},
    "publisher":   {"emoji": "📢", "name": "Публикатор",  "role": "Выкладывает в Telegram и VK"},
}

_lock = threading.Lock()


class Activity:
    def __init__(self, path: Path, tz):
        self.path, self.tz = path, tz
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.agents = {k: {"busy": False, "task": "", "since": None, "today": 0, "total": 0}
                       for k in AGENTS}
        for k, v in data.get("agents", {}).items():
            if k in self.agents:
                self.agents[k].update(today=v.get("today", 0), total=v.get("total", 0))
        self.stats = data.get("stats", {"videos_today": 0, "videos_total": 0, "published": 0})
        self.day = data.get("day")
        self.log = data.get("log", [])[-100:]
        self.job = None

    def _now(self):
        return dt.datetime.now(self.tz)

    def _rollover(self):
        today = self._now().date().isoformat()
        if self.day != today:
            self.day = today
            self.stats["videos_today"] = 0
            for a in self.agents.values():
                a["today"] = 0

    def _event(self, agent, text):
        self.log.append({"t": self._now().strftime("%H:%M:%S"), "agent": agent, "text": text})
        self.log = self.log[-100:]

    def _finish_busy(self):
        for a in self.agents.values():
            if a["busy"]:
                a["busy"], a["task"], a["since"] = False, "", None
                a["today"] += 1
                a["total"] += 1

    def set_active(self, agent, task):
        """Помощник взялся за дело; предыдущий в цепочке закончил своё."""
        if agent not in self.agents:
            return
        with _lock:
            self._rollover()
            self._finish_busy()
            self.agents[agent].update(busy=True, task=task, since=self._now().isoformat())
            self._event(agent, task)
        self.save()

    def job_start(self, kind, title):
        with _lock:
            self.job = {"kind": kind, "title": title, "since": self._now().isoformat()}
            self._event(None, f"Новая задача: {kind} «{title}»")
        self.save()

    def job_end(self, ok=True, note="", video=False):
        with _lock:
            self._rollover()
            self._finish_busy()
            if self.job:
                self._event(None, ("✅ Готово: " if ok else "⚠️ Не получилось: ")
                            + f"{self.job['kind']} «{self.job['title']}»" + (f" ({note})" if note else ""))
            if ok and video:
                self.stats["videos_today"] += 1
                self.stats["videos_total"] += 1
            self.job = None
        self.save()

    def published(self, n=1):
        with _lock:
            self.stats["published"] = self.stats.get("published", 0) + n
        self.save()

    def save(self):
        with _lock:
            data = {"agents": {k: {"today": v["today"], "total": v["total"]}
                               for k, v in self.agents.items()},
                    "stats": self.stats, "day": self.day, "log": self.log}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def snapshot(self, extra):
        with _lock:
            self._rollover()
            agents = [{**AGENTS[k], "id": k, **v} for k, v in self.agents.items()]
            return json.dumps({"agents": agents, "job": self.job, "stats": self.stats,
                               "log": list(reversed(self.log[-40:])),
                               "now": self._now().isoformat(), **extra}, ensure_ascii=False)


def serve_panel(activity: Activity, key: str, port: int, extra_fn):
    """Панель: http://сервер:порт/?key=... (только чтение)."""
    html = (Path(__file__).with_name("panel.html")).read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if parse_qs(url.query).get("key", [""])[0] != key:
                self._send(403, "Нет доступа. Возьми ссылку командой /panel в боте.".encode(),
                           "text/plain; charset=utf-8")
            elif url.path == "/api/state":
                self._send(200, activity.snapshot(extra_fn()).encode(), "application/json")
            elif url.path == "/":
                self._send(200, html, "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
