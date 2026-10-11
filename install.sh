#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="RemnaDownDetector"
INSTALL_DIR="${INSTALL_DIR:-/opt/remnadown}"
REPO_URL="${REPO_URL:-}"
NONINTERACTIVE="${NONINTERACTIVE:-0}"
PROXY_MODE="${PROXY_MODE:-auto}"
APP_PORT="${APP_PORT:-8088}"

green='\033[0;32m'; yellow='\033[1;33m'; red='\033[0;31m'; cyan='\033[0;36m'; blue='\033[1;34m'; bold='\033[1m'; dim='\033[2m'; reset='\033[0m'
info(){ printf "%b[+]%b %s\n" "$green" "$reset" "$*"; }
warn(){ printf "%b[!]%b %s\n" "$yellow" "$reset" "$*"; }
die(){ printf "%b[ERROR]%b %s\n" "$red" "$reset" "$*" >&2; exit 1; }

rule(){ printf "%b%*s%b\n" "$dim" "${COLUMNS:-78}" '' "$reset" | tr ' ' '-'; }
banner(){
  printf "%b" "$cyan"
  cat <<'EOF'
██████╗ ███████╗███╗   ███╗███╗   ██╗ █████╗ ██████╗  ██████╗ ██╗    ██╗███╗   ██╗
██╔══██╗██╔════╝████╗ ████║████╗  ██║██╔══██╗██╔══██╗██╔═══██╗██║    ██║████╗  ██║
██████╔╝█████╗  ██╔████╔██║██╔██╗ ██║███████║██║  ██║██║   ██║██║ █╗ ██║██╔██╗ ██║
██╔══██╗██╔══╝  ██║╚██╔╝██║██║╚██╗██║██╔══██║██║  ██║██║   ██║██║███╗██║██║╚██╗██║
██║  ██║███████╗██║ ╚═╝ ██║██║ ╚████║██║  ██║██████╔╝╚██████╔╝╚███╔███╔╝██║ ╚████║
╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝╚═════╝  ╚═════╝  ╚══╝╚══╝ ╚═╝  ╚═══╝
EOF
  printf "%b%b       Remnawave node monitoring and DPI automation%b\n\n" "$reset" "$dim" "$reset"
}
section(){ printf "\n%b%s%b\n" "$bold$blue" "$1" "$reset"; rule; }
step(){ printf "\n%b[%s/6]%b %b%s%b\n" "$cyan" "$1" "$reset" "$bold" "$2" "$reset"; }

interactive_menu(){
  [[ "$NONINTERACTIVE" == 1 ]] && return
  section "Мастер установки"
  printf "  %b1)%b Установить или обновить RemnaDownDetector\n" "$green" "$reset"
  printf "  %b2)%b Показать требования\n" "$cyan" "$reset"
  printf "  %b0)%b Выйти\n\n" "$red" "$reset"
  local choice
  read -r -p "Выберите действие [1]: " choice </dev/tty
  case "${choice:-1}" in
    1) ;;
    2) printf "\nUbuntu/Debian, root-доступ, домен с A/AAAA-записью, Docker и токен Remnawave.\n"; exit 0;;
    0) exit 0;;
    *) die "Неизвестный пункт меню: $choice";;
  esac
}

usage(){ cat <<EOF
$APP_NAME installer
Usage: sudo ./install.sh [--repo URL] [--dir PATH] [--non-interactive]

For unattended installation set:
  DOMAIN, REMNAWAVE_URL, REMNAWAVE_API_TOKEN
Optional:
  ADMIN_USERNAME, ADMIN_PASSWORD, DPI_API_KEY, APP_PORT,
  PROXY_MODE=auto|system-caddy|bundled-caddy|local
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_URL="$2"; shift 2;;
    --dir) INSTALL_DIR="$2"; shift 2;;
    --non-interactive) NONINTERACTIVE=1; shift;;
    -h|--help) usage; exit 0;;
    *) die "Неизвестный аргумент: $1";;
  esac
done

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "Запустите установщик через sudo или от root."
command -v openssl >/dev/null || die "Требуется openssl."
if ! command -v curl >/dev/null; then
  apt-get update
  apt-get install -y curl ca-certificates
fi

prompt(){
  local variable="$1"
  local message="$2"
  local default="${3:-}"
  local secret="${4:-0}"
  local value="${!variable:-}"
  if [[ -z "$value" && "$NONINTERACTIVE" != 1 ]]; then
    if [[ "$secret" == 1 ]]; then
      read -r -s -p "$message${default:+ [$default]}: " value </dev/tty; echo >/dev/tty
    else
      read -r -p "$message${default:+ [$default]}: " value </dev/tty
    fi
  fi
  printf -v "$variable" '%s' "${value:-$default}"
}

install_docker(){
  if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then return; fi
  info "Устанавливаю Docker Engine и Compose..."
  command -v curl >/dev/null || { apt-get update; apt-get install -y curl ca-certificates; }
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
  docker compose version >/dev/null 2>&1 || die "Docker Compose plugin не установлен."
}

prepare_source(){
  local script_dir
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  if [[ -n "$REPO_URL" ]]; then
    command -v git >/dev/null || { apt-get update; apt-get install -y git; }
    if [[ -d "$INSTALL_DIR/.git" ]]; then
      info "Обновляю существующий Git checkout..."
      git -C "$INSTALL_DIR" pull --ff-only
    else
      [[ ! -e "$INSTALL_DIR" || -z "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]] || die "$INSTALL_DIR уже существует и не является Git checkout."
      git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    fi
  elif [[ "$script_dir" != "$INSTALL_DIR" ]]; then
    mkdir -p "$INSTALL_DIR"
    cp -a "$script_dir"/. "$INSTALL_DIR"/
  fi
  cd "$INSTALL_DIR"
  [[ -f compose.yaml && -f main.py && -f remnadown/__init__.py ]] || \
    die "Файлы проекта или пакет remnadown не найдены в $INSTALL_DIR."
}

configure(){
  section "Параметры RemnaDownDetector"
  printf "%bУкажите отдельный домен, по которому будет открываться эта панель.%b\n" "$dim" "$reset"
  prompt DOMAIN "Домен панели RemnaDown, без https:// (например, monitor.example.com)" ""
  printf "\n%bПодключение к существующей панели Remnawave%b\n" "$bold" "$reset"
  prompt REMNAWAVE_URL "URL панели Remnawave (например, https://panel.example.com)" ""
  prompt REMNAWAVE_API_TOKEN "API-токен Remnawave" "" 1
  prompt DPI_API_KEY "API-ключ DPI Checker (Enter, чтобы пропустить)" "" 1
  prompt ADMIN_USERNAME "Логин администратора" "admin"
  if [[ -z "${ADMIN_PASSWORD:-}" ]]; then ADMIN_PASSWORD=$(openssl rand -hex 16); fi
  [[ -n "$DOMAIN" ]] || die "DOMAIN обязателен."
  [[ "$DOMAIN" != http://* && "$DOMAIN" != https://* ]] || die "DOMAIN указывается без протокола."
  [[ "$REMNAWAVE_URL" =~ ^https?:// ]] || die "REMNAWAVE_URL должен начинаться с http:// или https://."
  [[ -n "$REMNAWAVE_API_TOKEN" ]] || die "REMNAWAVE_API_TOKEN обязателен."

  local initial_port="$APP_PORT"
  while ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq ":${APP_PORT}$"; do
    APP_PORT=$((APP_PORT + 1))
    [[ "$APP_PORT" -le 8199 ]] || die "Не удалось найти свободный порт в диапазоне ${initial_port}-8199."
  done
  if [[ "$APP_PORT" != "$initial_port" ]]; then
    warn "Порт $initial_port занят; выбран свободный порт $APP_PORT."
  fi

  APP_SECRET_VALUE=$(openssl rand -hex 32)
}

write_env(){
  if [[ -f .env ]]; then
    cp .env ".env.backup.$(date +%Y%m%d%H%M%S)"
    warn "Существующий .env сохранён в резервную копию."
  fi
  cat >.env <<EOF
DOMAIN=$DOMAIN
APP_PORT=$APP_PORT
APP_SECRET=$APP_SECRET_VALUE
ADMIN_USERNAME=$ADMIN_USERNAME
ADMIN_PASSWORD=$ADMIN_PASSWORD
REMNAWAVE_URL=$REMNAWAVE_URL
REMNAWAVE_API_TOKEN=$REMNAWAVE_API_TOKEN
REMNAWAVE_CADDY_TOKEN=${REMNAWAVE_CADDY_TOKEN:-}
DPI_API_KEY=$DPI_API_KEY
DPI_API_URL=https://dpichecker.st/api/v1
DATABASE_PATH=/data/remnadown.db
POLL_INTERVAL_SECONDS=60
DEFAULT_DROP_PERCENT=45
BASELINE_SAMPLES=12
CONFIRM_SAMPLES=2
RECOVERY_SAMPLES=2
HISTORY_DAYS=30
PANEL_TIMEZONE=${PANEL_TIMEZONE:-Europe/Moscow}
IP_ALLOWLIST=${IP_ALLOWLIST:-}
TRUSTED_PROXY_IPS=172.16.0.0/12,127.0.0.1,::1
SECURE_COOKIES=true
WEBHOOK_ALLOW_PRIVATE_IPS=false
WEBHOOK_TIMEOUT_SECONDS=10
EOF
  chmod 600 .env
}

choose_proxy(){
  if [[ "$PROXY_MODE" == auto ]]; then
    local detected
    if systemctl is-active --quiet caddy 2>/dev/null; then detected=system-caddy
    elif ss -ltn 2>/dev/null | grep -qE ':(80|443)[[:space:]]'; then detected=local
    else detected=bundled-caddy
    fi
    if [[ "$NONINTERACTIVE" == 1 ]]; then
      PROXY_MODE=$detected
    else
      section "Публикация панели"
      printf "Обнаружен рекомендуемый режим: %b%s%b\n\n" "$green" "$detected" "$reset"
      printf "  1) Встроенный Caddy — автоматически получить HTTPS\n"
      printf "  2) Системный Caddy — добавить сайт в /etc/caddy/Caddyfile\n"
      printf "  3) Только локальный порт — настроить proxy самостоятельно\n\n"
      local proxy_choice default_choice=1
      [[ "$detected" == system-caddy ]] && default_choice=2
      [[ "$detected" == local ]] && default_choice=3
      read -r -p "Выберите режим [$default_choice]: " proxy_choice </dev/tty
      case "${proxy_choice:-$default_choice}" in
        1) PROXY_MODE=bundled-caddy;;
        2) PROXY_MODE=system-caddy;;
        3) PROXY_MODE=local;;
        *) die "Неизвестный режим публикации.";;
      esac
    fi
  fi
  case "$PROXY_MODE" in
    system-caddy|bundled-caddy|local) ;;
    *) die "Неизвестный PROXY_MODE=$PROXY_MODE";;
  esac
  info "Режим reverse proxy: $PROXY_MODE"
}

confirm_install(){
  [[ "$NONINTERACTIVE" == 1 ]] && return
  section "Проверка параметров"
  printf "  Домен панели RemnaDown  : %b%s%b\n" "$bold" "$DOMAIN" "$reset"
  printf "  Панель Remnawave        : %s\n" "$REMNAWAVE_URL"
  printf "  Каталог установки       : %s\n" "$INSTALL_DIR"
  printf "  Локальный порт          : %s\n" "$APP_PORT"
  printf "  Reverse proxy           : %s\n" "$PROXY_MODE"
  printf "  DPI Checker             : %s\n" "$([[ -n "$DPI_API_KEY" ]] && printf 'подключён' || printf 'пропущен')"
  printf "\n"
  local answer
  read -r -p "Начать установку? [Y/n]: " answer </dev/tty
  [[ "${answer:-y}" =~ ^[YyДд]$ ]] || { warn "Установка отменена."; exit 0; }
}

configure_system_caddy(){
  local config=/etc/caddy/Caddyfile marker="# RemnaDownDetector: $DOMAIN"
  [[ -f "$config" ]] || die "Не найден $config."
  cp "$config" "$config.remnadown.bak"
  local escaped_domain=${DOMAIN//./\\.}
  if grep -Eq "^[[:space:]]*${escaped_domain}[[:space:]]*\\{" "$config"; then
    warn "Сайт $DOMAIN уже существует в Caddyfile; обновляю его локальный upstream."
    sed -i "/^[[:space:]]*${escaped_domain}[[:space:]]*{/,/^[[:space:]]*}/ s#reverse_proxy[[:space:]]\+127\.0\.0\.1:[0-9]\+#reverse_proxy 127.0.0.1:$APP_PORT#" "$config"
  elif ! grep -Fq "$marker" "$config"; then
    cat >>"$config" <<EOF

$marker
$DOMAIN {
    reverse_proxy 127.0.0.1:$APP_PORT
}
EOF
  fi
  if ! caddy validate --config "$config"; then
    cp "$config.remnadown.bak" "$config"
    die "Конфигурация Caddy не прошла проверку; исходный файл восстановлен."
  fi
  systemctl reload caddy
}

deploy(){
  info "Собираю Docker-образ..."
  docker compose build app updater
  docker compose up -d app updater
  if [[ "$PROXY_MODE" == bundled-caddy ]]; then docker compose --profile bundled-proxy up -d caddy; fi
  if [[ "$PROXY_MODE" == system-caddy ]]; then configure_system_caddy; fi
  info "Ожидаю запуска приложения..."
  for _ in {1..30}; do
    if curl -fsS "http://127.0.0.1:$APP_PORT/health" >/dev/null 2>&1; then return; fi
    sleep 2
  done
  docker compose logs --tail=100 app >&2
  die "Приложение не прошло healthcheck."
}

banner
interactive_menu
step 1 "Проверка окружения и Docker"
install_docker
step 2 "Подготовка файлов проекта"
prepare_source
step 3 "Настройка панели и интеграций"
configure
step 4 "Выбор способа публикации"
choose_proxy
step 5 "Подтверждение установки"
confirm_install
write_env
step 6 "Сборка и запуск контейнеров"
deploy

section "Установка завершена"
printf "%b%s успешно установлен.%b\n\n" "$green$bold" "$APP_NAME" "$reset"
printf "  URL    : %bhttps://%s%b\n  Логин  : %s\n  Пароль : %b%s%b\n" "$cyan" "$DOMAIN" "$reset" "$ADMIN_USERNAME" "$yellow" "$ADMIN_PASSWORD" "$reset"
printf "\n%bСохраните пароль: повторно он не выводится.%b\n" "$yellow" "$reset"
