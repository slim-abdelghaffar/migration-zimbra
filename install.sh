#!/usr/bin/env bash
# Installation de NextStep Migration Suite (Ubuntu 22.04 / 24.04, ou WSL)
#   ./install.sh            installation / mise à jour (en ligne, ou hors-ligne si wheels/ et vendor/ sont fournis)
#   ./install.sh --reseau   idem, puis configuration de l'accès réseau (VM cliente)
set -euo pipefail
cd "$(dirname "$0")"

JAR=vendor/java-libpst-0.9.3.jar
JAR_URL=https://repo1.maven.org/maven2/com/pff/java-libpst/0.9.3/java-libpst-0.9.3.jar
JAR_SHA1=928a6698850cd89577d28201ff1ac443bb339d2b  # vérifié contre Maven Central
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✘\033[0m %s\n' "$*"; exit 1; }

echo "== Prérequis système"
MISSING=()
command -v python3 >/dev/null || MISSING+=(python3)
python3 -c 'import venv, ensurepip' 2>/dev/null || MISSING+=(python3-venv)
command -v javac >/dev/null || MISSING+=(openjdk-21-jdk-headless)
command -v openssl >/dev/null || MISSING+=(openssl)
if [ ${#MISSING[@]} -gt 0 ]; then
  warn "paquets manquants : ${MISSING[*]}"
  if command -v sudo >/dev/null && sudo -v; then
    sudo apt-get update -qq && sudo apt-get install -y "${MISSING[@]}" || die "installation apt impossible"
  else
    die "installez-les : sudo apt install ${MISSING[*]}"
  fi
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 minimum requis"
ok "python $(python3 -c 'import platform; print(platform.python_version())'), $(javac -version 2>&1)"

echo "== Environnement Python"
python3 -m venv .venv
if [ -d wheels ] && ls wheels/*.whl >/dev/null 2>&1; then
  .venv/bin/pip install -q --no-index --find-links wheels -r requirements.txt && ok "dépendances installées hors-ligne (wheels/)"
else
  .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt && ok "dépendances installées (PyPI)"
fi

echo "== java-libpst"
mkdir -p vendor
[ -f "$JAR" ] || curl -sfo "$JAR" "$JAR_URL" || die "téléchargement de java-libpst impossible (fournir $JAR)"
echo "$JAR_SHA1  $JAR" | sha1sum -c --quiet || { rm -f "$JAR"; die "SHA-1 invalide pour $JAR"; }
rm -rf tools/build && mkdir -p tools/build
javac -Xlint:none -cp "$JAR" -d tools/build tools/com/pff/PstExport.java tools/com/pff/PstIdentify.java
ok "extracteur compilé"

mkdir -p data logs work
chmod 700 data logs work
chmod +x run.sh set-password.sh install-service.sh package.sh 2>/dev/null || true
[ -f migration.env ] || cp migration.env.example migration.env

if [ "${1:-}" = "--reseau" ]; then
  echo "== Accès réseau"
  IPS=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' | grep -v ':' | paste -sd, -)
  read -rp "  Noms / IP autorisés pour ouvrir l'interface [$(hostname),$IPS] : " HOSTS
  HOSTS=${HOSTS:-$(hostname),$IPS}
  read -rp "  Port [8443] : " PORT; PORT=${PORT:-8443}
  mkdir -p data/tls
  if [ ! -f data/tls/cert.pem ]; then
    SAN=$(echo "$HOSTS" | tr ',' '\n' | awk '{ if ($0 ~ /^[0-9.]+$/) printf "IP:%s,", $0; else if ($0 != "") printf "DNS:%s,", $0 }' | sed 's/,$//')
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=$(hostname)" -addext "subjectAltName=$SAN" \
      -keyout data/tls/key.pem -out data/tls/cert.pem 2>/dev/null
    chmod 600 data/tls/key.pem
    ok "certificat HTTPS auto-signé créé (data/tls/cert.pem)"
  fi
  sed -i -e "s|^HOST=.*|HOST=0.0.0.0|" -e "s|^PORT=.*|PORT=$PORT|" \
         -e "s|^MIGRATION_ALLOWED_HOSTS=.*|MIGRATION_ALLOWED_HOSTS=$HOSTS|" \
         -e "s|^TLS_CERT=.*|TLS_CERT=$PWD/data/tls/cert.pem|" -e "s|^TLS_KEY=.*|TLS_KEY=$PWD/data/tls/key.pem|" migration.env
  ok "migration.env : https sur le port $PORT, hôtes autorisés : $HOSTS"
  [ -f data/access.json ] || ./set-password.sh
fi

echo
echo "Installation terminée."
echo "  Démarrer à la main :      ./run.sh"
echo "  Démarrage automatique :   ./install-service.sh"
. ./migration.env
if [ "${HOST:-127.0.0.1}" = "0.0.0.0" ]; then
  echo "  Ouvrir :                  https://$(echo "$MIGRATION_ALLOWED_HOSTS" | cut -d, -f1):${PORT}"
else
  echo "  Ouvrir :                  http://localhost:${PORT:-8000}"
fi
