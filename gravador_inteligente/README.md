# 🏒 Gravador Inteligente de Transmissões Esportivas

Grava transmissões ao vivo (hóquei no gelo ou qualquer esporte) a partir de **várias fontes para a mesma partida**,
supervisiona o que está sendo gravado e **troca sozinho para a fonte reserva** quando a principal falha.

Leia também: [`ARQUITETURA.md`](ARQUITETURA.md) (como funciona e limitações técnicas).

## O que ele faz

- Fonte principal + quantas reservas quiser; ordem = prioridade (alterável a qualquer momento).
- Grava em **segmentos MPEG-TS** (padrão: 60 s) sem recodificar. Se tudo cair, o que já foi gravado está no disco.
- Detecta: processo morto, **dados parados mesmo com a conexão aberta**, mídia que não avança, buffering prolongado,
  queda forte de taxa, **vídeo congelado**, **tela preta**.
- Troca para a próxima fonte **só depois de validar** que ela entrega vídeo + áudio avançando.
- Volta para a principal quando ela estabiliza (com cooldown, tempo de estabilidade e limite de tentativas).
- Junta os segmentos num arquivo final (sem recodificar quando possível) e gera relatório + log.
- Se o programa morrer no meio, detecta a gravação incompleta na próxima vez e recupera.

## Instalação

Requisitos: **Python 3.10+** e **FFmpeg** (com `ffprobe`) no PATH.

```bash
# 1) FFmpeg
#   Linux (Debian/Ubuntu):  sudo apt install ffmpeg
#   macOS:                  brew install ffmpeg
#   Windows:                winget install Gyan.FFmpeg      (reabra o terminal depois)

# 2) (opcional) só para links de PÁGINAS com player; links .m3u8/.mpd/FFmpeg não precisam
pip install -r requirements.txt
playwright install chromium

# 3) verificar
python -m gravador check
```

O painel usa apenas a biblioteca padrão do Python (sem Tkinter, sem instalar nada além do FFmpeg).

## Uso rápido

### Painel (recomendado)
```bash
python -m gravador ui            # abre http://127.0.0.1:8765
```
1. Preencha o **nome** (ex.: `AHL Hershey x Hartford 2026-11-02`) e a **pasta**.
2. Cole o link da **principal** e dos **reservas** em *Adicionar fonte*. Use ↑/↓ para a prioridade.
3. Clique **Testar** em cada link antes de começar (reproduz alguns segundos e valida vídeo/áudio/movimento).
4. **Iniciar** (ou escolha um horário em *Agendar início*). **Parar** finaliza e junta os arquivos.

Cores: 🔵 gravando · 🟢 funcionando · 🟡 instável/buffering · 🔴 indisponível · ⚪ em espera/não testada.
O painel só aceita conexões de `127.0.0.1`, com token por execução.

### Linha de comando (sem painel, bom para servidor)
```bash
python -m gravador record -n "NHL BOS x TOR 2026-11-02" -o ~/Gravacoes \
    -s "https://exemplo/principal.m3u8" -s "https://exemplo/reserva1.m3u8" -s "https://exemplo/reserva2.m3u8" \
    -q 720 --segment 60 --at "2026-11-02 19:00"
```
`--duration SEG` para parar sozinho · `--set chave=valor` para qualquer configuração (ex.: `--set cooldown_seconds=30`).

### Outros comandos
| Comando | Para quê |
|---|---|
| `python -m gravador test URL` | testa um link e mostra codec, resolução, taxa e o motivo se falhar |
| `python -m gravador recover [PASTA]` | recupera gravações interrompidas (sem argumento, procura na pasta de saída) |
| `python -m gravador record --resume PASTA ...` | continua uma gravação interrompida na mesma pasta |
| `python -m gravador merge PASTA [--normalize]` | refaz a junção; `--normalize` converte tudo para um formato comum |
| `python -m gravador login URL` | abre o navegador para você entrar **manualmente** em um site |

## Tipos de link

| Link | Suporte |
|---|---|
| `.m3u8` (HLS), inclusive master playlist com escolha de qualidade | ✅ |
| `.mpd` (DASH) com escolha de representação | ✅ (sem DRM) |
| `rtmp://`, `rtsp://`, `udp://`, `srt://`, `.ts`, `.mp4`, arquivo local | ✅ (qualquer coisa que o FFmpeg abra) |
| Página com player (descobre o `.m3u8`/`.mpd` pela rede) | ⚠️ depende do site; requer Playwright |
| Fonte com DRM (Widevine/FairPlay/PlayReady) | ❌ recusada com mensagem; **não** é contornada |
| Fonte que exige cookie de sessão para o stream | ❌ não suportado (cookies não são copiados para o FFmpeg) |

Para páginas com login: rode `python -m gravador login URL`, entre manualmente e feche a janela. A sessão fica no perfil
local do navegador (`~/.gravador/browser_profile`); o programa nunca lê nem guarda senhas.
Para o Chrome instalado (que toca H.264): `export GRAVADOR_BROWSER_CHANNEL=chrome`.

## O que fica na pasta de cada gravação

```
<pasta>/<nome>/
  <nome>.mkv               arquivo final (se os segmentos forem compatíveis)
  <nome>_parte1.mkv ...    partes separadas (se houver formatos diferentes)
  ordem_reproducao.m3u/.txt  ordem correta dos segmentos
  relatorio.md / .json     relatório (fontes, falhas, trocas, lacunas, junção)
  segments/                segmentos .ts (NUNCA apagados)
  events.jsonl             histórico de eventos (gravado com fsync)
  logs/gravador.log        log detalhado
  session.json             estado da gravação (usado na recuperação)
```

## Principais configurações

Todas no painel (*Configurações avançadas*) ou em `~/.gravador/config.json`.

| Chave | Padrão | O que faz |
|---|---|---|
| `segment_seconds` | 60 | duração de cada segmento (menor = mais seguro, mais arquivos) |
| `stall_data_seconds` | 6 | sem novos dados por tantos segundos = vermelho |
| `suspect_grace_seconds` | 6 | o problema precisa **persistir** isso tudo antes de trocar |
| `same_source_retries` | 1 | tentativas de religar a mesma fonte antes de trocar |
| `freeze_seconds` / `black_seconds` | 45 | imagem parada / preta por tanto tempo = problema de conteúdo |
| `freeze_action` | switch | `warn` só avisa (intervalos com imagem estática são normais em alguns esportes) |
| `return_to_primary` | true | voltar para a principal quando ela estabilizar |
| `primary_probe_interval` / `primary_stable_seconds` | 90 / 45 | de quanto em quanto tempo testa / quanto tempo precisa ficar estável |
| `cooldown_seconds` | 60 | intervalo mínimo entre retornos/trocas por conteúdo |
| `max_return_attempts` | 5 | tentativas de retorno por episódio |
| `require_audio` | true | só aceita fonte que entregue áudio e vídeo |
| `disk_warn_gb` / `disk_critical_gb` | 5 / 1 | avisa / encerra com segurança preservando tudo |
| `final_container` | mkv | `mkv`, `mp4` ou `ts` |
| `notify_webhooks`, `notify_command` | — | Discord/Slack/genérico e comando próprio (variável `GRAVADOR_MSG`) |

## Testes

```bash
pip install pytest
python -m pytest tests/test_unit.py -q            # rápidos (~1 s)
python -m pytest tests/test_scenarios.py -q       # cenários com transmissões simuladas (~8 min em série)
```
Os cenários usam um servidor local que gera transmissões ao vivo de verdade (MPEG-TS/HLS) e permite derrubar, travar,
congelar ou escurecer cada uma: transmissão normal, congelada, tela preta, que cai e volta (com retorno e sem
alternância), conexão travada, principal indisponível com reserva ok, todas indisponíveis, `kill -9` durante a gravação
(+ recuperação e retomada), disco cheio, codecs/resoluções diferentes (+ normalização), HLS com qualidade, DASH, página
com player e página com DRM. Dica: rode em vários terminais com `-k` para paralelizar.

## Limitações e avisos honestos

- **Toda troca gera uma lacuna** (iniciar + validar a nova fonte: ~4–8 s nos testes). Ela é medida e registrada, nunca disfarçada.
- Duas fontes costumam ter **atrasos diferentes**; na troca pode haver trecho repetido ou faltando. Não é corrigido.
- Intervalos/pré-jogo com imagem estática podem parecer "congelados": ajuste `freeze_seconds` ou use `freeze_action=warn`.
- Processos FFmpeg órfãos após `kill -9` são evitados no Linux (`PDEATHSIG`); no Windows/macOS use `recover` e feche
  eventuais `ffmpeg` restantes manualmente.
- Players de página com MSE/WebRTC/DRM não são capturáveis por rede.
- Os testes foram executados em Linux com FFmpeg 6.1. Windows/macOS usam o mesmo código, mas **não foram testados aqui**.
- Gravar transmissões pode violar os termos do serviço ou direitos autorais. Use apenas com o que você tem permissão para gravar.
