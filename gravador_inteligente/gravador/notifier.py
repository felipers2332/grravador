"""Notificações: log (sempre), desktop (melhor esforço), webhooks e comando externo."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

from . import util
from .config import Settings


class Notifier:
    def __init__(self, st: Settings):
        self.st = st
        self.history: deque = deque(maxlen=60)
        self._last: dict[str, float] = {}

    def notify(self, kind: str, title: str, body: str = "", key: str | None = None, force: bool = False) -> None:
        stamp = time.strftime("%H:%M:%S")
        text = f"[{stamp}] {title}" + (f" — {body}" if body else "")
        util.log.info("NOTIFICAÇÃO (%s): %s", kind, text)
        self.history.append({"t": time.time(), "kind": kind, "title": title, "body": body, "text": text})
        k = key or kind
        now = time.time()
        if not force and now - self._last.get(k, 0) < self.st.notify_min_interval_s:
            return
        self._last[k] = now
        threading.Thread(target=self._deliver, args=(title, body, text), daemon=True).start()

    def _deliver(self, title: str, body: str, text: str) -> None:
        st = self.st
        if st.notify_desktop:
            try:
                self._desktop(title, body or title)
            except Exception:
                pass
        for url in st.notify_webhooks or []:
            try:
                data = json.dumps({"content": text, "text": text}).encode()
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=8).read()
            except Exception as e:
                util.log.warning("webhook falhou: %s", e)
        if st.notify_command:
            try:
                subprocess.Popen(st.notify_command, shell=True, env={**os.environ, "GRAVADOR_MSG": text,
                                 "GRAVADOR_TITLE": title, "GRAVADOR_BODY": body},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                util.log.warning("comando de notificação falhou: %s", e)

    @staticmethod
    def _desktop(title: str, body: str) -> None:
        esc = lambda s: s.replace('"', "'").replace("\\", "")
        if sys.platform.startswith("linux"):
            subprocess.Popen(["notify-send", "-a", "Gravador", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", f'display notification "{esc(body)}" with title "{esc(title)}"'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "win32":
            ps = ("Add-Type -AssemblyName System.Windows.Forms;$n=New-Object System.Windows.Forms.NotifyIcon;"
                  "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                  f"$n.ShowBalloonTip(5000,'{esc(title)}','{esc(body)}','Info');Start-Sleep 6;$n.Dispose()")
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
