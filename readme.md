# rbmc\_ntrip\_optimized

Este repositório contém implementações de *Gateway* e Caster NTRIP otimizados para distribuição de pacotes RTCM em tempo real, utilizados na pesquisa de TCC para o MBA em Engenharia de Software na USP-Esalq.

## 🚀 Funcionalidades

### Gateway NTRIP (`client_ntrip_test.py`)

* Lê sentenças NMEA GGA de um receptor GNSS *via porta serial*.
* Realiza handshake NTRIP enviando GGA ao servidor.
* Suporte a **mountpoint via rota** (`GET /MOUNTPOINT`): se `--mount` for definido, envia requisição NTRIP padrão ao caster para seleção de base fixa.
* Parse de **HTTP chunked** para extrair somente bytes RTCM3.
* Validação de **CRC-24Q** (`Crc24LteA`) em cada frame RTCM3.
* **Deduplicação de frames RTCM por tipo de mensagem**: em caso de burst (lag de rede), mantém apenas o frame mais recente de cada tipo de mensagem antes de enviar ao receptor GNSS — reduz latência e evita sobrecarga do receptor.
* Retransmissão de RTCM limpo para o receptor GNSS via serial.
* **Reenvio periódico de GGA** (a cada 60s) para manter o canal ativo.
* **Reconexão automática** com **backoff exponencial** (2s → 30s) em caso de falha.
* **TCP Keepalive** configurado via `SIO_KEEPALIVE_VALS` no Windows (probe a cada 10s) para detecção rápida de conexões mortas.
* **Timeout de conexão** (10s) para evitar travamento em rede indisponível.
* **Timeout de leitura** (30s) para detectar conexões "half-open" sem resposta de dados.
* **Timestamps** (`HH:MM:SS`) em todas as mensagens de log para diagnóstico em campo.
* **Estatísticas de sessão**: número da sessão, bytes recebidos e duração.
* **Argumentos CLI** para configurar porta serial, porta do caster, prefixo de log e mountpoint.

### Caster NTRIP (`caster_ntrip.py`)

* **Modo Otimizado (porta 2102)** — `[Caster-OPT]`:
  * Aguarda conexão de receptor GNSS e recebe primeira mensagem GGA.
  * Determina as estações RBMC mais próximas via `services.base_priorization_service`.
  * Conecta ao servidor RBMC NTRIP usando `services.get_rtcm.NtripClient`.
  * Encaminha pacotes RTCM ao receptor com headers NTRIP (`ICY 200 OK`).
  * **Fallback** automático entre base principal e auxiliar.
  * Reavalia proximidade a cada nova GGA, trocando a base se necessário.
  * **Socket de escuta persistente** (`SO_REUSEADDR`): não recria o socket entre sessões, eliminando conflitos de porta e acelerando reconexões.
  * **TCP Keepalive** + **timeout de envio** (10s) para detectar e encerrar sessões com cliente morto.

* **Modo Base Fixa (porta 2103)** — `[Caster-FIX]`:
  * Aceita conexão NTRIP padrão com mountpoint via rota (ex: `GET /SPAR0`).
  * Se nenhum mountpoint for especificado, seleciona a base mais próxima mas **nunca troca** durante a sessão ("nearest-fixo").
  * Encaminha pacotes RTCM da base fixa ao receptor.
  * **Socket de escuta persistente**: igual ao modo otimizado — sem conflito de porta em reconexões.
  * **TCP Keepalive** + **timeout de envio** para detectar cliente morto e encerrar a sessão.

* Ambos os modos executam em **threads separadas** no mesmo processo.

### Serviços

* `services/get_rtcm.py` — **NtripClient** (subclasse de `threading.Thread`):
  * **Autenticação Basic HTTP** (`Authorization: Basic <base64>`) com cabeçalho `Ntrip-Version: Ntrip/2.0`.
  * Buffer de dados RTCM com acesso **thread-safe** via `threading.Lock`.
  * Timeout de header (15s) e timeout de dados (30s).
  * **Backoff exponencial** (2s → 30s) na reconexão com o RBMC.
  * Fechamento seguro do socket em bloco `finally`.

* `services/base_priorization_service.py` — **IBGEEndpointClient**:
  * Consulta a sourcetable RBMC NTRIP para listar bases ativas.
  * **Retry automático** (3 tentativas com backoff 1s → 2s → 4s) para tolerância a falhas transitórias.
  * Ordena as bases por distância geodésica e retorna as 2 mais próximas.

### Simulações (`simulations/`)

* `simulations/simulate_disconnect.py` — **Simulador de desconexão de client**:
  * Conecta ao caster, faz handshake (GGA ou `GET /MOUNT`) e recebe RTCM por 5s.
  * Permite simular três cenários de desconexão para validar a robustez do caster:
    1. **Congelamento**: para de ler dados sem fechar o socket (enche buffer TCP).
    2. **Kill abrupto**: encerra o processo sem enviar FIN (simula crash/kill).
    3. **Desconexão limpa**: fecha o socket normalmente (envia FIN).
  * Suporte a `--port` e `--mount` via CLI.

---

## 🛡️ Robustez de Conectividade (4G)

O sistema foi projetado para operar em campo via **sinal 4G**, onde a conectividade é instável. As proteções implementadas incluem:

| Camada | Proteção | Descrição |
|--------|----------|-----------|
| **Gateway** | Timeout de conexão | 10s (evita travamento em rede indisponível) |
| **Gateway** | Timeout de leitura | 30s (detecta conexão "half-open" sem FIN) |
| **Gateway** | TCP Keepalive | Probes a cada 10s no Windows |
| **Gateway** | Backoff exponencial | 2s → 4s → 8s → ... → 30s max na reconexão |
| **Gateway** | Proteção de envio | `sendall` com captura de `BrokenPipeError` |
| **Gateway** | Deduplicação RTCM | Descarta frames stale em bursts, envia só o mais recente por tipo |
| **Caster** | Socket persistente | Não recria socket de escuta entre sessões (sem conflito de porta) |
| **Caster** | Timeout de envio | 10s para detectar client morto via `sendall` travado |
| **Caster** | Detecção de desconexão | Captura `OSError`/`BrokenPipeError` e reinicia sessão |
| **NtripClient** | Buffer thread-safe | `Lock` para acesso concorrente ao buffer RTCM |
| **NtripClient** | Autenticação Basic | `Authorization: Basic` com `Ntrip-Version: Ntrip/2.0` |
| **NtripClient** | Cleanup de socket | `finally` garante fechamento mesmo com erro |
| **IBGE API** | Retry com backoff | 3 tentativas (1s → 2s) tolerância a falhas transitórias |

---

## 📦 Pré-requisitos

* Python 3.8 ou superior
* Bibliotecas listadas em `requirements.txt`:

  ```text
  pyserial
  crccheck
  python-dotenv
  geopy
  requests
  ```

## ⚙️ Configuração

Copie `.env.example` para `.env` e configure as variáveis:

```env
RBMC_CASTER = 170.84.40.52
RBMC_PORT = 2101
RBMC_USER = seuUsuario
RBMC_PASS = suaSenha
IBGE_ENDPOINT_URL = http://170.84.40.52:2101/
LOCAL_NTRIP_PORT = 2102
LOCAL_NTRIP_PORT_FIXED = 2103
SERIAL_PORT_TEST = COM5
ORCH_HOST = 127.0.0.1
ORCH_PORT_TEST = 2102
```

---

## ⚙️ Uso

### Executar o Caster (servidor — ambos os modos)

```bash
python caster_ntrip.py
```

Inicia dois listeners simultâneos:
- **Porta 2102** — Caster otimizado (troca automática de base)
- **Porta 2103** — Caster de base fixa (mountpoint via rota)

### Executar o Gateway (em campo via 4G)

```bash
# Uso básico — conecta ao caster otimizado (porta padrão do .env)
python client_ntrip_test.py

# Modo otimizado explícito (troca automática de base)
python client_ntrip_test.py --serial COM5 --port 2102 --prefix OPT

# Modo base fixa (mountpoint fixo via rota NTRIP)
python client_ntrip_test.py --serial COM6 --port 2103 --prefix FIX --mount SPAR0
```

**Argumentos disponíveis:**

| Argumento    | Descrição                                                    | Padrão              |
|-------------|--------------------------------------------------------------|---------------------|
| `--serial`  | Porta serial do receptor GNSS (ex: COM5, COM6)               | `.env` SERIAL_PORT_TEST |
| `--port`    | Porta TCP do Caster (ex: 2102, 2103)                         | `.env` ORCH_PORT_TEST   |
| `--prefix`  | Prefixo do arquivo de log (ex: OPT, FIX)                     | vazio               |
| `--mount`   | Mountpoint NTRIP para base fixa (ex: SPAR0)                  | auto                |

**Informações exibidas na inicialização:**

```
============================================================
  Cliente NTRIP
  Serial : COM5
  Caster : 127.0.0.1:2102
  Mount  : (auto)
  Prefixo: OPT
  Timeouts: connect=10s, read=30s
  Backoff : 2s → 30s
============================================================
```

### Executar o Simulador de Desconexão

```bash
# Simula desconexão no caster otimizado
python simulations/simulate_disconnect.py --port 2102

# Simula desconexão no caster de base fixa
python simulations/simulate_disconnect.py --port 2103 --mount SPAR0
```

Após conectar e receber RTCM por 5s, apresenta um menu interativo:

```
==================================================
  Escolha o cenário de desconexão:
==================================================
  1. Congelamento (para de ler, conexão aberta)
  2. Kill abrupto (encerra sem fechar socket)
  3. Desconexão limpa (close TCP normal)
==================================================
```

---

## 🧪 Teste de Campo (Ensaio Comparativo)

Para comparar a acurácia entre seleção dinâmica e fixa de base:

```bash
# Terminal 1: Caster no servidor (ambas as portas)
python caster_ntrip.py

# Terminal 2: Receptor com otimização (troca automática de base)
python client_ntrip_test.py --serial COM5 --port 2102 --prefix OPT

# Terminal 3: Receptor com base fixa (ex: SPAR0)
python client_ntrip_test.py --serial COM6 --port 2103 --prefix FIX --mount SPAR0
```

Os logs são gerados em `logs/` com prefixos distintos:
- `logs/OPT_LOG<timestamp>.txt` — dados do receptor com otimização
- `logs/FIX_LOG<timestamp>.txt` — dados do receptor com base fixa

> **Dica de validação pré-campo:** Use `simulations/simulate_disconnect.py` para testar a resiliência do caster sem precisar de hardware GNSS — escolha o cenário "Congelamento" e observe o caster detectar o timeout de envio em até 10s.

---

## 📂 Estrutura de Diretórios

```
rbmc_ntrip_optmized/
├── caster_ntrip.py              # Caster dual-mode (otimizado + base fixa)
├── client_ntrip_test.py         # Gateway NTRIP com reconexão e deduplicação RTCM
├── services/
│   ├── get_rtcm.py              # NtripClient thread-safe com auth Basic e retry
│   └── base_priorization_service.py  # Consulta RBMC com retry e priorização geodésica
├── simulations/
│   └── simulate_disconnect.py   # Simulador de cenários de desconexão (3 modos)
├── logs/                        # Logs NMEA dos receptores (gerados em runtime)
├── requirements.txt             # Dependências Python
├── .env                         # Variáveis de ambiente (não versionado)
└── README.md                    # Este arquivo
```

---

## 📖 Como Funciona

1. **Gateway**:

   * Captura GGA do receptor GNSS via serial e envia ao Caster.
   * Se `--mount` for definido, envia `GET /MOUNTPOINT HTTP/1.1` para selecionar base fixa; caso contrário, envia apenas o GGA para o caster otimizado.
   * Recebe RTCM3 em chunks HTTP chunked, valida CRC-24Q e deduplica por tipo de mensagem antes de repassar ao GNSS.
   * Mantém GGA ativo via reenvio periódico (60s).
   * Reconecta automaticamente com backoff exponencial e TCP keepalive.
   * Registra estatísticas de cada sessão (número, bytes recebidos, duração).

2. **Caster**:

   * Mantém socket de escuta persistente (sem rebind entre sessões).
   * Processa GGA recebido, calcula estação RBMC mais próxima via IBGE API.
   * Conecta ao RBMC Caster com autenticação Basic, envia header NTRIP e replica RTCM.
   * Monitora novas GGAs para reavaliar e trocar de base sem interrupções.
   * Detecta cliente morto via timeout de envio (10s) e reinicia a sessão de forma limpa.

3. **Caster Base Fixa** (`caster_ntrip.py` — porta 2103):

   * Mantém socket de escuta persistente (sem rebind entre sessões).
   * Parseia requisição NTRIP do client (`GET /MOUNTPOINT`) para determinar a base.
   * Se nenhum mountpoint é especificado, seleciona a mais próxima e **nunca troca** ("nearest-fixo").
   * Detecta cliente morto via timeout de envio e encerra/recicla a sessão.

4. **NtripClient** (`services/get_rtcm.py`):

   * Thread daemon que mantém conexão contínua com o RBMC NTRIP.
   * Autentica via HTTP Basic e envia cabeçalho `Ntrip-Version: Ntrip/2.0`.
   * Armazena dados RTCM em buffer thread-safe; o caster consome via `get_data()`.

5. **IBGEEndpointClient** (`services/base_priorization_service.py`):

   * Consulta a sourcetable do RBMC NTRIP para listar bases ativas.
   * Calcula distância geodésica de cada base ao receptor e retorna as 2 mais próximas.
   * Retry automático com backoff para falhas de rede transitórias.
