"""Resolução de fontes: URL direta (HLS/DASH/FFmpeg) ou página com player (Playwright).

Nunca contorna DRM, login ou paywall. Não lê nem repassa cookies/credenciais.
"""
from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

from . import util
from .config import SourceCfg

log = util.log

DIRECT_EXT = (".m3u8", ".mpd", ".ts", ".mp4", ".flv", ".mkv", ".webm", ".m4v", ".mov")
DIRECT_SCHEMES = ("rtmp", "rtmps", "rtsp", "udp", "srt", "rtp", "tcp", "file")
ALLOWED_HEADERS = {"referer", "user-agent"}  # nada de cookie/authorization

DRM_MARKERS = [
    "com.apple.streamingkeydelivery",          # FairPlay
    "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",    # Widevine
    "9a04f079-9840-4286-ab92-e65be0885f95",    # PlayReady
    "com.microsoft.playready",
    "com.widevine",
    "94ce86fb-07ff-4f43-adb8-93d2fa968ca2",    # FairPlay (DASH)
]


class SourceError(Exception):
    """Fonte inacessível/inválida (mensagem pensada para o usuário)."""


class ProtectedSourceError(SourceError):
    """Fonte protegida por DRM — não suportada e não será contornada."""


@dataclass
class Resolved:
    url: str
    headers: dict = field(default_factory=dict)
    map_args: list = field(default_factory=lambda: ["-map", "0:v:0", "-map", "0:a:0?"])
    note: str = ""


def classify(url: str) -> str:
    u = url.strip()
    parsed = urlparse(u)
    if parsed.scheme in DIRECT_SCHEMES or (not parsed.scheme and Path(u).exists()):
        return "direct"
    path = parsed.path.lower()
    if path.endswith(DIRECT_EXT):
        return "direct"
    if re.search(r"\.(m3u8|mpd)(\?|$)", u.lower()):
        return "direct"
    return "page"


def _clean_headers(h: dict | None) -> dict:
    return {k: v for k, v in (h or {}).items() if k.lower() in ALLOWED_HEADERS}


def _fetch_text(url: str, headers: dict, timeout: float = 15, limit: int = 2_000_000) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 gravador/1.0", **_clean_headers(headers)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(limit).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SourceError(f"acesso negado ({e.code}): a fonte exige autenticação/permissão. "
                              f"Este programa não contorna proteções de acesso.")
        raise SourceError(f"servidor respondeu HTTP {e.code} ao abrir a fonte")
    except Exception as e:
        raise SourceError(f"não foi possível acessar a fonte: {e}")


def check_protection(text: str) -> None:
    low = text.lower()
    if "#extm3u" in low:
        for m in re.finditer(r"#ext-x-(?:session-)?key:([^\n]*)", low):
            attrs = m.group(1)
            if "method=none" in attrs:
                continue
            if any(mk in attrs for mk in DRM_MARKERS) or "keyformat" in attrs and "identity" not in attrs:
                raise ProtectedSourceError("fonte protegida por DRM (HLS com FairPlay/Widevine/PlayReady). "
                                           "Não é possível gravar e o programa não tenta contornar.")
    if "<mpd" in low and "contentprotection" in low:
        if any(mk in low for mk in DRM_MARKERS) or "urn:mpeg:dash:mp4protection" in low:
            raise ProtectedSourceError("fonte protegida por DRM (DASH ContentProtection). "
                                       "Não é possível gravar e o programa não tenta contornar.")


def parse_quality(q: str):
    q = (q or "best").strip().lower()
    if q in ("best", "melhor", ""):
        return ("best", None)
    if q in ("worst", "pior"):
        return ("worst", None)
    m = re.search(r"(\d{3,4})", q)
    return ("height", int(m.group(1))) if m else ("best", None)


def pick_hls_variant(master_text: str, base_url: str, quality: str) -> tuple[str, str]:
    """Escolhe a variante de um master playlist. Retorna (url, descrição)."""
    variants = []
    lines = master_text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF:"):
            bw = re.search(r"BANDWIDTH=(\d+)", ln)
            res = re.search(r"RESOLUTION=(\d+)x(\d+)", ln)
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].startswith("#")):
                j += 1
            if j < len(lines):
                variants.append({"bw": int(bw.group(1)) if bw else 0,
                                 "h": int(res.group(2)) if res else 0,
                                 "w": int(res.group(1)) if res else 0,
                                 "url": urljoin(base_url, lines[j].strip())})
    if not variants:
        return base_url, ""
    mode, h = parse_quality(quality)
    key = lambda v: (v["h"], v["bw"])
    if mode == "best":
        v = max(variants, key=key)
    elif mode == "worst":
        v = min(variants, key=key)
    else:
        le = [x for x in variants if x["h"] and x["h"] <= h]
        v = max(le, key=key) if le else min(variants, key=key)
    return v["url"], f"variante {v['w']}x{v['h']} @{v['bw'] // 1000} kbps"


def _pick_dash_streams(url: str, quality: str) -> tuple[list, str]:
    info = util.ffprobe_json(url, timeout=30)
    if not info:
        return ["-map", "0:v:0", "-map", "0:a:0?"], ""
    vids = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    auds = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not vids:
        return ["-map", "0:v:0", "-map", "0:a:0?"], ""
    mode, h = parse_quality(quality)
    key = lambda s: (int(s.get("height", 0)), int(s.get("bit_rate", 0) or 0))
    if mode == "best":
        v = max(vids, key=key)
    elif mode == "worst":
        v = min(vids, key=key)
    else:
        le = [s for s in vids if int(s.get("height", 0)) <= h]
        v = max(le, key=key) if le else min(vids, key=key)
    args = ["-map", f"0:{v['index']}"]
    if auds:
        args += ["-map", f"0:{auds[0]['index']}"]
    return args, f"representação {v.get('width')}x{v.get('height')}"


def resolve_direct(url: str, headers: dict, quality: str) -> Resolved:
    headers = _clean_headers(headers)
    lower = url.lower().split("?")[0]
    if urlparse(url).scheme in ("http", "https"):
        if lower.endswith(".m3u8") or ".m3u8" in url.lower():
            text = _fetch_text(url, headers)
            if "#EXTM3U" not in text:
                raise SourceError("a URL .m3u8 não retornou uma playlist HLS válida")
            check_protection(text)
            if "#EXT-X-STREAM-INF" in text:
                new_url, desc = pick_hls_variant(text, url, quality)
                if new_url != url:
                    sub = _fetch_text(new_url, headers)
                    check_protection(sub)
                return Resolved(new_url, headers, note=desc)
            return Resolved(url, headers)
        if lower.endswith(".mpd") or ".mpd" in url.lower():
            text = _fetch_text(url, headers)
            if "<MPD" not in text:
                raise SourceError("a URL .mpd não retornou um manifesto DASH válido")
            check_protection(text)
            args, desc = _pick_dash_streams(url, quality)
            return Resolved(url, headers, map_args=args, note=desc)
    return Resolved(url, headers)


# ---------------------------------------------------------------- páginas (Playwright)

_DRM_HOOK = """
(() => {
  const o = navigator.requestMediaKeySystemAccess;
  if (o) navigator.requestMediaKeySystemAccess = function(ks, c) {
    window.__gravador_drm = ks; return o.apply(this, arguments);
  };
})();
"""
_PLAY_JS = "document.querySelectorAll('video').forEach(v=>{v.muted=true;try{v.play().catch(()=>{})}catch(e){}})"
_STREAM_RE = re.compile(r"\.(m3u8|mpd)(\?|$)", re.I)


def default_profile_dir() -> Path:
    return Path(os.environ.get("GRAVADOR_HOME", Path.home() / ".gravador")) / "browser_profile"


def _launch(p, profile_dir: Path, headless: bool):
    channel = os.environ.get("GRAVADOR_BROWSER_CHANNEL") or None  # ex.: "chrome" (tem codecs H.264)
    profile_dir.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(str(profile_dir), headless=headless, channel=channel,
                                                args=["--autoplay-policy=no-user-gesture-required"])


def resolve_page(source: SourceCfg, quality: str, profile_dir: Path | None = None,
                 timeout: float = 40, headless: bool = True) -> Resolved:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SourceError("Playwright não instalado: `pip install playwright` e `playwright install chromium`.")
    profile_dir = profile_dir or default_profile_dir()
    found: list[tuple[str, dict]] = []
    with sync_playwright() as p:
        try:
            ctx = _launch(p, profile_dir, headless)
        except Exception as e:
            raise SourceError(f"não foi possível abrir o navegador (rode `playwright install chromium`): {str(e)[:150]}")
        try:
            ctx.add_init_script(_DRM_HOOK)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            def on_req(req):
                if _STREAM_RE.search(req.url):
                    found.append((req.url, dict(req.headers)))
            ctx.on("request", on_req)
            try:
                page.goto(source.url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            except Exception as e:
                raise SourceError(f"falha ao carregar a página: {str(e)[:150]}")
            t0 = time.time()
            while time.time() - t0 < timeout and not found:
                for fr in page.frames:
                    try:
                        fr.evaluate(_PLAY_JS)
                    except Exception:
                        pass
                page.wait_for_timeout(1000)
            drm = None
            for fr in page.frames:
                try:
                    drm = drm or fr.evaluate("window.__gravador_drm || null")
                except Exception:
                    pass
            if drm:
                raise ProtectedSourceError("a página usa DRM (Encrypted Media Extensions). "
                                           "Não é possível gravar e o programa não tenta contornar.")
            if not found:
                raise SourceError("nenhum stream HLS/DASH em claro foi encontrado na página "
                                  "(pode exigir login manual — use o comando `login` — ou usar DRM/MSE).")
            url, req_headers = found[0]
            hdrs = {"Referer": req_headers.get("referer", source.url),
                    "User-Agent": req_headers.get("user-agent", "Mozilla/5.0")}
            note = "URL obtida da página"
        finally:
            ctx.close()
    r = resolve_direct(url, hdrs, quality)
    r.note = (note + "; " + r.note).strip("; ")
    return r


def login_interactive(url: str, profile_dir: Path | None = None) -> None:
    """Abre navegador visível para o usuário entrar manualmente. Fecha quando ele fechar a janela."""
    from playwright.sync_api import sync_playwright
    profile_dir = profile_dir or default_profile_dir()
    with sync_playwright() as p:
        ctx = _launch(p, profile_dir, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url)
        print("Faça o login manualmente na janela aberta. Feche a janela do navegador quando terminar.")
        try:
            while ctx.pages:
                time.sleep(1)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass


def resolve(source: SourceCfg, quality: str, profile_dir: Path | None = None) -> Resolved:
    kind = source.kind if source.kind != "auto" else classify(source.url)
    if kind == "direct":
        return resolve_direct(source.url, source.headers, quality)
    return resolve_page(source, quality, profile_dir)
