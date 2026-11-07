"""Análise de compatibilidade e junção dos segmentos (sem recodificar quando possível)."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import util
from .capture import SEG_RE
from .config import Settings

MIN_VALID_SECONDS = 0.3


@dataclass
class SegInfo:
    path: str
    cid: int
    sid: str
    seq: int
    size: int
    duration: float = 0.0
    sig: tuple = ()
    ok: bool = False
    reason: str = ""
    has_audio: bool = False
    video: dict = field(default_factory=dict)


def list_segments(seg_dir: Path) -> list[Path]:
    items = []
    for p in Path(seg_dir).glob("c*.ts"):
        m = SEG_RE.match(p.name)
        if m:
            items.append((int(m.group(1)), int(m.group(3)), p))
    return [p for _, _, p in sorted(items)]


def _fps(s: str | None) -> float:
    try:
        a, b = (s or "0/1").split("/")
        return float(a) / float(b) if float(b) else 0.0
    except Exception:
        return 0.0


def probe_segment(path: Path) -> SegInfo:
    m = SEG_RE.match(path.name)
    cid, sid, seq = (int(m.group(1)), m.group(2), int(m.group(3))) if m else (0, "?", 0)
    si = SegInfo(str(path), cid, sid, seq, path.stat().st_size if path.exists() else 0)
    if si.size == 0:
        si.reason = "arquivo vazio"
        return si
    info = util.ffprobe_json(str(path))
    if not info:
        si.reason = "ilegível pelo ffprobe"
        return si
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    try:
        si.duration = float(info.get("format", {}).get("duration") or 0)
    except ValueError:
        si.duration = 0.0
    if not v:
        si.reason = "sem trilha de vídeo"
        return si
    if si.duration < MIN_VALID_SECONDS:
        si.reason = f"duração muito curta ({si.duration:.2f}s)"
        return si
    si.video = {"codec": v.get("codec_name"), "w": v.get("width"), "h": v.get("height"), "fps": _fps(v.get("avg_frame_rate"))}
    si.has_audio = a is not None
    si.sig = (v.get("codec_name"), v.get("profile"), v.get("width"), v.get("height"), round(si.video["fps"]),
              a.get("codec_name") if a else None, a.get("sample_rate") if a else None, a.get("channels") if a else None)
    si.ok = True
    return si


def sig_text(sig: tuple) -> str:
    if not sig:
        return "?"
    vc, prof, w, h, fps, ac, sr, ch = sig
    audio = f"{ac} {sr}Hz {ch}ch" if ac else "sem áudio"
    return f"{vc}{'/' + prof if prof else ''} {w}x{h} {fps}fps, {audio}"


def group_compatible(infos: list[SegInfo]) -> list[list[SegInfo]]:
    groups: list[list[SegInfo]] = []
    for si in infos:
        if groups and groups[-1][0].sig == si.sig:
            groups[-1].append(si)
        else:
            groups.append([si])
    return groups


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _write_concat(files: list[str], list_path: Path) -> None:
    with open(list_path, "w", encoding="utf-8") as f:
        for p in files:
            f.write("file '" + str(Path(p).resolve()).replace("'", "'\\''") + "'\n")


def _run(cmd: list[str], timeout: float = 7200) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode == 0, r.stderr.decode("utf-8", "replace")[-600:]
    except Exception as e:
        return False, str(e)


def concat_copy(files: list[str], out: Path, tmp_dir: Path) -> tuple[bool, str]:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    lp = tmp_dir / (out.stem + ".txt")
    _write_concat(files, lp)
    cmd = [util.find_bin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
           "-fflags", "+genpts", "-i", str(lp), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
    if out.suffix == ".mp4":
        cmd += ["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"]
    cmd += [str(out)]
    return _run(cmd)


def _check_output(path: Path, expected: float) -> tuple[bool, str]:
    info = util.ffprobe_json(str(path))
    if not info:
        return False, "arquivo final ilegível"
    try:
        dur = float(info["format"].get("duration") or 0)
    except Exception:
        dur = 0
    tol = max(2.0, expected * 0.03)
    if abs(dur - expected) > tol:
        return False, f"duração do arquivo final ({dur:.1f}s) difere da soma dos segmentos ({expected:.1f}s)"
    return True, f"{dur:.1f}s"


def normalize_group(files: list[str], target: dict, out: Path, tmp_dir: Path) -> tuple[bool, str]:
    """Recodifica um grupo para o formato comum (resolução/fps/codec/áudio)."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    joined = tmp_dir / (out.stem + "_src.ts")
    ok, err = concat_copy(files, joined.with_suffix(".mkv"), tmp_dir)
    if not ok:
        return False, err
    src = joined.with_suffix(".mkv")
    w, h, fps = target["w"], target["h"], target["fps"]
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}")
    has_audio = bool(util.ffprobe_json(str(src)) and any(
        s.get("codec_type") == "audio" for s in util.ffprobe_json(str(src)).get("streams", [])))
    cmd = [util.find_bin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0", "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest", "-f", "mpegts", str(out)]
    ok, err = _run(cmd)
    try:
        src.unlink()
    except OSError:
        pass
    return ok, err


def finalize(root: Path, name: str, st: Settings, normalize: bool | None = None) -> dict:
    """Analisa os segmentos e tenta gerar o arquivo final. Nunca apaga segmentos."""
    root = Path(root)
    seg_dir = root / "segments"
    tmp_dir = root / "tmp_merge"
    normalize = st.normalize_on_incompatible if normalize is None else normalize
    res = {"segments_total": 0, "valid": 0, "invalid": [], "groups": [], "outputs": [], "mode": "nenhum",
           "ok": False, "messages": [], "total_seconds": 0.0}
    files = list_segments(seg_dir)
    res["segments_total"] = len(files)
    infos = [probe_segment(f) for f in files]
    valid = [i for i in infos if i.ok]
    res["valid"] = len(valid)
    res["invalid"] = [{"file": Path(i.path).name, "reason": i.reason} for i in infos if not i.ok]
    res["total_seconds"] = round(sum(i.duration for i in valid), 2)
    if not valid:
        res["messages"].append("Nenhum segmento válido encontrado.")
        return res

    groups = group_compatible(valid)
    res["groups"] = [{"signature": sig_text(g[0].sig), "segments": len(g), "seconds": round(sum(s.duration for s in g), 2),
                      "first": Path(g[0].path).name, "last": Path(g[-1].path).name} for g in groups]

    # lista de ordem de reprodução (sempre gerada)
    with open(root / "ordem_reproducao.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for i in valid:
            f.write(f"#EXTINF:{i.duration:.2f},{Path(i.path).name}\n{Path(i.path).relative_to(root).as_posix()}\n")
    with open(root / "ordem_reproducao.txt", "w", encoding="utf-8") as f:
        for gi, g in enumerate(groups, 1):
            f.write(f"# Grupo {gi}: {sig_text(g[0].sig)}\n")
            for i in g:
                f.write(Path(i.path).name + "\n")

    ext = "." + st.final_container.lower().lstrip(".")
    base = root / util.slug(name)

    def try_concat(files_: list[str], out: Path, expected: float) -> tuple[bool, str, Path]:
        ok, err = concat_copy(files_, out, tmp_dir)
        if ok:
            ok, err = _check_output(out, expected)
        if not ok and out.suffix != ".mkv":  # fallback de contêiner
            out2 = out.with_suffix(".mkv")
            res["messages"].append(f"Contêiner {out.suffix} falhou ({err}); tentando .mkv")
            ok, err = concat_copy(files_, out2, tmp_dir)
            if ok:
                ok, err = _check_output(out2, expected)
            out = out2
        return ok, err, out

    if len(groups) == 1:
        out = _unique(base.with_suffix(ext))
        ok, msg, out = try_concat([i.path for i in valid], out, res["total_seconds"])
        if ok:
            res.update(ok=True, mode="único", outputs=[out.name])
            res["messages"].append(f"Segmentos unidos sem recodificar em {out.name} ({msg}).")
        else:
            res["messages"].append(f"Não foi possível unir os segmentos: {msg}. Segmentos mantidos em segments/.")
            res["mode"] = "segmentos"
        return res

    res["messages"].append(f"{len(groups)} grupos com formatos diferentes (codec/resolução/fps/áudio) — "
                           "não podem ser unidos diretamente sem recodificar.")
    if normalize:
        biggest = max(groups, key=lambda g: (g[0].video.get("w") or 0) * (g[0].video.get("h") or 0))
        target = {"w": (biggest[0].video["w"] // 2) * 2, "h": (biggest[0].video["h"] // 2) * 2,
                  "fps": max(1, min(60, int(round(max(g[0].video.get("fps") or 25 for g in groups)))))}
        tmp_dir.mkdir(parents=True, exist_ok=True)
        parts, allok = [], True
        for gi, g in enumerate(groups, 1):
            p = tmp_dir / f"norm_{gi:02d}.ts"
            ok, err = normalize_group([i.path for i in g], target, p, tmp_dir)
            if not ok:
                allok = False
                res["messages"].append(f"Falha ao normalizar o grupo {gi}: {err}")
                break
            parts.append(str(p))
        if allok:
            out = _unique(base.with_name(base.name + "_normalizado").with_suffix(ext))
            ok, msg, out = try_concat(parts, out, res["total_seconds"])
            if ok:
                res.update(ok=True, mode="normalizado", outputs=[out.name])
                res["messages"].append(f"Convertido para {target['w']}x{target['h']} {target['fps']}fps e unido em {out.name}.")
                return res
            res["messages"].append(f"Falha ao unir partes normalizadas: {msg}")
        # em falha da normalização, cai para partes separadas
    outs = []
    for gi, g in enumerate(groups, 1):
        out = _unique(base.with_name(f"{base.name}_parte{gi}").with_suffix(ext))
        ok, msg, out = try_concat([i.path for i in g], out, sum(s.duration for s in g))
        if ok:
            outs.append(out.name)
        else:
            res["messages"].append(f"Parte {gi} não pôde ser gerada ({msg}); segmentos preservados.")
    res.update(ok=bool(outs), mode="partes", outputs=outs)
    res["messages"].append("Partes separadas geradas; ordem em ordem_reproducao.m3u/.txt. "
                           "Use `merge --normalize` para converter tudo a um formato comum.")
    return res
