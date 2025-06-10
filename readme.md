# rbmc\_ntrip\_optimized

Este repositório contém implementações de *Gateway* e Caster NTRIP otimizados para distribuição de pacotes RTCM em tempo real, utilizados na pesquisa de TCC para o MBA em Engenharia de Software na USP-Esalq.

## 🚀 Funcionalidades

* **Gateway NTRIP (client\_ntrip.py)**

  * Lê sentenças NMEA GGA de um receptor GNSS *via porta seri*al.
  * Realiza handshake NTRIP enviando GGA ao servidor.
  * Parse de **HTTP chunked** para extrair somente bytes RTCM3.
  * Validação de **CRC-24Q** em cada frame RTCM.
  * Retransmissão de RTCM limpo para o receptor GNSS.
  * **Reenvio periódico** de GGA para manter o canal ativo.
  * **Reconexão automática** ao servidor em caso de falha.

* **Caster NTRIP (caster\_ntrip.py)**

  * Aguarda conexão de receptor GNSS e recebe primeira mensagem GGA.
  * Determina as estações IBGE mais próximas via serviço `services.base_priorization_service`.
  * Conecta ao servidor RBMC NTRIP usando `services.get_rtcm.NtripClient`.
  * Encaminha pacotes RTCM ao receptor, com headers NTRIP (`ICY 200 OK`).
  * **Fallback** automático entre base principal e auxiliar.
  * Reavalia proximidade a cada nova GGA, trocando a base se necessário.

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

## ⚙️ Uso

### Executar o Gateway (cliente)

```bash
python client_ntrip.py
```

### Executar o Caster (servidor)

```bash
python caster_ntrip.py
```

## 📂 Estrutura de Diretórios

```
rbmc_ntrip_optmized/
├── caster_ntrip.py         # Lógica do NTRIP server com fallback de base
├── client_ntrip.py         # Lógica do NTRIP client/gateway para GNSS
├── services/              # Módulos: base_priorization_service, get_rtcm
├── requirements.txt       # Dependências Python
├── .env.example           # Inclusão das variáveis de ambiente
└── README.md              # Este arquivo
```

## 📖 Como Funciona

1. **Gateway**:

   * Captura GGA e envia ao Caster.
   * Recebe RTCM3 em chunks, valida CRC e repassa ao GNSS.
   * Mantém GGA ativo via reenvio periódico e reconecta automaticamente.

2. **Caster**:

   * Processa GGA recebido, calcula estação IBGE mais próxima.
   * Conecta ao RBMC Caster, envia header NTRIP e replica RTCM.
   * Monitora novas GGAs para reavaliar e trocar de base sem interrupções.

