<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024" role="img" aria-label="Remnawave Downdetector icon">
  <defs>
    <radialGradient id="tile" cx="49%" cy="73%" r="83%" fx="49%" fy="73%">
      <stop offset="0" stop-color="#fafafa"/>
      <stop offset="0.48" stop-color="#e8eaec"/>
      <stop offset="1" stop-color="#aeb2b6"/>
    </radialGradient>
    <linearGradient id="dark" x1="0" y1="0" x2="0.95" y2="1">
      <stop offset="0" stop-color="#1b1b1b"/>
      <stop offset="1" stop-color="#292929"/>
    </linearGradient>
    <linearGradient id="alert" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#202020"/>
      <stop offset="0.25" stop-color="#242020"/>
      <stop offset="0.55" stop-color="#a01818"/>
      <stop offset="0.78" stop-color="#ff191b"/>
      <stop offset="1" stop-color="#ff641d"/>
    </linearGradient>
  </defs>
  <rect width="1024" height="1024" rx="235" fill="url(#tile)"/>
  <rect x="212" y="390" width="80" height="168" rx="40" fill="url(#dark)"/>
  <rect x="340" y="290" width="80" height="340" rx="40" fill="url(#dark)"/>
  <rect x="596" y="390" width="80" height="250" rx="40" fill="url(#dark)"/>
  <rect x="716" y="474" width="76" height="158" rx="38" fill="url(#dark)"/>
  <rect x="824" y="540" width="64" height="93" rx="32" fill="url(#dark)"/>
  <path d="M472 252 C472 230 490 212 512 212 C534 212 552 230 552 252 L552 678 C552 689 558 695 570 695 L591 695 C612 695 623 719 607 735 L531 814 C520 826 504 826 493 814 L417 735 C401 719 412 695 433 695 L454 695 C466 695 472 689 472 678 Z" fill="url(#alert)"/>
</svg>
<img width="1024" height="1024" alt="remnawave-downdetector" src="https://github.com/user-attachments/assets/a6b3a437-35b9-4740-99b3-0227dddc358b" />
# RemnaDownDetector

Детектор падения онлайна на нодах Remnawave 2.7.4 — latest с веб-интерфейсом, графиками, настраиваемыми вебхуками и проверками через DPI Checker.

## Возможности

- компактный интерфейс в стилистике Remnawave с цветными SVG-флагами, данными провайдера и локальным кэшем внешних иконок;
- интерактивные графики онлайна, CPU, RAM и RX/TX за 1, 3, 7, 14 или 30 дней;
- глобальный и индивидуальный срок хранения истории до 365 дней;
- опциональные звуковые уведомления о падении в браузере;
- медианный baseline и индивидуальный порог падения;
- подтверждение падения и восстановления несколькими измерениями;
- ручные, событийные и периодические DPI-проверки из России, Китая, Ирана и Туркменистана;
- отдельный набор вебхуков для каждой ноды;
- выбор часового пояса панели;
- общий и индивидуальный интервал опроса нод от 10 до 3600 секунд;
- Telegram-уведомления о событиях и входах с поддержкой топиков форума;
- отдельные события недоступности и восстановления соединения с нодой;
- HMAC-подпись вебхуков;
- авторизация, CSRF, защита от перебора и IP allowlist;
- SQLite, Prometheus-метрики и автоматическая очистка истории;
- Docker Compose и поддержка системного либо встроенного Caddy.

## Требования

- Linux-сервер с root-доступом или `sudo`;
- домен, направленный на сервер;
- Remnawave Panel 2.7.4 или новее;
- API-токен Remnawave с разрешениями чтения и изменения нод, системных метрик и инфрабиллинга;
- API-ключ DPI Checker — необязательно.

## Установка из Git

```bash
git clone https://github.com/ap1w1/Remna-Downdetector.git /opt/remnadown
cd /opt/remnadown
sudo bash install.sh
```

Установщик автоматически:

- установит Docker и Docker Compose при необходимости;
- запросит домен, URL Remnawave и API-токен;
- создаст `APP_SECRET` и случайный пароль администратора;
- обнаружит системный Caddy;
- соберёт и запустит приложение;
- выполнит healthcheck;
- покажет URL, логин и пароль.

Сохраните выданный пароль: повторно установщик его не выводит.

## Автоматическая установка

```bash
sudo DOMAIN=monitor.example.com \
  REMNAWAVE_URL=https://panel.example.com \
  REMNAWAVE_API_TOKEN=secret-token \
  PROXY_MODE=auto \
  bash install.sh --non-interactive
```

Дополнительные переменные: `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `DPI_API_KEY`, `APP_PORT`, `REMNAWAVE_CADDY_TOKEN` и `IP_ALLOWLIST`.

Режимы `PROXY_MODE`:

- `auto` — использовать системный Caddy или поднять встроенный;
- `system-caddy` — подключить приложение к `/etc/caddy/Caddyfile`;
- `bundled-caddy` — запустить Caddy в Docker;
- `local` — слушать только `127.0.0.1:8088`.

## Существующий Caddy

При активном systemd-сервисе Caddy установщик автоматически добавит:

```caddyfile
monitor.example.com {
    reverse_proxy 127.0.0.1:8088
}
```

Перед изменением создаётся `/etc/caddy/Caddyfile.remnadown.bak`. Конфигурация проверяется через `caddy validate`; при ошибке исходный файл восстанавливается.

## Существующий Nginx

Запустите установку с `PROXY_MODE=local`, затем добавьте в HTTPS server block Nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:8088;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## Обновление

```bash
cd /opt/remnadown
sudo bash update.sh
```

Выполняется `git pull --ff-only`, пересборка образа, перезапуск и healthcheck. Настройки и история сохраняются.

## Удаление

Удалить контейнеры, сохранив историю:

```bash
cd /opt/remnadown
sudo bash uninstall.sh
```

Удалить также Docker volume с историей:

```bash
cd /opt/remnadown
sudo PURGE_DATA=1 bash uninstall.sh
sudo rm -rf -- /opt/remnadown
```

## Ручная конфигурация

Скопируйте `.env.example` в `.env`. Основные переменные:

| Переменная | Назначение |
| --- | --- |
| `DOMAIN` | Домен веб-интерфейса без протокола |
| `APP_PORT` | Локальный порт приложения |
| `APP_SECRET` | Секрет подписи сессий |
| `ADMIN_USERNAME` | Логин администратора |
| `ADMIN_PASSWORD` | Пароль администратора |
| `REMNAWAVE_URL` | URL панели Remnawave |
| `REMNAWAVE_API_TOKEN` | Токен с правами на ноды, `system:nodes-metrics` и инфрабиллинг |
| `REMNAWAVE_CADDY_TOKEN` | Токен Caddy Security перед Remnawave |
| `DPI_API_KEY` | API-ключ DPI Checker |
| `POLL_INTERVAL_SECONDS` | Интервал опроса |
| `DEFAULT_DROP_PERCENT` | Стандартный порог падения |
| `HISTORY_DAYS` | Стандартный срок хранения; также меняется в GUI |
| `PANEL_TIMEZONE` | Исходный часовой пояс панели, например `Europe/Moscow` |
| `IP_ALLOWLIST` | Разрешённые IP/CIDR через запятую |

Ограничьте доступ к файлу:

```bash
chmod 600 .env
```

## Алгоритм детектора

1. Приложение получает ноды через `GET /api/nodes`.
2. Нормальный онлайн вычисляется как медиана предыдущих измерений.
3. Падение определяется относительно индивидуального процентного порога.
4. Инцидент открывается после нескольких последовательных плохих измерений.
5. Восстановление фиксируется после нескольких нормальных измерений.
6. Потеря соединения с нодой считается падением на 100%.

## DPI Checker

При наличии `DPI_API_KEY` проверку можно запустить вручную, при открытии инцидента или периодически. Для каждого режима выбираются регионы DPI Checker: Россия, Китай, Иран и Туркменистан. Приложение получает оптимальные PoP и использует `Idempotency-Key`. Учитывайте, что каждый регион создаёт отдельную платную проверку.

Для каждой ноды можно проверять IP или VLESS. Источником VLESS служит сохранённый ключ, подписка существующего пользователя Remnawave или временный пользователь. Для временного пользователя приложение само подбирает один internal squad, сопоставляет URI с привязанными к ноде Remnawave Hosts, находит ключ именно выбранной ноды и отклоняет служебные VLESS-заглушки. Подписка запрашивается с HWID-заголовками для совместимости с Remnawave latest. Пользователь создаётся с именем `dpi_checker_{id}` и запрашиваемым тегом `dpi//checker`; если версия API принимает только шаблон `^[A-Z0-9_]+$`, используется совместимый тег `DPI_CHECKER` с обязательной записью причины в журнал. Пользователь удаляется после завершения, ошибки или тайм-аута проверки.

При событии падения онлайна доступна упорядоченная цепочка действий: DPI по IP, DPI по VLESS, переход на резервный IP и обновление связанных DNS-записей. Каждый шаг можно независимо включить; начало и результат шага записываются в события, журналы и вебхуки.

## Метрики нод

Для Remnawave 2.8.1 RX/TX берутся напрямую из `system.stats.interface.rxBytesPerSec` и `txBytesPerSec`, переводятся из байт в биты и отображаются в `Мбит/с` или `Гбит/с`. Суммарные значения берутся из `rxTotal` и `txTotal`. RAM рассчитывается по `memoryUsed / memoryTotal`. Так как API отдаёт load average, но не процент загрузки CPU, панель рассчитывает CPU как `loadAvg[0] / system.info.cpus × 100`. Для старых ответов API сохранён резервный разбор `/api/system/nodes/metrics`. Данные интерфейса перечитываются каждые 5 секунд, а фактическая частота новых замеров определяется общим или индивидуальным интервалом опроса ноды.

## Telegram

В разделе «Настройки» укажите токен бота и Chat ID. Для форум-группы можно отдельно указать ID топика событий и ID топика авторизаций; пустой ID отправляет сообщения в общий чат. Токен хранится только в закрытом Docker volume, не возвращается через API и не записывается в логи. Сообщения авторизации содержат только логин и IP — пароль никогда не включается в уведомления или журналы.

## Безопасность

- подписанные `HttpOnly`, `Secure`, `SameSite=Strict` cookies;
- CSRF-защита изменяющих запросов;
- блокировка IP после пяти неудачных входов;
- поддержка IP/CIDR allowlist;
- доверие `X-Forwarded-For` только от настроенных прокси;
- CSP, HSTS и защитные HTTP-заголовки;
- запрет приватных адресов вебхуков и HTTP redirects для защиты от SSRF;
- HMAC-SHA256 подпись вебхуков;
- read-only контейнер без Linux capabilities.

Пароль хранится в закрытом `.env`. Используйте уникальный пароль и права файла `600`.

## Обслуживание

```bash
docker compose ps
docker compose logs -f app
curl http://127.0.0.1:8088/health
```

Prometheus: `GET /internal/metrics`, пользователь `metrics`, пароль — первые 24 символа `APP_SECRET`.

## Поддержать проект

Если RemnaDownDetector оказался полезен, вы можете поддержать развитие проекта:

| Способ | Адрес или ссылка |
| --- | --- |
| CryptoBot | [Отправить донат](https://t.me/send?start=IV112jEOQ7s1) |
| XRocket | [Отправить донат](https://t.me/xrocket?start=inv_XGcVb2B8xfpV6Wi) |
| USDT TRC20 | `TNFKQe2hpC12U2M9z9JX1WRjhZjS9c4Nqa` |
| USDT TON | `UQA4wspCqRxx7fJAXPfX64e0zhzzD7IZVBIs2LzaMe6etcGN` |
| USDT BEP20 | `0x1B7905b1F335cdA649100f660d824018e6dE60A2` |
| BTC | `bc1q72wry34y4duwkprpsdevjedrjgdngwnrhmlgfy` |

Спасибо за поддержку разработки, исправление ошибок и развитие новых интеграций.

## Совместимость

Проект автоматически определяет доступность современного API через system recap и содержит fallback для метрик старых версий. В настройках можно зафиксировать профиль `2.7.4` или `latest`; при обновлении на новую мажорную версию сверяйте права токена на users, squads, nodes, hosts и system stats.
