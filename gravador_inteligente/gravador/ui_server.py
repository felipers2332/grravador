"""Painel web local (somente 127.0.0.1, token por execução). Sem dependências externas."""
from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import util
from .app import AppController

HTML_PATH = Path(__file__).parent / "ui.html"


def make_handler(app: AppController, token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode())

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"host invalido", "text/plain")
            if self.path in ("/", "/index.html"):
                html = HTML_PATH.read_text(encoding="utf-8").replace("__TOKEN__", token)
                return self._send(200, html.encode(), "text/html")
            if self.path.startswith("/api/state"):
                if self.headers.get("X-Token") != token:
                    return self._json({"error": "token"}, 403)
                return self._json(app.state())
            self._send(404, b"nao encontrado", "text/plain")

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Token") != token:
                return self._json({"error": "acesso negado"}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "JSON inválido"}, 400)
            try:
                res = self.route(self.path, data)
                self._json({"ok": True, **(res or {})})
            except Exception as e:
                util.log.warning("API %s falhou: %s", self.path, e)
                self._json({"ok": False, "error": str(e)}, 400)

        def route(self, path: str, d: dict):
            if path == "/api/config":
                app.set_basic(d.get("name"), d.get("out_dir"), d.get("settings"))
            elif path == "/api/source/add":
                app.add_source(d.get("url", ""), d.get("label", ""))
            elif path == "/api/source/remove":
                app.remove_source(d["id"])
            elif path == "/api/source/move":
                app.move_source(d["id"], int(d["delta"]))
            elif path == "/api/source/toggle":
                app.toggle_source(d["id"], bool(d["enabled"]))
            elif path == "/api/source/test":
                app.test_source(d["id"])
            elif path == "/api/start":
                app.start_recording(d.get("schedule_at") or None)
            elif path == "/api/stop":
                app.stop_recording()
            elif path == "/api/recover":
                app.recover(d["path"], d.get("normalize"))
            else:
                raise ValueError("rota desconhecida")

    return H


def serve(app: AppController, port: int = 8765, open_browser: bool = True) -> None:
    token = secrets.token_urlsafe(16)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, token, port))
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"Painel em {url}  (Ctrl+C para sair)")
    inc = app.incomplete()
    if inc:
        print(f"ATENÇÃO: {len(inc)} gravação(ões) incompleta(s) detectada(s) — use o painel ou `recover`.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        r = app.recorder
        if r and r.running:
            print("Finalizando a gravação em andamento…")
            r.stop(wait=True)
        srv.server_close()
