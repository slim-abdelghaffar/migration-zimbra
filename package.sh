#!/usr/bin/env bash
# Construit un paquet d'installation hors-ligne : dist/nextstep-migration-<version>.tar.gz
# (code + java-libpst + dépendances Python pour Python 3.10 à 3.14, Linux x86_64)
set -euo pipefail
cd "$(dirname "$0")"
VERSION=$(.venv/bin/python -c "from app.brand import VERSION; print(VERSION)")
NAME=nextstep-migration-$VERSION
STAGE=work/package/$NAME
rm -rf work/package && mkdir -p "$STAGE/wheels" dist
for v in 3.10 3.11 3.12 3.13 3.14; do
  .venv/bin/pip download -q -d "$STAGE/wheels" -r requirements.txt --only-binary=:all: \
    --python-version "$v" --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 \
    --platform manylinux_2_28_x86_64 --implementation cp || echo "  (dépendances Python $v incomplètes)"
done
cp -r app tools vendor requirements.txt install.sh install-service.sh run.sh set-password.sh package.sh \
      migration.env.example README.md "$STAGE/"
rm -rf "$STAGE/tools/build" "$STAGE/app/__pycache__" "$STAGE"/app/static/brand/logo.* 2>/dev/null || true
tar -C work/package -czf "dist/$NAME.tar.gz" "$NAME"
rm -rf work/package
echo "Paquet : dist/$NAME.tar.gz ($(du -h "dist/$NAME.tar.gz" | cut -f1))"
