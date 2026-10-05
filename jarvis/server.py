"""JARVIS ローカルサーバー（127.0.0.1 限定・標準ライブラリのみ）。

* GET  /                 画面
* GET  /api/state        キャッシュ済みデータから判断結果を返す（外部取得しない）
* POST /api/sync         各ソースを読み取り取得して判断結果を返す（?source=todo,cash 等で限定可）
* POST /api/chat         {"message": "...", "budget": 180}

認証情報はサーバー側だけで扱い、レスポンスに含めない。書き込み系の外部APIは呼ばない。
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import chat, engine
from .config import JARVIS_DIR, Settings, load_settings
from .models import SourceResult, now_jst, redact
from .sync import FETCHERS, load_cache, run_sync, summary

logger = logging.getLogger("jarvis")
WEB_DIR = JARVIS_DIR / "web"
CLAUDE_MD = JARVIS_DIR.parent / "CLAUDE.md"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}

_sync_lock = threading.Lock()


def build_state(results: dict[str, SourceResult], settings: Settings, *, budget: int = 180,
                count: int = 5, today: date | None = None) -> dict[str, Any]:
    """画面・チャット共通の状態を組み立てる。"""
    today = today or now_jst().date()
    rec = engine.recommend(results, settings, today=today, budget=budget, count=count, claude_md=CLAUDE_MD)
    def data(name: str) -> dict[str, Any] | None:
        r = results.get(name)
        return r.data if r and r.data else None
    slack = data("slack") or {}
    return {
        "generatedAt": now_jst().isoformat(timespec="seconds"),
        "rec": rec,
        "cash": data("cash"),
        "flycka": data("flycka"),
        "slack": {k: slack.get(k) for k in ("items", "coverage", "tokenKind", "missingScopes",
                                            "channelsVisible", "channelsReadable")} if slack else None,
        "batch": data("batch"),
        "todoUrl": (data("todo") or {}).get("sheetUrl"),
        "sources": {s["name"]: s for s in summary(results)},
        "llm": {"connected": False, "note": "自由会話AIは未接続（ルール方式）"},
    }


class Handler(BaseHTTPRequestHandler):
    """HTTP ハンドラ。"""

    settings: Settings = load_settings()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        logger.info("%s %s", self.address_string(), redact(fmt % args))

    def _json(self, body: Any, status: int = 200) -> None:
        raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _params(self) -> tuple[int, int]:
        q = parse_qs(urlparse(self.path).query)
        budget = int(q.get("budget", ["180"])[0])
        count = int(q.get("count", ["5"])[0])
        return budget, count

    def _check_origin(self) -> bool:
        """他サイトからの POST（CSRF）を拒否する。"""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return origin is None or urlparse(origin).netloc == host

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/state":
                budget, count = self._params()
                self._json(build_state(load_cache(self.settings), self.settings, budget=budget, count=count))
                return
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            file = (WEB_DIR / rel).resolve()
            if WEB_DIR.resolve() not in file.parents or not file.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            raw = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(file.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            logger.exception("GET失敗")
            self._json({"error": redact(f"{type(e).__name__}: {e}")}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_origin():
            self._json({"error": "origin not allowed"}, 403)
            return
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(length, 20000)) or b"{}")
            if path == "/api/sync":
                q = parse_qs(urlparse(self.path).query)
                only = [s for s in (q.get("source", [""])[0].split(",")) if s in FETCHERS] or None
                if not _sync_lock.acquire(blocking=False):
                    self._json({"error": "同期中です。少し待ってください"}, 409)
                    return
                try:
                    results = run_sync(self.settings, only)
                finally:
                    _sync_lock.release()
                budget, count = self._params()
                self._json(build_state(results, self.settings, budget=budget, count=count))
            elif path == "/api/chat":
                msg = str(body.get("message", ""))
                default_budget = int(body.get("budget") or 180)
                cache = load_cache(self.settings)
                res = chat.answer(msg, lambda b: build_state(
                    cache, self.settings, budget=b if b is not None else default_budget))
                self._json(res)
            else:
                self._json({"error": "not found"}, 404)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            logger.exception("POST失敗")
            self._json({"error": redact(f"{type(e).__name__}: {e}")}, 500)


class LocalServer(ThreadingHTTPServer):
    """重複バインドを禁止したサーバー（Windows の SO_REUSEADDR で別プロセスと同じポートを
    共有し、要求が横取りされるのを防ぐ）。"""

    allow_reuse_address = False


def main(argv: list[str] | None = None) -> None:
    """サーバーを起動する。"""
    ap = argparse.ArgumentParser(description="JARVIS ローカルサーバー")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--sync", action="store_true", help="起動前に全ソースを同期する")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = load_settings()
    if args.sync:
        run_sync(settings)
    port = args.port or settings.port
    httpd = LocalServer((settings.host, port), Handler)
    logger.info("JARVIS: http://%s:%d/ （Ctrl+C で終了）", settings.host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
