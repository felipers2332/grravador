"""Configurações e cadastro de fontes."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass
class SourceCfg:
    url: str
    label: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:6])
    enabled: bool = True
    kind: str = "auto"          # auto | direct | page
    headers: dict = field(default_factory=dict)  # apenas Referer/User-Agent (nunca credenciais)

    def display(self) -> str:
        return self.label or self.url[:60]


@dataclass
class Settings:
    # --- gravação ---
    quality: str = "best"                 # best | worst | altura (ex. 720)
    segment_seconds: int = 60
    final_container: str = "mkv"          # mkv | mp4 | ts
    auto_merge: bool = True
    normalize_on_incompatible: bool = False
    require_audio: bool = True
    ffmpeg_rw_timeout_s: int = 20
    analyze_seconds: float = 3.0          # análise inicial da entrada (menor = troca mais rápida)

    # --- detecção ---
    poll_interval: float = 1.0
    startup_timeout_s: float = 25.0
    stall_data_seconds: float = 6.0
    stall_time_seconds: float = 8.0
    buffering_speed: float = 0.8
    buffering_warn_seconds: float = 15.0
    buffering_bad_seconds: float = 60.0
    bitrate_drop_ratio: float = 0.15
    bitrate_drop_seconds: float = 20.0
    freeze_detect: bool = True
    freeze_seconds: float = 45.0
    black_seconds: float = 45.0
    content_check_interval: float = 20.0
    freeze_action: str = "switch"         # switch | warn

    # --- troca ---
    suspect_grace_seconds: float = 6.0
    same_source_retries: int = 1
    validate_media_seconds: float = 4.0
    validate_timeout_s: float = 30.0
    motion_check: bool = True
    cooldown_seconds: float = 60.0
    all_down_retry_seconds: float = 10.0

    # --- retorno à principal ---
    return_to_primary: bool = True
    primary_probe_interval: float = 90.0
    primary_stable_seconds: float = 45.0
    max_return_attempts: int = 5

    # --- disco ---
    disk_warn_gb: float = 5.0
    disk_critical_gb: float = 1.0

    # --- notificações ---
    notify_desktop: bool = True
    notify_webhooks: list = field(default_factory=list)   # URLs (Discord/Slack/genérico)
    notify_command: str = ""                               # comando executado com GRAVADOR_MSG no ambiente
    notify_min_interval_s: float = 20.0

    def update(self, d: dict) -> None:
        names = {f.name: f for f in fields(self)}
        for k, v in d.items():
            if k in names:
                cur = getattr(self, k)
                try:
                    if isinstance(cur, bool):
                        v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "sim", "on")
                    elif isinstance(cur, int):
                        v = int(float(v))
                    elif isinstance(cur, float):
                        v = float(v)
                    elif isinstance(cur, list) and isinstance(v, str):
                        v = [x.strip() for x in v.split(",") if x.strip()]
                    setattr(self, k, v)
                except (TypeError, ValueError):
                    pass


@dataclass
class AppConfig:
    name: str = "Partida"
    out_dir: str = str(Path.home() / "Gravacoes")
    sources: list = field(default_factory=list)   # lista de SourceCfg (dict ao salvar)
    settings: Settings = field(default_factory=Settings)

    @staticmethod
    def load(path: Path) -> "AppConfig":
        cfg = AppConfig()
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            cfg.name = d.get("name", cfg.name)
            cfg.out_dir = d.get("out_dir", cfg.out_dir)
            cfg.settings.update(d.get("settings", {}))
            cfg.sources = [SourceCfg(**s) for s in d.get("sources", [])]
        return cfg

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        d = {"name": self.name, "out_dir": self.out_dir,
             "settings": asdict(self.settings),
             "sources": [asdict(s) for s in self.sources]}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
