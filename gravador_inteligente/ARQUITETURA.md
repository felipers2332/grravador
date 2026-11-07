# Gravador Inteligente de Transmissões — Arquitetura

## 1. Visão geral

Python orquestra, FFmpeg captura. O FFmpeg roda como **processo filho** e nunca decodifica a transmissão principal (`-c copy`), então o consumo de CPU/memória é baixo e uma falha do Python não corrompe o que já foi escrito.

```
                       ┌──────────────────────────────────────────┐
  CLI / Painel web ──► │ AppController  (config.json, start/stop) │
                       └───────────────┬──────────────────────────┘
                                       ▼
                       ┌──────────────────────────────┐
                       │ Recorder (engine.py)         │  laço principal (1 thread)
                       │  • máquina de estados        │
                       │  • failover / retorno        │
                       │  • disco, eventos, finalizar │
                       └──┬─────────┬─────────┬───────┘
          resolve URL     │         │         │
  ┌────────────────┐      │  ┌──────▼──────┐  │  ┌───────────────┐
  │ sources.py     │◄─────┘  │ capture.py  │  └─►│ monitor.py    │
  │ direto/página  │         │ ffmpeg      │     │ sinais de     │
  │ HLS/DASH/DRM   │         │ segmentado  │────►│ saúde         │
  └────────────────┘         └──────┬──────┘     └───────────────┘
                                    ▼
                         session.py (pasta da partida)
                         segments/ events.jsonl session.json
                                    ▼
                  merge.py (juntar/normalizar)  →  report.py
                  notifier.py (log, desktop, webhook, comando)
```

| Responsabilidade | Módulo |
|---|---|
| Captura e segmentação | `capture.py` (1 processo FFmpeg por captura) |
| Resolução de links (direto, página, qualidade, DRM) | `sources.py` |
| Detecção de falhas | `monitor.py` |
| Troca automática, retorno, cooldown | `engine.py` |
| Preservação e recuperação | `session.py`, `merge.py` |
| Relatório e logs | `report.py`, `util.py` |
| Notificações | `notifier.py` |
| Interface | `ui_server.py` (painel web local) e `cli.py` |

## 2. Tratamento de cada tipo de link

| Tipo | Como é tratado |
|---|---|
| `.m3u8` (HLS) | Se for *master playlist*, o programa lê as variantes e escolhe pela qualidade (`best`, `worst` ou altura, ex. `720`). Playlist com DRM (FairPlay/Widevine/PlayReady) é **recusada** com mensagem clara. AES-128 com chave acessível é HLS comum e o FFmpeg trata. |
| `.mpd` (DASH) | `ffprobe` lista as representações; escolhe vídeo conforme a qualidade e o primeiro áudio. Manifesto com `ContentProtection` DRM é **recusado**. |
| URL compatível com FFmpeg (`.ts`, `.mp4`, `.flv`, `rtmp://`, `rtsp://`, `udp://`, `srt://`, arquivo local) | Passa direto ao FFmpeg. |
| Página com player | Playwright abre a página (perfil persistente), observa as requisições de rede, captura a URL do `.m3u8`/`.mpd` que o próprio player pediu e grava essa URL com FFmpeg. Detecta uso de EME/DRM e recusa. A URL é **re-resolvida a cada início de captura** (tokens expiram). |
| Login | Comando `login URL` abre o navegador visível para você entrar manualmente; o perfil fica no disco local. Nenhuma senha é lida ou guardada pelo programa. |

## 3. Detecção de falhas e prevenção de falsos alarmes

O sinal vem do **que o FFmpeg realmente está gravando** (`-progress`), não do estado da página.

| Sinal | Falha detectada |
|---|---|
| Processo FFmpeg | encerrou / travou |
| Bytes gravados (`total_size`) | sem novos dados por X s |
| Tempo de mídia (`out_time`) | não avança por X s (dados chegam, mas sem mídia útil) |
| Velocidade (`speed`) | < 0,8× por muito tempo → buffering prolongado |
| Taxa de dados em janela de 10 s vs. linha de base | queda forte e sustentada |
| `freezedetect` (só quadros-chave, barato) | vídeo congelado |
| `blackdetect` | tela preta persistente |
| stderr do FFmpeg | traduz 401/403/404/5xx/timeout em motivos legíveis |

**Contra falsos alarmes**
1. Dois estágios: limiar do sinal (ex. 6 s sem dados) **e depois** uma janela de tolerância configurável (`suspect_grace_seconds`) em que o problema precisa persistir.
2. Histerese: o problema só é "limpo" após alguns segundos seguidos saudáveis.
3. Combinação de sinais: amarelo (instabilidade) não troca de fonte sozinho; vermelho exige sinal duro persistente.
4. Congelamento/tela preta são **conteúdo**, não rede: intervalos de jogo podem ter imagem estática legítima. Por isso o limiar é longo (padrão 45 s) e a troca só ocorre se a reserva **mostrar movimento**; senão continua gravando e apenas avisa.
5. Se o processo morre, primeiro tenta religar a mesma fonte uma vez dentro da tolerância.

## 4. Gravação em segmentos e recuperação

- FFmpeg com `-c copy -f segment -segment_format mpegts -segment_time N`. **MPEG-TS não tem cabeçalho global**: um segmento cortado no meio continua reproduzível, ao contrário de MP4/MKV abertos.
- Cada captura nomeia seus arquivos `cNNN_<fonte>_NNNNN.ts` na pasta `segments/`; nada é sobrescrito (o número da captura sempre incrementa).
- `events.jsonl` é *append-only* com `fsync` por evento (início, falhas, trocas, lacunas). `session.json` é escrito de forma atômica (arquivo temporário + `os.replace`).
- Arquivo de trava com PID. Na próxima inicialização, uma sessão `recording` cujo PID não existe mais é uma **gravação incompleta**; o programa avisa e oferece `recover`, que reanalisa os segmentos, junta e gera o relatório.
- Em Linux, o FFmpeg filho recebe `PDEATHSIG`: se o Python morrer com `kill -9`, o FFmpeg é encerrado limpo e não fica órfão.
- **Junção final:** `ffprobe` em cada segmento → assinatura (codec, perfil, resolução, fps, áudio). Segmentos consecutivos com a mesma assinatura são concatenados com `-c copy`. Se há assinaturas diferentes: mantém as partes separadas + `ordem_reproducao.m3u`/`.txt`, ou, com `--normalize`, converte tudo a um formato comum (reencodifica).
- **Lacunas** são medidas (último dado da captura anterior → primeiro dado da seguinte) e registradas. Nada é preenchido artificialmente.
- Nenhum segmento válido é apagado. Só arquivos de 0 byte e a pasta de sondas de teste (`probe/`, conteúdo duplicado) são removidos.

## 5. Failover e retorno

**Falha:** vermelho persistente → `failure_detected` → candidatas em ordem de prioridade (a que falhou fica por último) → cada candidata inicia como **captura real** gravando em `segments/` e precisa provar: processo vivo, vídeo (e áudio, configurável), mídia avançando ≥ N s, bytes recebidos. Se valida, ela já é a gravação (sem reiniciar → menor lacuna). Se falha, tenta a próxima. Se todas falham: `all_down`, notifica e repete a cada `all_down_retry_seconds`.

**Retorno à principal:** só se `return_to_primary`; respeita `cooldown_seconds` desde a última troca; a sonda roda **em pasta de teste**, uma por vez, sem interromper a gravação ativa; precisa ficar saudável por `primary_stable_seconds` e mostrar movimento; no máximo `max_return_attempts` tentativas por episódio, com intervalo `primary_probe_interval`.

## 6. Limitações técnicas (sem rodeios)

- **DRM (Widevine, FairPlay, PlayReady)** não é gravável e não será contornado. Muitas transmissões de ligas (NHL.tv/ESPN+/etc.) usam DRM ou exigem cookies de sessão: nesses casos o programa informa o motivo e não grava.
- **Autenticação:** só funciona se a URL do stream for autossuficiente (token na URL) ou aberta. O programa **não copia cookies** do navegador para o FFmpeg; stream que dependa de cookie não é gravável por este método.
- **Páginas:** a captura de URL por observação de rede só funciona se o player usar HLS/DASH em claro e o site permitir. Players que usam MSE com segmentos blobificados, WebRTC ou DRM não são capturáveis.
- Tokens que expiram durante a partida: a re-resolução é feita a cada reinício de captura, mas a página precisa continuar acessível.
- A troca **sempre** gera alguns segundos de lacuna (iniciar + validar). Duas fontes costumam ter atrasos diferentes (uma pode estar 20 s à frente): pode haver trecho repetido ou faltante na junção; isso é registrado, não corrigido.
- Monitoramento de conteúdo (congelado/preto) tem custo de CPU pequeno (só quadros-chave) e depende do GOP da fonte.
- Gravar transmissões pode violar os termos do serviço ou direitos autorais; a responsabilidade pelo uso autorizado é do usuário.
