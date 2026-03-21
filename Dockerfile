# ── Build Stage ──────────────────────────────────────────────────────────────
FROM python:3.13.5-slim

# Metadados
LABEL maintainer="rbmc_ntrip_optimized"
LABEL description="Caster NTRIP otimizado — relay RTCM para receptores GNSS"

# Variáveis de ambiente para Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Diretório de trabalho
WORKDIR /app

# Instala dependências primeiro (aproveita cache de layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código da aplicação
COPY caster_ntrip.py .
COPY services/ ./services/

# Portas expostas pelo caster
# 2103 — Caster Base Fixa (mountpoint via rota) 
# 2104 — Caster Otimizado (troca automática de base)
# 2153 — Status server — Caster Base Fixa (base ativa)
# 2154 — Status server — Caster Otimizado (base ativa)
EXPOSE 2103 2104 2153 2154

# Comando de inicialização
CMD ["python", "-u", "caster_ntrip.py"]
