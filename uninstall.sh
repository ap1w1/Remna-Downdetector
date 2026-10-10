#!/usr/bin/env bash
set -Eeuo pipefail
INSTALL_DIR="${INSTALL_DIR:-/opt/remnadown}"
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "Запустите через sudo." >&2; exit 1; }
cd "$INSTALL_DIR"
docker compose --profile bundled-proxy down --remove-orphans || true
if [[ "${PURGE_DATA:-0}" == 1 ]]; then docker volume rm remnadown_remnadown-data 2>/dev/null || true; fi
if [[ -f /etc/caddy/Caddyfile ]]; then
  sed -i '/# RemnaDownDetector:/,+3d' /etc/caddy/Caddyfile
  caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy || true
fi
echo "Контейнеры удалены. Каталог $INSTALL_DIR оставлен; удалите его вручную при необходимости."
