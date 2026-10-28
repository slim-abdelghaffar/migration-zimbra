#!/usr/bin/env bash
# Définit (ou change) le mot de passe d'accès à l'interface web
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m app.access
