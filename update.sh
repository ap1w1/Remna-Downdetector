#!/usr/bin/env bash
set -Eeuo pipefail
INSTALL_DIR="${INSTALL_DIR:-/opt/remnadown}"
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "Запустите через sudo." >&2; exit 1; }
cd "$INSTALL_DIR"
[[ -f .env ]] || { echo "Не найден $INSTALL_DIR/.env" >&2; exit 1; }
if [[ -d .git ]]; then git pull --ff-only; else echo "Не Git checkout: загрузите новые файлы вручную." >&2; exit 1; fi
[[ -f remnadown/__init__.py ]] || { echo "Не найден пакет $INSTALL_DIR/remnadown" >&2; exit 1; }
remnadown_version="$(git rev-parse --short=7 HEAD)"
docker build --network=host --build-arg "REMNADOWN_VERSION=$remnadown_version" -t remnadown-app:latest .
docker compose up -d app
app_port=$(sed -n 's/^APP_PORT=//p' .env | tail -n1)
curl --retry 20 --retry-delay 2 --retry-connrefused -fsS "http://127.0.0.1:${app_port:-8088}/health"
echo "RemnaDownDetector обновлён."
