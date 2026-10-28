#!/usr/bin/env bash
# Démarre l'interface NextStep Migration Suite (paramètres dans migration.env)
set -euo pipefail
cd "$(dirname "$0")"
if [ -f migration.env ]; then set -a; . ./migration.env; set +a; fi
HOST="${HOST:-127.0.0.1}"; PORT="${PORT:-8000}"
export MIGRATION_ALLOWED_HOSTS="${MIGRATION_ALLOWED_HOSTS:-}"
TLS=()
if [ -n "${TLS_CERT:-}" ] && [ -n "${TLS_KEY:-}" ]; then TLS=(--ssl-certfile "$TLS_CERT" --ssl-keyfile "$TLS_KEY"); fi
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" --proxy-headers "${TLS[@]}"
