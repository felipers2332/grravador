"""Linha de comando: ui | record | test | recover | merge | login | check."""
from __future__ import annotations

import argparse
import json
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__, util
from .app import AppController
from .config import AppConfig, SourceCfg


def _app(args) -> AppController:
    return AppController(Path(args.config) if getattr(args, "config", None) else None)


def cmd_check(args) -> int:
    ok = True
    for b in ("ffmpeg", "ffprobe"):
        try:
            p = util.find_bin(b)
            print(f"[ok] {b}: {p}")
        except FileNotFoundError as e:
            ok = False
            print(f"[FALTA] {e}")
    try:
        import playwright  # noqa
        print("[ok] playwright instalado (necessário só para links de páginas)")
    except ImportError:
        print("[aviso] playwright ausente: links de página não funcionarão (links .m3u8/.mpd/FFmpeg funcionam)")
    return 0 if ok else 1


def cmd_ui(args) -> int:
    from .ui_server import serve
    app = _app(args)
    util.setup_logging(Path(app.config_path).parent / "gravador-ui.log", console=False)
    serve(app, args.port, not args.no_browser)
    return 0


def cmd_record(args) -> int:
    app = _app(args)
    if args.name:
        app.cfg.name = args.name
    if args.out:
        app.cfg.out_dir = args.out
    if args.quality:
        app.cfg.settings.quality = args.quality
    if args.segment:
        app.cfg.settings.segment_seconds = args.segment
    if args.set:
        kv = {}
        for item in args.set:
            k, _, v = item.partition("=")
            kv[k] = v
        app.cfg.settings.update(kv)
    if args.source:
        app.cfg.sources = [SourceCfg(url=u, label=("Principal" if i == 0 else f"Reserva {i}"))
                           for i, u in enumerate(args.source)]
    if not args.resume and not any(s.enabled for s in app.cfg.sources):
        print("Informe ao menos uma fonte com -s URL (ou cadastre no painel).", file=sys.stderr)
        return 2
    util.setup_logging(None, console=True)
    when = None
    if args.at:
        when = datetime.fromisoformat(args.at.replace(" ", "T")).isoformat()
    rec = app.start_recording(when, args.duration, args.resume)
    print(f"Gravando em: {rec.session.root}\nCtrl+C para parar e finalizar com segurança.")
    stop = {"n": 0}

    def on_sig(*_):
        stop["n"] += 1
        if stop["n"] == 1:
            print("\nParando… (Ctrl+C de novo força a saída; os segmentos já gravados ficam preservados)")
            rec.stop(wait=False)
        else:
            raise SystemExit(130)
    signal.signal(signal.SIGINT, on_sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_sig)
    last = 0.0
    while rec.running:
        time.sleep(0.5)
        if time.time() - last > 15:
            last = time.time()
            s = rec.snapshot()
            print(f"[{time.strftime('%H:%M:%S')}] {s['state']} | fonte: {s['active_source']} | "
                  f"{util.fmt_dur(s['recorded_seconds'])} | {util.fmt_bytes(s['bytes'])} | falhas: {s['failures']}")
    print(f"Finalizado. Relatório: {rec.session.root / 'relatorio.md'}")
    m = rec.merge_result or {}
    for line in m.get("messages", []):
        print(" -", line)
    return 0


def cmd_test(args) -> int:
    from . import probe
    cfg = AppConfig()
    if args.quality:
        cfg.settings.quality = args.quality
    util.setup_logging(None, console=False)
    res = probe.test_source(SourceCfg(url=args.url), cfg.settings)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


def cmd_recover(args) -> int:
    from . import recovery
    from .session import Session
    app = _app(args)
    util.setup_logging(None, console=True)
    paths = [Path(args.path)] if args.path else [s.root for s in recovery.scan(args.scan or app.cfg.out_dir)]
    if not paths:
        print("Nenhuma gravação incompleta encontrada.")
        return 0
    for p in paths:
        print(f"Recuperando {p} …")
        res = recovery.recover(Session.open(p), app.cfg.settings, args.normalize or None)
        print(f"  {res.get('valid')}/{res.get('segments_total')} segmentos válidos, {util.fmt_dur(res.get('total_seconds'))}; "
              f"saída: {', '.join(res.get('outputs') or ['(segmentos preservados)'])}")
        for m in res.get("messages", []):
            print("  -", m)
    return 0


def cmd_merge(args) -> int:
    from . import merge, report
    from .session import Session
    app = _app(args)
    s = Session.open(args.path)
    res = merge.finalize(s.root, s.meta.get("name", "gravacao"), app.cfg.settings, args.normalize or None)
    s.meta["merge"] = res
    s.save_meta()
    report.write(s, res)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


def cmd_login(args) -> int:
    from . import sources
    sources.login_interactive(args.url)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gravador", description="Gravador Inteligente de Transmissões Esportivas")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", help="arquivo de configuração (padrão: ~/.gravador/config.json)")
    sp = ap.add_subparsers(dest="cmd", required=True)

    sp.add_parser("check", help="verifica FFmpeg/Playwright").set_defaults(fn=cmd_check)
    p = sp.add_parser("ui", help="abre o painel web local")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_ui)

    p = sp.add_parser("record", help="grava pela linha de comando (sem painel)")
    p.add_argument("-n", "--name", help="nome da gravação (equipes, liga, data)")
    p.add_argument("-o", "--out", help="pasta de saída")
    p.add_argument("-s", "--source", action="append", help="URL da fonte; repita para reservas (ordem = prioridade)")
    p.add_argument("-q", "--quality", help="best | worst | 720 …")
    p.add_argument("--segment", type=int, help="duração dos segmentos em segundos")
    p.add_argument("--at", help="agendar início, ex.: '2026-11-02 19:00'")
    p.add_argument("--duration", type=float, help="parar após N segundos")
    p.add_argument("--resume", help="continuar uma gravação interrompida (pasta da sessão)")
    p.add_argument("--set", action="append", metavar="CHAVE=VALOR", help="qualquer configuração, ex.: cooldown_seconds=30")
    p.set_defaults(fn=cmd_record)

    p = sp.add_parser("test", help="testa um link (reproduz alguns segundos e valida)")
    p.add_argument("url")
    p.add_argument("-q", "--quality")
    p.set_defaults(fn=cmd_test)

    p = sp.add_parser("recover", help="recupera gravações interrompidas")
    p.add_argument("path", nargs="?", help="pasta da gravação (omita para procurar na pasta de saída)")
    p.add_argument("--scan", help="pasta onde procurar")
    p.add_argument("--normalize", action="store_true", help="converter a um formato comum se houver formatos diferentes")
    p.set_defaults(fn=cmd_recover)

    p = sp.add_parser("merge", help="(re)junta os segmentos de uma gravação")
    p.add_argument("path")
    p.add_argument("--normalize", action="store_true")
    p.set_defaults(fn=cmd_merge)

    p = sp.add_parser("login", help="abre o navegador para você entrar manualmente em um site")
    p.add_argument("url")
    p.set_defaults(fn=cmd_login)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
