"""Utilitários: logging, FFmpeg/ffprobe, disco, escrita atômica."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("gravador")


def setup_logging(log_file: Path | None = None, console: bool = True, level=logging.INFO) -> None:
    """Configura log legível com rotação. Pode ser chamado de novo (troca o arquivo)."""
    root = logging.getLogger("gravador")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def find_bin(name: str) -> str:
    """Localiza ffmpeg/ffprobe (variável GRAVADOR_FFMPEG / GRAVADOR_FFPROBE tem prioridade)."""
    env = os.environ.get("GRAVADOR_" + name.upper())
    if env:
        return env
    p = shutil.which(name)
    if not p:
        raise FileNotFoundError(f"'{name}' não encontrado no PATH. Instale o FFmpeg (veja o README).")
    return p


def free_bytes(path: Path | str) -> int:
    """Espaço livre em bytes. Os testes substituem esta função para simular disco cheio."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def ffprobe_json(path: str, timeout: float = 25, extra: list[str] | None = None) -> dict | None:
    cmd = [find_bin("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json"]
    cmd += extra or []
    cmd += [str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return None


def atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # processo zumbi conta como morto
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except Exception:
        return True


def child_preexec():
    """No Linux, faz o filho (ffmpeg) receber SIGTERM se o Python morrer (evita órfãos)."""
    if sys.platform.startswith("linux"):
        def _pre():
            try:
                import ctypes
                import signal
                ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
            except Exception:
                pass
        return _pre
    return None


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def fmt_dur(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(max(0, sec))
    return f"{sec // 3600:d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def fmt_bytes(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def slug(text: str, default: str = "gravacao") -> str:
    s = re.sub(r"[^\w\-. ]+", "", text, flags=re.UNICODE).strip().replace(" ", "_")
    return s[:100] or default


_FRIENDLY = [
    (r"\b401\b|Unauthorized", "acesso não autorizado (401): a fonte exige autenticação"),
    (r"\b403\b|Forbidden", "acesso negado (403): permissão/token inválido ou expirado"),
    (r"\b404\b|Not Found", "recurso não encontrado (404)"),
    (r"\b5\d\d\b.*(Server|Error)|Server returned 5\d\d", "erro do servidor (5xx)"),
    (r"Connection refused", "conexão recusada"),
    (r"timed out|Connection timed out|timeout", "tempo esgotado (timeout)"),
    (r"Name or service not known|Failed to resolve|getaddrinfo", "falha de DNS / sem internet"),
    (r"Connection reset|Broken pipe|End of file|Input/output error", "conexão perdida/interrompida pelo servidor"),
    (r"Invalid data found", "dados inválidos no fluxo"),
    (r"Protocol not found|Unable to open resource", "não foi possível abrir o recurso"),
]


def friendly_error(lines) -> str | None:
    text = "\n".join(lines[-40:]) if lines else ""
    for pat, msg in _FRIENDLY:
        if re.search(pat, text, re.I):
            return msg
    return None
