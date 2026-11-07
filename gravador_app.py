"""Ponto de entrada do executável: duplo clique abre o painel; com argumentos funciona como a CLI."""
import sys
import traceback


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # console do Windows
        except Exception:
            pass


def run() -> int:
    _utf8()
    from gravador.cli import main
    argv = sys.argv[1:] or ["ui"]
    try:
        code = main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    except Exception:
        traceback.print_exc()
        code = 1
    if getattr(sys, "frozen", False) and code not in (0, None):
        try:
            input("\nOcorreu um erro. Pressione Enter para fechar...")
        except Exception:
            pass
    return code or 0


if __name__ == "__main__":
    sys.exit(run())
