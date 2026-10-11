if (!Array.prototype.at) {
  Object.defineProperty(Array.prototype, "at", {
    value(index) {
      index = Math.trunc(index) || 0;
      if (index < 0) index += this.length;
      return this[index];
    },
  });
}

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const csrf = document.body.dataset.csrf;
const dpiEnabled = document.body.dataset.dpiEnabled === "true";
const REGIONS = {
  russia: { name: "Россия", code: "RU" },
};
const WEBHOOK_EVENTS = ["node.unavailable", "node.available", "node.online_drop", "node.online_recovered", "node.cpu_threshold", "node.ram_threshold", "node.rx_threshold", "node.tx_threshold", "node.ip_changed", "domain.ip_changed", "dpi.check_started", "dpi.check_completed", "dpi.balance_low", "rule.action_started", "rule.action_completed"];
const TELEGRAM_EVENTS = [...WEBHOOK_EVENTS, "auth.login", "auth.failed"];

let state = { nodes: [], events: [], webhooks: [], settings: {}, integration_alerts: [] };
let selected = null;
let history = [];
let metric = "online";
let days = 1;
let dpiTimer = null;
let lastEvent = 0;
let lastSeenEvent = Number(
  localStorage.getItem("remnadown-events-seen") || 0,
);
let loading = false;
let settingsHydrated = false;
let toastTimer = null;
let refreshTimer = null;
let dpiOverview = { profile: {}, checks: [], pops: [], stats: [] };
let onlineHours = 24;
let onlineSelected = new Set();
const ONLINE_COLORS = ["#2dd4bf", "#818cf8", "#fb7185", "#fbbf24", "#38bdf8", "#c084fc", "#4ade80", "#fb923c"];
const dpiAvailable = () => dpiEnabled || Boolean(state.settings.dpi_api_configured);
let domainData = [];
let domainsLoadedAt = 0;
let dpiLoadSequence = 0;
let pendingActivateId = null;
let notificationAudioContext = null;
let lastIntegrationAlert = sessionStorage.getItem("remnadown-integration-alert") || "";
let activeScheduleDay = 0;

const esc = (value) => String(value ?? "").replace(
  /[&<>"']/g,
  (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
);

const finite = (value) => {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

function enhanceCompactPickers() {
  $$("details.region-picker").forEach((picker) => {
    const popover = $(".region-picker-popover", picker);
    if (!popover || $(".picker-actions", popover)) return;
    popover.insertAdjacentHTML("beforeend", '<div class="picker-actions"><button type="button" data-picker-all>Выбрать все</button><button type="button" data-picker-clear>Очистить</button><button type="button" data-picker-done>Готово</button></div>');
    $("[data-picker-all]", popover).addEventListener("click", () => { $$("input[type='checkbox']", popover).forEach((input) => { input.checked = true; input.dispatchEvent(new Event("change", { bubbles: true })); }); });
    $("[data-picker-clear]", popover).addEventListener("click", () => { $$("input[type='checkbox']", popover).forEach((input) => { input.checked = false; input.dispatchEvent(new Event("change", { bubbles: true })); }); });
    $("[data-picker-done]", popover).addEventListener("click", () => { picker.open = false; });
    picker.addEventListener("toggle", () => { if (picker.open) $$("details.region-picker[open]").filter((item) => item !== picker).forEach((item) => { item.open = false; }); });
  });
}
enhanceCompactPickers();
document.addEventListener("pointerdown", (event) => { $$("details.region-picker[open]").forEach((picker) => { if (!picker.contains(event.target)) picker.open = false; }); });
function jsonHighlight(value) { return esc(typeof value === "string" ? value : JSON.stringify(value, null, 2)).replace(/(&quot;.*?&quot;)(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?|[{}\[\],:]/g, (match, text, colon, literal) => text ? `<span class="json-${colon ? "key" : "string"}">${text}</span>${colon ? '<span class="json-punctuation">:</span>' : ""}` : literal ? `<span class="json-literal">${match}</span>` : /^-?\d/.test(match) ? `<span class="json-number">${match}</span>` : `<span class="json-punctuation">${match}</span>`); }
function highlightedText(value) { try { return jsonHighlight(JSON.parse(value)); } catch { return esc(value); } }

function bytes(value) {
  let number = finite(value);
  if (number === null) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let unit = 0;
  number = Math.max(0, number);
  while (number >= 1024 && unit < units.length - 1) {
    number /= 1024;
    unit += 1;
  }
  const digits = unit >= 3 ? 2 : unit ? 1 : 0;
  return `${number.toFixed(digits)} ${units[unit]}`;
}

function bitrate(bytesPerSecond) {
  let number = finite(bytesPerSecond);
  if (number === null) return "—";
  number = Math.max(0, number) * 8;
  const units = ["b/s", "Kb/s", "Mb/s", "Gb/s", "Tb/s"];
  let unit = 0;
  while (number >= 1000 && unit < units.length - 1) {
    number /= 1000;
    unit += 1;
  }
  const digits = unit >= 3 ? 2 : unit ? 1 : 0;
  return `${number.toFixed(digits)} ${units[unit]}`;
}

function percent(value) {
  const number = finite(value);
  return number === null ? "—" : `${number.toFixed(number >= 10 ? 0 : 1)}%`;
}

function compactNumber(value) {
  return new Intl.NumberFormat("ru-RU").format(Math.max(0, finite(value) ?? 0));
}

function formatUptime(value) {
  const seconds = finite(value);
  if (seconds === null || seconds < 0) return "—";
  const total = Math.floor(seconds);
  const day = Math.floor(total / 86400);
  const hour = Math.floor((total % 86400) / 3600);
  const minute = Math.floor((total % 3600) / 60);
  if (day) return `${day}d ${hour}h`;
  if (hour) return `${hour}h ${minute}m`;
  return `${minute}m`;
}

function formatTime(value) {
  if (!value) return "—";
  try {
    return new Intl.DateTimeFormat("ru-RU", {
      timeZone: state.settings.timezone || "Europe/Moscow",
      day: "2-digit",
      month: "2-digit",
      year: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(value));
  } catch {
    return "—";
  }
}

function flag(code) {
  const country = String(code || "").toUpperCase().replace(/[^A-Z]/g, "").slice(0, 2);
  const base = (content, label = country || "—") => `<svg class="flag-svg" viewBox="0 0 30 20" role="img" aria-label="${esc(label)}">${content}</svg>`;
  const horizontal = (colors) => base(colors.map((color, index) => `<rect y="${(20 / colors.length) * index}" width="30" height="${20 / colors.length + 0.1}" fill="${color}"/>`).join(""));
  const vertical = (colors) => base(colors.map((color, index) => `<rect x="${(30 / colors.length) * index}" width="${30 / colors.length + 0.1}" height="20" fill="${color}"/>`).join(""));
  const flags = {
    RU: () => horizontal(["#fff", "#1751a4", "#d52b1e"]),
    NL: () => horizontal(["#ae1c28", "#fff", "#21468b"]),
    DE: () => horizontal(["#111", "#d00", "#ffce00"]),
    FR: () => vertical(["#002395", "#fff", "#ed2939"]),
    IT: () => vertical(["#009246", "#fff", "#ce2b37"]),
    BE: () => vertical(["#111", "#fdda24", "#ef3340"]),
    RO: () => vertical(["#002b7f", "#fcd116", "#ce1126"]),
    BG: () => horizontal(["#fff", "#00966e", "#d62612"]),
    HU: () => horizontal(["#ce2939", "#fff", "#477050"]),
    AT: () => horizontal(["#ed2939", "#fff", "#ed2939"]),
    PL: () => horizontal(["#fff", "#dc143c"]),
    UA: () => horizontal(["#0057b7", "#ffd700"]),
    EE: () => horizontal(["#4891d9", "#111", "#fff"]),
    LV: () => horizontal(["#9e3039", "#fff", "#9e3039"]),
    LT: () => horizontal(["#fdb913", "#006a44", "#c1272d"]),
    ES: () => base('<rect width="30" height="20" fill="#aa151b"/><rect y="5" width="30" height="10" fill="#f1bf00"/>'),
    PT: () => base('<rect width="12" height="20" fill="#046a38"/><rect x="12" width="18" height="20" fill="#da291c"/><circle cx="12" cy="10" r="3" fill="#f9d616"/>'),
    FI: () => base('<rect width="30" height="20" fill="#fff"/><rect x="8" width="4" height="20" fill="#003580"/><rect y="8" width="30" height="4" fill="#003580"/>'),
    SE: () => base('<rect width="30" height="20" fill="#006aa7"/><rect x="9" width="3" height="20" fill="#fecc00"/><rect y="8" width="30" height="3" fill="#fecc00"/>'),
    NO: () => base('<rect width="30" height="20" fill="#ba0c2f"/><rect x="8" width="5" height="20" fill="#fff"/><rect y="7.5" width="30" height="5" fill="#fff"/><rect x="9.5" width="2" height="20" fill="#00205b"/><rect y="9" width="30" height="2" fill="#00205b"/>'),
    DK: () => base('<rect width="30" height="20" fill="#c60c30"/><rect x="9" width="3" height="20" fill="#fff"/><rect y="8.5" width="30" height="3" fill="#fff"/>'),
    CH: () => base('<rect width="30" height="20" fill="#d52b1e"/><rect x="12.5" y="4" width="5" height="12" fill="#fff"/><rect x="9" y="7.5" width="12" height="5" fill="#fff"/>'),
    CZ: () => base('<rect width="30" height="10" fill="#fff"/><rect y="10" width="30" height="10" fill="#d7141a"/><path d="M0 0 14 10 0 20Z" fill="#11457e"/>'),
    US: () => base(`${Array.from({ length: 13 }, (_, index) => `<rect y="${index * 20 / 13}" width="30" height="${20 / 13 + 0.1}" fill="${index % 2 ? "#fff" : "#b22234"}"/>`).join("")}<rect width="13" height="10.8" fill="#3c3b6e"/><g fill="#fff">${Array.from({ length: 12 }, (_, index) => `<circle cx="${2 + (index % 4) * 3}" cy="${2 + Math.floor(index / 4) * 3}" r=".55"/>`).join("")}</g>`),
    GB: () => base('<rect width="30" height="20" fill="#012169"/><path d="m0 0 30 20M30 0 0 20" stroke="#fff" stroke-width="5"/><path d="m0 0 30 20M30 0 0 20" stroke="#c8102e" stroke-width="2"/><path d="M15 0v20M0 10h30" stroke="#fff" stroke-width="6"/><path d="M15 0v20M0 10h30" stroke="#c8102e" stroke-width="3"/>'),
    TR: () => base('<rect width="30" height="20" fill="#e30a17"/><circle cx="12" cy="10" r="5" fill="#fff"/><circle cx="13.7" cy="10" r="4" fill="#e30a17"/><path d="m18 7.6.8 1.6 1.8.2-1.3 1.2.4 1.8-1.7-.9-1.6.9.3-1.8-1.3-1.2 1.8-.2Z" fill="#fff"/>'),
    KZ: () => base('<rect width="30" height="20" fill="#00afca"/><circle cx="16" cy="9" r="3" fill="#f7d117"/><path d="M11 14c3 2 7 2 10 0" fill="none" stroke="#f7d117" stroke-width="1"/>'),
    CN: () => base('<rect width="30" height="20" fill="#de2910"/><path d="m6 3 .7 2.1h2.2L7.1 6.4l.7 2.1L6 7.2 4.2 8.5l.7-2.1-1.8-1.3h2.2Z" fill="#ffde00"/>'),
    IR: () => base('<rect width="30" height="20" fill="#239f40"/><rect y="6.67" width="30" height="6.67" fill="#fff"/><rect y="13.34" width="30" height="6.66" fill="#da0000"/><circle cx="15" cy="10" r="2" fill="#da0000"/>'),
    TM: () => base('<rect width="30" height="20" fill="#00843d"/><rect x="5" width="3" height="20" fill="#d22630"/><circle cx="18" cy="8" r="4" fill="#fff"/><circle cx="19.5" cy="8" r="3.4" fill="#00843d"/>'),
  };
  if (flags[country]) return flags[country]();
  return base(`<rect width="30" height="20" fill="#26333d"/><text x="15" y="13.5" text-anchor="middle" font-family="sans-serif" font-size="7" font-weight="700" fill="#c9d1d9">${esc(country || "—")}</text>`);
}

function providerMark(node) {
  const initials = String(node.provider_name || "?")
    .split(/\s+/)
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  if (!node.uuid) return `<i class="provider-mark">${esc(initials)}</i>`;
  return `<img class="provider-mark" src="/api/nodes/${encodeURIComponent(node.uuid)}/provider-icon" alt="" loading="lazy">`;
}

function latestSample(node) {
  return node.samples?.at(-1) || {};
}

function nodeState(node) {
  const sample = latestSample(node);
  if (node.is_unavailable || sample.connected === 0 || sample.connected === false) {
    return { className: "unavailable", label: "Недоступна" };
  }
  if (node.is_down) return { className: "warning", label: "Падение онлайна" };
  return { className: "healthy", label: "Онлайн" };
}

function healthIcon(className) {
  if (className === "unavailable") {
    return '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="m9 9 6 6M15 9l-6 6"/></svg>';
  }
  if (className === "warning") {
    return '<svg viewBox="0 0 24 24"><path d="M12 3 2.7 19h18.6L12 3Z"/><path d="M12 8v5M12 16h.01"/></svg>';
  }
  return '<svg viewBox="0 0 24 24"><path d="M4 13h3l2-6 4 11 2-5h5"/></svg>';
}

function renderSummary() {
  const latest = state.nodes.map(latestSample);
  const totalRx = latest.reduce((sum, sample) => sum + (finite(sample.rx_bps) ?? 0), 0);
  const totalTx = latest.reduce((sum, sample) => sum + (finite(sample.tx_bps) ?? 0), 0);
  const totalOnline = state.nodes.reduce((sum, node) => sum + (finite(node.latest_online) ?? 0), 0);
  const available = state.nodes.filter((node) => nodeState(node).className !== "unavailable").length;
  const cards = [
    {
      label: "Скорость загрузки",
      value: bitrate(totalRx),
      tone: "teal",
      icon: '<svg viewBox="0 0 24 24"><path d="M12 4v12M7 11l5 5 5-5M5 20h14"/></svg>',
    },
    {
      label: "Скорость отдачи",
      value: bitrate(totalTx),
      tone: "indigo",
      icon: '<svg viewBox="0 0 24 24"><path d="M12 20V8M7 13l5-5 5 5M5 4h14"/></svg>',
    },
    {
      label: "Пользователи онлайн",
      value: compactNumber(totalOnline),
      tone: "cyan",
      icon: '<svg viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    },
    {
      label: "Ноды онлайн",
      value: `${available} / ${state.nodes.length}`,
      tone: "teal",
      icon: '<svg viewBox="0 0 24 24"><path d="M4 13h3l2-6 4 11 2-5h5"/></svg>',
    },
  ];
  $("#summary").innerHTML = cards.map((card) => `
    <article class="summary-card">
      <span class="summary-icon ${card.tone}">${card.icon}</span>
      <div class="summary-copy"><span>${card.label}</span><b>${card.value}</b></div>
    </article>
  `).join("");
}

function nodeRow(node) {
  const sample = latestSample(node);
  const status = nodeState(node);
  const ram = Math.max(0, Math.min(100, finite(sample.ram_percent) ?? 0));
  const cpu = Math.max(0, Math.min(100, finite(sample.cpu_percent) ?? 0));
  const loadPressure = node.cpu_count
    ? Math.max(0, Math.min(100, ((finite(sample.load_1) ?? 0) / node.cpu_count) * 100))
    : 0;
  const systemLoad = Math.max(cpu, ram, loadPressure);
  const loadTone = systemLoad >= 85 ? "critical" : systemLoad >= 65 ? "warning" : "";
  const totalTraffic = finite(node.traffic_used_bytes)
    ?? ((finite(sample.rx_total) ?? 0) + (finite(sample.tx_total) ?? 0));
  const load = [sample.load_1, sample.load_5, sample.load_15]
    .map((value) => finite(value))
    .map((value) => value === null ? "—" : value.toFixed(2))
    .join(" ");
  return `
    <button class="node-row ${status.className}" type="button" data-node="${esc(node.uuid)}">
      <span class="node-primary">
        <span class="node-title-line">
          <span class="health-icon">${healthIcon(status.className)}</span>
          <span class="online-badge"><svg viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8"/></svg>${compactNumber(node.latest_online)}</span>
          ${flag(node.country_code)}
          <span class="node-name">${esc(node.name)}</span>
        </span>
        <span class="node-subline">
          <span class="ram-mini-track ${loadTone}" style="--load-width:${systemLoad}%" title="Нагрузка системы: ${Math.round(systemLoad)}%"></span>
          <span>${Math.round(systemLoad)}%</span>
          <span class="load-value">● ${load}</span>
          <span class="rx">↓ ${bitrate(sample.rx_bps)}</span>
          <span class="tx">↑ ${bitrate(sample.tx_bps)}</span>
        </span>
      </span>
      <span class="node-provider">
        <span class="provider-line"><span class="provider-badge">${providerMark(node)}<span>${esc(node.provider_name || "Провайдер")}</span></span></span>
        <span class="node-address"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M4 12h16M12 4a12 12 0 0 1 0 16M12 4a12 12 0 0 0 0 16"/></svg><span>${esc(node.address || "Адрес не указан")}</span></span>
      </span>
      <span class="node-traffic">
        <span class="traffic-line"><b>${bytes(totalTraffic)}</b><span>∞</span></span>
        <span class="progress-track"><i></i></span>
      </span>
      <span class="node-runtime">
        <span class="runtime-line">✦ ${formatUptime(node.uptime)}</span>
        <span class="runtime-versions"><span>◆ ${esc(node.xray_version || "—")}</span><span>◆ ${esc(node.node_version || "—")}</span></span>
      </span>
      <span class="node-chevron"><svg viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg></span>
    </button>
  `;
}

function filteredNodes() {
  const query = $("#search").value.trim().toLowerCase();
  const status = $("#status-filter").value;
  const country = $("#country-filter").value;
  const sort = $("#node-sort").value;
  let nodes = state.nodes.filter((node) => {
    const current = nodeState(node).className;
    const matchesQuery = !query || `${node.name} ${node.address} ${node.provider_name}`.toLowerCase().includes(query);
    const matchesCountry = country === "all" || node.country_code === country;
    const matchesStatus = status === "all"
      || (status === "up" && current === "healthy")
      || (status === "down" && current === "warning")
      || (status === "unavailable" && current === "unavailable");
    return matchesQuery && matchesCountry && matchesStatus;
  });
  nodes = [...nodes];
  if (sort === "online-desc") nodes.sort((left, right) => right.latest_online - left.latest_online);
  if (sort === "online-asc") nodes.sort((left, right) => left.latest_online - right.latest_online);
  if (sort === "name-asc") nodes.sort((left, right) => left.name.localeCompare(right.name, "ru"));
  if (sort === "name-desc") nodes.sort((left, right) => right.name.localeCompare(left.name, "ru"));
  return nodes;
}

function populateCountryFilter() {
  const select = $("#country-filter");
  const current = select.value;
  const countries = [...new Set(state.nodes.map((node) => node.country_code).filter(Boolean))].sort();
  select.innerHTML = '<option value="all">Все страны</option>' + countries
    .map((code) => `<option value="${esc(code)}">${esc(code)}</option>`)
    .join("");
  select.value = countries.includes(current) ? current : "all";
}

function renderNodes() {
  const nodes = filteredNodes();
  $("#nodes").innerHTML = nodes.length
    ? nodes.map(nodeRow).join("")
    : '<div class="empty">По заданным условиям ноды не найдены</div>';
  $("#nodes-subtitle").textContent = `Показано ${nodes.length} из ${state.nodes.length}`;
}

const EVENT_META = {
  "node.unavailable": {
    label: "Нода недоступна",
    className: "unavailable",
    icon: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="m9 9 6 6M15 9l-6 6"/></svg>',
  },
  "node.available": {
    label: "Нода снова доступна",
    className: "available",
    icon: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="m8.5 12 2.3 2.3 4.8-5"/></svg>',
  },
  "node.online_drop": {
    label: "Падение онлайна",
    className: "drop",
    icon: '<svg viewBox="0 0 24 24"><path d="M4 7h5v5M20 17h-5v-5M9 12 4 7M15 12l5 5"/></svg>',
  },
  "node.online_recovered": {
    label: "Онлайн восстановлен",
    className: "recovered",
    icon: '<svg viewBox="0 0 24 24"><path d="M4 17h5v-5M20 7h-5v5M9 12l-5 5M15 12l5-5"/></svg>',
  },
  "node.ip_changed": {
    label: "IP ноды изменён",
    className: "available",
    icon: '<svg viewBox="0 0 24 24"><path d="M7 7h11v11M17 7 6 18"/></svg>',
  },
  "dpi.check_started": { label: "DPI-проверка запущена", className: "dpi-event", icon: '<img src="/api/integration-icon/dpi" alt="">' },
  "dpi.check_completed": { label: "DPI-проверка завершена", className: "dpi-event", icon: '<img src="/api/integration-icon/dpi" alt="">' },
  "dpi.balance_low": { label: "Заканчивается баланс DPI", className: "dpi-event", icon: '<img src="/api/integration-icon/dpi" alt="">' },
  "domain.ip_changed": { label: "DNS-запись изменена", className: "dns-event", icon: '<img src="/api/integration-icon/regru" alt="">' },
  "node.cpu_threshold": { label: "Превышен порог CPU", className: "drop", icon: '<svg viewBox="0 0 24 24"><path d="M8 8h8v8H8zM4 10h4M16 10h4M10 4v4M10 16v4"/></svg>' },
  "node.ram_threshold": { label: "Превышен порог RAM", className: "drop", icon: '<svg viewBox="0 0 24 24"><rect x="5" y="7" width="14" height="10" rx="2"/><path d="M8 10v4M12 10v4M16 10v4"/></svg>' },
  "node.rx_threshold": { label: "Превышен порог RX", className: "drop", icon: '<svg viewBox="0 0 24 24"><path d="M12 4v14m0 0-5-5m5 5 5-5"/></svg>' },
  "node.tx_threshold": { label: "Превышен порог TX", className: "drop", icon: '<svg viewBox="0 0 24 24"><path d="M12 20V6m0 0-5 5m5-5 5 5"/></svg>' },
};

function newestEventId() {
  return Math.max(0, ...state.events.map((event) => finite(event.id) ?? 0));
}

function updateEventsBadge() {
  const seen = Number.isFinite(lastSeenEvent) ? lastSeenEvent : 0;
  const unread = state.events.filter(
    (event) => (finite(event.id) ?? 0) > seen,
  ).length;
  $("#events-count").hidden = unread === 0;
  $("#events-count").textContent = unread > 99 ? "99+" : String(unread);
}

function markEventsRead() {
  lastSeenEvent = Math.max(lastSeenEvent || 0, newestEventId());
  localStorage.setItem("remnadown-events-seen", String(lastSeenEvent));
  updateEventsBadge();
}

function renderEvents() {
  const query = $("#event-search").value.trim().toLowerCase();
  const kind = $("#event-filter").value;
  const events = state.events.filter((event) => (
    (!query || String(event.name || "").toLowerCase().includes(query))
    && (kind === "all" || event.event === kind)
  ));
  if ($("#events").classList.contains("active")) markEventsRead();
  else updateEventsBadge();
  $("#events-list").innerHTML = events.length ? events.map((event) => {
    const meta = { ...(EVENT_META[event.event] || { label: event.event, className: "", icon: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="2"/></svg>' }) };
    let payload = {};
    try { payload = JSON.parse(event.payload || "{}"); } catch { payload = {}; }
    if (event.event === "domain.ip_changed") meta.icon = `<img src="/api/integration-icon/${payload.provider === "Cloudflare" ? "cloudflare" : "regru"}" alt="">`;
    const details = event.event.startsWith("dpi.")
      ? `${payload.target || "—"} · ${payload.status || (event.event.endsWith("started") ? "запущена" : "завершена")}`
      : event.event === "node.ip_changed"
      ? `${payload.old_address || "—"} → ${payload.new_address || "—"}`
      : event.event === "domain.ip_changed"
      ? `${payload.domain || "—"} · ${payload.old_ip || "—"} → ${payload.new_ip || "—"}`
      : payload.online !== undefined
      ? `Онлайн: ${compactNumber(payload.online)}${payload.drop_percent !== undefined ? ` · изменение ${Number(payload.drop_percent).toFixed(1)}%` : ""}`
      : "Состояние ноды изменено";
    return `
      <details class="event-card ${meta.className}"><summary>
        <span class="event-kind-icon">${meta.icon}</span>
        <div class="event-copy"><small>${esc(meta.label)}</small><b>${flag(event.country_code)} ${esc(event.name)}</b><span>${esc(details)}</span></div>
        <code class="event-code">${esc(event.event)}</code><time class="event-time">${formatTime(event.created_at)}</time><span class="event-chevron">⌄</span></summary>
        <div class="event-details"><div class="json-toolbar"><span>JSON события</span><button type="button" data-copy-json='${esc(JSON.stringify(payload))}'>Копировать</button></div><pre>${jsonHighlight(payload)}</pre></div>
      </details>
    `;
  }).join("") : '<div class="empty">Событий по заданному фильтру нет</div>';
}

function renderHooks() {
  $("#hooks-list").innerHTML = state.webhooks.length ? state.webhooks.map((hook) => `
    <article class="hook-row hook-row-events" data-hook-id="${hook.id}">
      <div class="hook-main"><span class="hook-status"></span><span><b>${esc(hook.name)}</b><small>${esc(hook.url)}</small></span><label class="hook-enabled"><input type="checkbox" data-hook-enabled ${hook.enabled ? "checked" : ""}><i></i></label><button class="icon-row-button danger" type="button" data-delete-hook="${hook.id}" title="Удалить">×</button></div>
      <details class="event-select-dropdown route-events"><summary><span>Выбрать события</span><b>${hook.events?.length ? `${hook.events.length} из ${WEBHOOK_EVENTS.length}` : "Все события"}</b></summary><div class="hook-event-list">${WEBHOOK_EVENTS.map((name) => `<label><input type="checkbox" value="${name}" ${!hook.events?.length || hook.events.includes(name) ? "checked" : ""}><span>${name}</span></label>`).join("")}</div><button class="route-save-button" type="button" data-save-hook-events><span>✓</span> Сохранить маршрут</button></details>
    </article>
  `).join("") : '<div class="empty">Вебхуков пока нет</div>';
}

function render() {
  populateCountryFilter();
  renderSummary();
  renderNodes();
  renderEvents();
  renderHooks();
  const current = checked("#dpi-node-options");
  $("#dpi-node-options").innerHTML = state.nodes.map((node) => `<label><input type="checkbox" value="${esc(node.uuid)}" ${current.includes(node.uuid) ? "checked" : ""}><span>${flag(node.country_code)}<b>${esc(node.name)}</b><small>${esc(node.address || "IP не указан")}</small></span></label>`).join("");
  updateDpiNodeCount();
}
function updateDpiNodeCount() { const count = checked("#dpi-node-options").length; $("#dpi-node-count").textContent = count ? `Выбрано: ${count}` : "Выберите ноды"; }

function renderOnline() {
  const nodes = onlineSelected.size ? state.nodes.filter((node) => onlineSelected.has(node.uuid)) : state.nodes;
  const total = nodes.reduce((sum, node) => sum + (finite(node.latest_online) ?? 0), 0);
  $("#online-summary").innerHTML = `<article class="summary-card"><span>Общий онлайн</span><b>${compactNumber(total)}</b></article><article class="summary-card"><span>Нод в графике</span><b>${nodes.length}</b></article><article class="summary-card"><span>Доступны</span><b>${nodes.filter((node) => !node.is_down && !node.is_unavailable).length}</b></article>`;
  $("#online-nodes").innerHTML = nodes.map((node) => `<article class="online-node-card">${flag(node.country_code)}<span><b>${esc(node.name)}</b><small>${esc(node.address)}</small></span><strong>${compactNumber(node.latest_online)}</strong></article>`).join("") || '<div class="empty">Нет нод по выбранным фильтрам</div>';
  renderOnlineChart(nodes);
}

function renderOnlineChart(nodes) {
  const cutoff = Date.now() - onlineHours * 3600000;
  const series = nodes.map((node, index) => ({ node, color: ONLINE_COLORS[index % ONLINE_COLORS.length], points: (node.samples || []).map((sample) => [new Date(sample.created_at).getTime(), finite(sample.online) ?? 0]).filter(([time]) => time >= cutoff) })).filter((item) => item.points.length);
  const points = series.flatMap((item) => item.points);
  $("#online-chart-caption").innerHTML = series.map((item) => `<span class="online-legend"><i style="background:${item.color}"></i>${esc(item.node.name)}</span>`).join("");
  if (!points.length) { $("#online-chart").innerHTML = '<div class="empty">Нет данных за выбранный период</div>'; ["#online-current", "#online-max", "#online-min"].forEach((id) => $(id).textContent = "—"); return; }
  const width = 1000, height = 270, margin = { top: 14, right: 18, bottom: 30, left: 76 };
  const values = points.map(([, value]) => value), max = Math.max(1, ...values), min = 0, span = Math.max(1, max);
  const minTime = Math.min(...points.map(([time]) => time)), maxTime = Math.max(...points.map(([time]) => time));
  const xForTime = (time) => margin.left + (time - minTime) * (width - margin.left - margin.right) / Math.max(1, maxTime - minTime);
  const yFor = (value) => margin.top + (max - value) * (height - margin.top - margin.bottom) / span;
  const grid = Array.from({ length: 5 }, (_, index) => { const ratio = index / 4, y = margin.top + ratio * (height - margin.top - margin.bottom), value = max - ratio * span; return `<line class="chart-grid" x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}"/><text class="chart-axis-label" x="${margin.left - 9}" y="${y + 3}" text-anchor="end">${compactNumber(Math.round(value))}</text>`; }).join("");
  const lines = series.map((item) => `<path class="online-node-line" style="stroke:${item.color}" d="${item.points.map(([time,value], index) => `${index ? "L" : "M"}${xForTime(time).toFixed(2)},${yFor(value).toFixed(2)}`).join(" ")}"/>`).join("");
  $("#online-chart").innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">${grid}${lines}</svg>`;
  $("#online-current").textContent = compactNumber(series.reduce((sum,item) => sum + item.points.at(-1)[1], 0)); $("#online-max").textContent = compactNumber(max); $("#online-min").textContent = compactNumber(min);
}

function bindOnlineChart(points, xFor, margin, width, height) {
  const container = $("#online-chart"), cursor = $(".chart-cursor", container), tooltip = $(".chart-tooltip", container);
  container.onpointermove = (event) => { const rect = container.getBoundingClientRect(), x = Math.max(0, Math.min(rect.width, event.clientX - rect.left)), chartX = x / rect.width * width, index = Math.max(0, Math.min(points.length - 1, Math.round((chartX - margin.left) / Math.max(1, width - margin.left - margin.right) * (points.length - 1)))); cursor.setAttribute("x1", xFor(index)); cursor.setAttribute("x2", xFor(index)); cursor.setAttribute("visibility", "visible"); tooltip.hidden = false; tooltip.style.left = `${x}px`; tooltip.innerHTML = `<b>${compactNumber(points[index][1])}</b><span>${new Date(points[index][0]).toLocaleString("ru-RU")}</span>`; };
  container.onpointerleave = () => { cursor.setAttribute("visibility", "hidden"); tooltip.hidden = true; };
}

function regionChoices(selector, selectedRegions = []) {
  $(selector).innerHTML = Object.entries(REGIONS).map(([value, region]) => `
    <label class="choice">
      <input type="checkbox" value="${value}" ${selectedRegions.includes(value) ? "checked" : ""}>
      ${flag(region.code)}
      <span>${region.name}</span>
    </label>
  `).join("");
}

function checked(selector) {
  return $$(`${selector} input:checked`).map((input) => input.value);
}

function renderIpPool(node) {
  $("#max-ip-replacements").value = node.max_ip_replacements ?? 1;
  const items = node.ip_pool || [];
  $("#ip-pool-list").innerHTML = items.length ? items.map((item) => `
    <div class="ip-pool-row ${esc(item.status)}" draggable="true" data-ip-id="${item.id}">
      <span class="drag-handle">⋮⋮</span><span><b>${esc(item.address)}</b><small>${item.status === "valid" ? "Привязан" : item.status === "invalid" ? esc(item.error || "Не подключился") : "Не проверен"}</small></span>
      <span class="ip-row-actions"><button class="icon-row-button" type="button" data-recheck-ip="${item.id}" title="Перепроверить">↻</button><button class="icon-row-button activate" type="button" data-activate-ip="${item.id}" title="Установить этот IP" ${item.status !== "valid" ? "disabled" : ""}>✓</button><button class="icon-row-button danger" type="button" data-delete-ip="${item.id}" title="Удалить">×</button></span>
    </div>
  `).join("") : '<div class="empty">Резервные IP не добавлены</div>';
}

$(".timezone-select")?.closest("label")?.insertAdjacentHTML("afterend", '<label>API Remnawave<select name="remnawave_api_version"><option value="auto">Автоопределение</option><option value="2.7.4">2.7.4</option><option value="latest">Latest</option></select><small id="remnawave-version-state"></small></label>');

function fillAppSettings(force = false) {
  const form = $("#app-settings");
  if (!form || (settingsHydrated && !force)) {
    if ($("#telegram-state")) {
      $("#telegram-state").textContent = state.settings.telegram_configured ? "Токен сохранён" : "Бот не настроен";
    }
    return;
  }
  form.history_days.value = state.settings.history_days ?? 30;
  form.poll_interval_seconds.value = state.settings.poll_interval ?? 60;
  form.timezone.value = state.settings.timezone || "Europe/Moscow";
  form.remnawave_api_version.value = state.settings.remnawave_api_version || "auto";
  $("#remnawave-version-state").textContent = `Обнаружено: ${state.settings.remnawave_detected_version || "—"}`;
  form.dpi_schedule_enabled.checked = Boolean(state.settings.dpi_schedule_enabled);
  form.dpi_schedule_mode.value = state.settings.dpi_schedule_mode || "interval";
  form.dpi_schedule_time.value = state.settings.dpi_schedule_time || "03:00";
  form.dpi_interval_minutes.value = state.settings.dpi_interval_minutes ?? 360;
  form.dpi_infra_billing_enabled.checked = Boolean(state.settings.dpi_infra_billing_enabled);
  form.dpi_balance_threshold.value = state.settings.dpi_balance_threshold ?? 1;
  form.dpi_api_key.value = "";
  $("#dpi-api-state").textContent = state.settings.dpi_api_configured ? "API-токен сохранён" : "API-токен не настроен";
  form.telegram_enabled.checked = Boolean(state.settings.telegram_enabled);
  form.telegram_bot_token.value = "";
  form.telegram_chat_id.value = state.settings.telegram_chat_id || "";
  form.telegram_events_topic.value = state.settings.telegram_events_topic || "";
  form.telegram_auth_topic.value = state.settings.telegram_auth_topic || "";
  const telegramSelected = state.settings.telegram_event_types || [];
  $("#telegram-event-types").innerHTML = TELEGRAM_EVENTS.map((name) => `<label><input type="checkbox" value="${name}" ${!telegramSelected.length || telegramSelected.includes(name) ? "checked" : ""}><span>${name}</span></label>`).join("");
  const updateTelegramCount = () => { const count = checked("#telegram-event-types").length; $("#telegram-event-count").textContent = count === TELEGRAM_EVENTS.length ? "Все события" : `${count} из ${TELEGRAM_EVENTS.length}`; };
  $$("input", $("#telegram-event-types")).forEach((input) => input.addEventListener("change", updateTelegramCount)); updateTelegramCount();
  form.regru_ttl.value = state.settings.regru_ttl ?? 600;
  form.cloudflare_ttl.value = state.settings.cloudflare_ttl ?? 600;
  renderWeeklySchedule();
  $("#telegram-state").textContent = state.settings.telegram_configured ? "Токен сохранён" : "Бот не настроен";
  const scheduledNodes = state.settings.dpi_schedule_nodes || [];
  $("#schedule-nodes").innerHTML = state.nodes.length ? state.nodes.map((node) => `
    <label><input type="checkbox" value="${esc(node.uuid)}" ${scheduledNodes.includes(node.uuid) ? "checked" : ""}><span>${flag(node.country_code)}<span><b>${esc(node.name)}</b><small>${esc(node.address || "IP не указан")}</small></span></span></label>
  `).join("") : '<span class="muted">Ноды ещё не загружены</span>';
  updateScheduleNodeCount();
  syncScheduleEnabled();
  settingsHydrated = true;
}

function scheduleTimeRow(day, value = "03:00") {
  return `<div class="schedule-time-row"><input type="time" data-weektime="${day}" value="${esc(value)}"><button type="button" data-remove-time aria-label="Удалить время">×</button></div>`;
}

function bindScheduleCalendar() {
  $$('[data-schedule-tab]').forEach((button) => {
    if (button.dataset.bound) return;
    button.dataset.bound = "1";
    button.addEventListener("click", () => {
      activeScheduleDay = Number(button.dataset.scheduleTab);
      syncScheduleDayPanels();
    });
  });
  $$('[data-add-time]').forEach((button) => {
    if (button.dataset.bound) return;
    button.dataset.bound = "1";
    button.addEventListener("click", () => {
      $(".schedule-times", button.closest(".schedule-day")).insertAdjacentHTML("beforeend", scheduleTimeRow(button.dataset.addTime));
      bindScheduleCalendar();
      updateScheduleSummary();
    });
  });
  $$('[data-remove-time]').forEach((button) => {
    if (button.dataset.bound) return;
    button.dataset.bound = "1";
    button.addEventListener("click", () => {
      button.closest(".schedule-time-row").remove();
      updateScheduleSummary();
    });
  });
  $$('[data-weektime]').forEach((input) => {
    if (input.dataset.bound) return;
    input.dataset.bound = "1";
    input.addEventListener("change", updateScheduleSummary);
  });
}

function syncScheduleDayPanels() {
  $$('[data-schedule-tab]').forEach((button) => {
    button.classList.toggle(
      "active",
      Number(button.dataset.scheduleTab) === activeScheduleDay,
    );
  });
  $$('[data-schedule-panel]').forEach((panel) => {
    panel.classList.toggle(
      "active",
      Number(panel.dataset.schedulePanel) === activeScheduleDay,
    );
  });
}

function renderWeeklySchedule() {
  const days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"];
  const shortDays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
  const grouped = new Map();
  (state.settings.dpi_weekly_schedule || []).filter((item) => item.enabled !== false).forEach((item) => {
    const day = Number(item.day);
    grouped.set(day, [...(grouped.get(day) || []), item.time || "03:00"]);
  });
  if (!grouped.has(activeScheduleDay) && grouped.size) {
    activeScheduleDay = grouped.keys().next().value;
  }
  const tabs = days.map((name, day) => {
    const count = (grouped.get(day) || []).length;
    return `<button class="schedule-day-tab ${day === activeScheduleDay ? "active" : ""} ${count ? "selected" : ""}" type="button" data-schedule-tab="${day}" title="${name}"><span>${shortDays[day]}</span><b data-tab-count>${count || ""}</b></button>`;
  }).join("");
  const panels = days.map((name, day) => {
    const times = grouped.get(day) || [];
    return `<section class="schedule-day schedule-day-panel ${times.length ? "selected" : ""} ${day === activeScheduleDay ? "active" : ""}" data-schedule-panel="${day}"><div class="schedule-day-name"><span>${shortDays[day]}</span><div><b>${name}</b><small data-day-count>${times.length ? `${times.length} запусков` : "Не запускается"}</small></div></div><div class="schedule-times">${times.map((time) => scheduleTimeRow(day, time)).join("")}</div><button class="schedule-add-time" type="button" data-add-time="${day}">+ Время</button></section>`;
  }).join("");
  $("#dpi-weekly-calendar").innerHTML = `<div class="schedule-day-tabs">${tabs}</div><div class="schedule-day-panels">${panels}</div>`;
  bindScheduleCalendar();
  syncScheduleDayPanels();
  syncScheduleMode();
  updateScheduleSummary();
}

function updateScheduleSummary() {
  const inputs = $$('[data-weektime]');
  const dayCount = new Set(inputs.map((input) => input.dataset.weektime)).size;
  $("#schedule-summary").textContent = inputs.length ? `${dayCount} дн. · ${inputs.length} запусков` : "Расписание не задано";
  $$(".schedule-day", $("#dpi-weekly-calendar")).forEach((day) => {
    const count = $$('[data-weektime]', day).length;
    day.classList.toggle("selected", count > 0);
    const label = $("[data-day-count]", day);
    if (label) label.textContent = count ? `${count} запусков` : "Не запускается";
    const dayNumber = day.dataset.schedulePanel;
    const tab = $(`[data-schedule-tab="${dayNumber}"]`);
    if (tab) {
      tab.classList.toggle("selected", count > 0);
      $("[data-tab-count]", tab).textContent = count || "";
    }
  });
}

function syncScheduleMode() {
  const daily = $("#dpi-schedule-mode").value === "daily";
  $("#dpi-calendar-field").hidden = !daily;
  $("#dpi-interval-field").hidden = daily;
}

function syncScheduleEnabled() {
  $("#dpi-schedule-settings").hidden = !$("#dpi-schedule-enabled").checked;
}

function updateScheduleNodeCount() {
  const count = checked("#schedule-nodes").length;
  $("#schedule-node-count").textContent = count ? `Выбрано: ${count}` : "Все ноды";
}

function syncVlessSource() {
  const mode = $("#vless-mode").value;
  $$('[data-vless-field]').forEach((field) => { field.hidden = field.dataset.vlessField !== mode; });
}

function syncAutomationOptions() {
  const vlessEnabled = $('#node-action-chain input[value="dpi_vless"]').checked;
  const dpiEnabled = vlessEnabled || $('#node-action-chain input[value="dpi_ip"]').checked;
  $("#vless-config-block").hidden = !vlessEnabled;
  $("#node-regions-block").hidden = !dpiEnabled;
}

function populateNodeDetails(node, fillForm = false) {
  const sample = latestSample(node);
  const status = nodeState(node);
  const totalTraffic = (finite(sample.rx_total) ?? 0) + (finite(sample.tx_total) ?? 0);
  const totalMemory = finite(node.memory_total) ?? finite(node.total_ram);
  const usedMemory = finite(node.memory_used)
    ?? (totalMemory === null ? null : totalMemory * (finite(sample.ram_percent) ?? 0) / 100);
  const ramPercent = Math.max(0, Math.min(100, finite(sample.ram_percent) ?? 0));
  $("#dialog-title").textContent = node.name || "Нода";
  $("#dialog-address").textContent = node.address || "—";
  $("#dialog-provider").textContent = node.provider_name || "Провайдер не указан";
  $("#dialog-status").className = `status-pill ${status.className}`;
  $("#dialog-status").textContent = status.label;
  $("#dialog-system-badge").textContent = node.country_code ? `${node.country_code} / NODE` : "NODE";
  $("#dialog-traffic-total").textContent = bytes(totalTraffic);
  $("#dialog-traffic-bar").style.width = totalTraffic > 0 ? "100%" : "0%";
  $("#dialog-online").textContent = compactNumber(node.latest_online);
  $("#dialog-xray-version").textContent = node.xray_version || "—";
  $("#dialog-node-version").textContent = node.node_version || "—";
  $("#dialog-uptime").textContent = formatUptime(node.uptime);
  $("#dialog-memory-values").textContent = totalMemory === null ? `${percent(sample.ram_percent)}` : `${bytes(usedMemory)} / ${bytes(totalMemory)} (${percent(sample.ram_percent)})`;
  $("#dialog-memory-bar").style.width = `${ramPercent}%`;
  $("#dialog-rx").textContent = bitrate(sample.rx_bps);
  $("#dialog-tx").textContent = bitrate(sample.tx_bps);
  $("#dialog-rx-total").textContent = `Всего: ${bytes(sample.rx_total)}`;
  $("#dialog-tx-total").textContent = `Всего: ${bytes(sample.tx_total)}`;
  $("#dialog-cpu").textContent = `${node.cpu_count || "—"} × ${node.cpu_model || "CPU"} · ${percent(sample.cpu_percent)}`;
  $("#dialog-load").textContent = `Load: ${[sample.load_1, sample.load_5, sample.load_15].map((value) => finite(value)?.toFixed(2) ?? "—").join(" / ")}`;
  if (!fillForm) return;
  const form = $("#node-settings");
  form.drop_percent.value = node.drop_percent ?? 35;
  form.retention_days.value = node.retention_days ?? state.settings.history_days ?? 30;
  form.poll_interval_seconds.value = node.poll_interval_seconds || "";
  form.webhooks_enabled.checked = Boolean(node.webhooks_enabled);
  form.auto_dpi.checked = Boolean(node.auto_dpi);
  form.vless_mode.value = node.vless_mode === "manual" && !node.vless_key_configured ? "temporary" : (node.vless_mode || "temporary");
  form.vless_key.value = "";
  form.vless_username.value = node.vless_username || "";
  syncVlessSource();
  const enabledActions = new Set((node.action_chain || []).filter((item) => item.enabled !== false).map((item) => item.type));
  $$("input[type='checkbox']", $("#node-action-chain")).forEach((input) => { input.checked = enabledActions.has(input.value); });
  syncAutomationOptions();
  form.cpu_threshold.value = node.cpu_threshold || 0;
  form.ram_threshold.value = node.ram_threshold || 0;
  form.rx_threshold_mbps.value = node.rx_threshold_mbps || 0;
  form.tx_threshold_mbps.value = node.tx_threshold_mbps || 0;
  $("#node-hooks").innerHTML = state.webhooks.length ? state.webhooks.map((hook) => `
    <label class="node-hook-option"><input type="checkbox" value="${hook.id}" ${(node.webhook_ids || []).includes(hook.id) ? "checked" : ""}><i></i><span><b>${esc(hook.name)}</b><small>${esc(hook.url)}</small></span></label>
  `).join("") : '<span class="muted">Сначала добавьте вебхук</span>';
  $("#node-regions").innerHTML = dpiOverview.pops.length ? dpiOverview.pops.map((pop) => popChoiceMarkup(pop, (node.dpi_pop_ids || []).includes(Number(pop.id)))).join("") : '<span class="muted">Сначала загрузите регионы DPI Checker</span>';
  filterNodeRegions();
  renderIpPool(node);
  $("#dpi-run").disabled = !dpiAvailable();
  if (!dpiAvailable()) $("#dpi-result").innerHTML = '<div class="empty">DPI API-токен не настроен</div>';
}

function refreshIntervalMs() {
  const seconds = Math.max(10, finite(state.settings.poll_interval) ?? 60);
  return seconds * 1000;
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  if (!document.hidden) refreshTimer = setTimeout(() => load(), refreshIntervalMs());
}

async function load({ force = false } = {}) {
  if (loading) return;
  loading = true;
  const refreshStartedAt = force ? performance.now() : 0;
  const nodeDialogScroll = $("#node-dialog")?.open ? $(".dialog-scroll", $("#node-dialog")) : null;
  const nodeDialogScrollTop = nodeDialogScroll?.scrollTop ?? null;
  const refreshButton = $("#refresh");
  if (force && refreshButton) {
    refreshButton.disabled = true;
    refreshButton.classList.add("is-refreshing");
  }
  try {
    const response = await fetch("/api/dashboard", {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (response.status === 401) {
      window.location.assign("/login");
      return;
    }
    if (!response.ok) throw new Error(await jsonError(response));
    const payload = await response.json();
    const previousEvent = lastEvent;
    state = {
      nodes: Array.isArray(payload.nodes) ? payload.nodes : [],
      events: Array.isArray(payload.events) ? payload.events : [],
      webhooks: Array.isArray(payload.webhooks) ? payload.webhooks : [],
      settings: payload.settings && typeof payload.settings === "object" ? payload.settings : {},
      integration_alerts: Array.isArray(payload.integration_alerts) ? payload.integration_alerts : [],
    };
    const integrationAlert = state.integration_alerts[0];
    if (integrationAlert && integrationAlert.id !== lastIntegrationAlert) {
      lastIntegrationAlert = integrationAlert.id;
      sessionStorage.setItem("remnadown-integration-alert", integrationAlert.id);
      toast(integrationAlert.message, true);
    }
    const newestEvent = Math.max(0, ...state.events.map((event) => finite(event.id) ?? 0));
    if (previousEvent && newestEvent > previousEvent && localStorage.getItem("remnadown-sound") === "on") beep();
    lastEvent = newestEvent;
    render();
    fillAppSettings();
    if (selected) {
      selected = state.nodes.find((node) => node.uuid === selected.uuid) || null;
      if (selected && $("#node-dialog").open) populateNodeDetails(selected);
    }
    if (nodeDialogScroll && nodeDialogScrollTop !== null) {
      requestAnimationFrame(() => { nodeDialogScroll.scrollTop = nodeDialogScrollTop; });
    }
  } catch (error) {
    if (!state.nodes.length) {
      $("#nodes").innerHTML = `<div class="empty">Не удалось загрузить ноды: ${esc(error.message)}</div>`;
      $("#nodes-subtitle").textContent = "Ошибка загрузки данных";
    }
    if (force) toast(`Ошибка обновления: ${error.message}`, true);
  } finally {
    if (force) {
      const remaining = 450 - (performance.now() - refreshStartedAt);
      if (remaining > 0) await new Promise((resolve) => setTimeout(resolve, remaining));
    }
    loading = false;
    if (refreshButton) {
      refreshButton.disabled = false;
      refreshButton.classList.remove("is-refreshing");
    }
    scheduleRefresh();
  }
}

async function openNode(uuid) {
  selected = state.nodes.find((node) => node.uuid === uuid);
  if (!selected) return;
  populateNodeDetails(selected, true);
  history = [];
  $("#dialog-chart").innerHTML = '<div class="empty">Загрузка истории…</div>';
  $("#node-dialog").showModal();
  await loadHistory();
}

async function loadHistory() {
  if (!selected) return;
  try {
    const response = await fetch(`/api/nodes/${encodeURIComponent(selected.uuid)}/history?days=${days}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    history = Array.isArray(payload.samples) ? payload.samples : [];
    drawChart();
  } catch (error) {
    $("#dialog-chart").innerHTML = `<div class="empty">Не удалось загрузить историю: ${esc(error.message)}</div>`;
  }
}

function sampleValues(sample) {
  if (metric === "network") return [finite(sample.rx_bps), finite(sample.tx_bps)];
  return [finite(sample[metric])];
}

function metricValue(value) {
  if (value === null || value === undefined) return "—";
  if (metric === "network") return bitrate(value);
  if (metric === "online") return compactNumber(Math.round(value));
  return percent(value);
}

function chartPath(samples, series, xFor, yFor) {
  let path = "";
  let drawing = false;
  samples.forEach((sample, index) => {
    const value = sampleValues(sample)[series];
    if (value === null) {
      drawing = false;
      return;
    }
    path += `${drawing ? "L" : "M"}${xFor(index).toFixed(2)},${yFor(value).toFixed(2)} `;
    drawing = true;
  });
  return path.trim();
}

function drawChart() {
  const container = $("#dialog-chart");
  const allValues = history.flatMap(sampleValues).filter((value) => value !== null);
  if (!history.length || !allValues.length) {
    $("#chart-current").textContent = "—";
    $("#chart-max").textContent = "—";
    $("#chart-min").textContent = "—";
    container.innerHTML = '<div class="empty">История этой метрики пока не накоплена</div>';
    return;
  }
  const width = 1000;
  const height = 270;
  const margin = { top: 14, right: 18, bottom: 30, left: 76 };
  const rawMax = Math.max(...allValues);
  const rawMin = metric === "online" ? Math.min(...allValues) : 0;
  const max = rawMax === rawMin ? rawMax + 1 : rawMax;
  const min = rawMin;
  const span = max - min || 1;
  const xFor = (index) => margin.left + index * (width - margin.left - margin.right) / Math.max(1, history.length - 1);
  const yFor = (value) => margin.top + (max - value) * (height - margin.top - margin.bottom) / span;
  const firstPath = chartPath(history, 0, xFor, yFor);
  const secondPath = metric === "network" ? chartPath(history, 1, xFor, yFor) : "";
  const areaPoints = history.map((sample, index) => {
    const value = sampleValues(sample)[0];
    return value === null ? null : [xFor(index), yFor(value)];
  }).filter(Boolean);
  const areaPath = areaPoints.length > 1
    ? `M${areaPoints[0][0]},${height - margin.bottom} L${areaPoints.map(([x, y]) => `${x},${y}`).join(" L")} L${areaPoints.at(-1)[0]},${height - margin.bottom} Z`
    : "";
  const grid = Array.from({ length: 5 }, (_, index) => {
    const ratio = index / 4;
    const y = margin.top + ratio * (height - margin.top - margin.bottom);
    const value = max - ratio * span;
    return `<line class="chart-grid" x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}"/><text class="chart-axis-label" x="${margin.left - 9}" y="${y + 3}" text-anchor="end">${esc(metricValue(value))}</text>`;
  }).join("");
  const xLabels = [0, Math.floor((history.length - 1) / 2), history.length - 1].map((index) => {
    const sample = history[index];
    return `<text class="chart-axis-label" x="${xFor(index)}" y="${height - 8}" text-anchor="${index === 0 ? "start" : index === history.length - 1 ? "end" : "middle"}">${esc(formatTime(sample.created_at))}</text>`;
  }).join("");
  container.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-label="Интерактивный график">
      <defs><linearGradient id="chart-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2dd4bf" stop-opacity=".26"/><stop offset="1" stop-color="#2dd4bf" stop-opacity="0"/></linearGradient></defs>
      ${grid}${xLabels}
      ${areaPath ? `<path class="chart-area" d="${areaPath}"/>` : ""}
      <path class="chart-line" d="${firstPath}"/>
      ${secondPath ? `<path class="chart-line tx-line" d="${secondPath}"/>` : ""}
      <line class="chart-cursor" x1="0" y1="${margin.top}" x2="0" y2="${height - margin.bottom}" visibility="hidden"/>
    </svg>
    <div class="chart-tooltip" hidden></div>
  `;
  const latest = history.at(-1);
  const latestValues = sampleValues(latest);
  $("#chart-current").textContent = metric === "network"
    ? `RX ${metricValue(latestValues[0])} · TX ${metricValue(latestValues[1])}`
    : metricValue(latestValues[0]);
  $("#chart-max").textContent = metricValue(rawMax);
  $("#chart-min").textContent = metricValue(Math.min(...allValues));
  container.onpointermove = (event) => {
    const rect = container.getBoundingClientRect();
    const relative = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    const index = Math.round(relative * (history.length - 1));
    const sample = history[index];
    const values = sampleValues(sample);
    const tooltip = $(".chart-tooltip", container);
    const cursor = $(".chart-cursor", container);
    const x = margin.left + relative * (width - margin.left - margin.right);
    tooltip.hidden = false;
    tooltip.style.left = `${relative * 100}%`;
    tooltip.style.top = `${Math.max(52, event.clientY - rect.top)}px`;
    tooltip.innerHTML = metric === "network"
      ? `<b>${esc(formatTime(sample.created_at))}</b><br>RX ${esc(metricValue(values[0]))}<br>TX ${esc(metricValue(values[1]))}`
      : `<b>${esc(formatTime(sample.created_at))}</b><br>${esc(metricValue(values[0]))}`;
    cursor.setAttribute("x1", String(x));
    cursor.setAttribute("x2", String(x));
    cursor.setAttribute("visibility", "visible");
  };
  container.onpointerleave = () => {
    const tooltip = $(".chart-tooltip", container);
    const cursor = $(".chart-cursor", container);
    if (tooltip) tooltip.hidden = true;
    if (cursor) cursor.setAttribute("visibility", "hidden");
  };
}

function dpiStatus(job) {
  return String(job.result?.status || job.status || "unknown").toLowerCase();
}

function collection(payload, ...keys) {
  if (Array.isArray(payload)) return payload;
  for (const key of keys) {
    if (Array.isArray(payload?.[key])) return payload[key];
  }
  return [];
}

function money(value) {
  const number = finite(value);
  return number === null ? "—" : `$${number.toFixed(4).replace(/0+$/, "").replace(/\.$/, ".00")}`;
}

function popChoiceMarkup(pop, selected = false) { return `<label class="pop-choice ${pop.healthy === false ? "unhealthy" : ""}"><input type="checkbox" value="${Number(pop.id)}" ${selected ? "checked" : ""} ${pop.healthy === false ? "disabled" : ""}><span><b>${esc(pop.region || pop.name || `PoP ${pop.id}`)}</b><small>${esc(pop.name || "")}${pop.healthy === false ? " · недоступен" : ""}</small></span></label>`; }

function renderDpiOverview() {
  const profile = dpiOverview.profile?.data || dpiOverview.profile || {};
  const checks = dpiOverview.checks || [];
  const balance = profile.balance ?? profile.usd_balance ?? profile.balance_usd ?? profile.available_balance;
  const calculatedSpent = checks.reduce((sum, check) => sum + (finite(check.usd_cost) ?? 0), 0);
  const spent = profile.total_spent ?? profile.spent_usd ?? profile.usd_spent ?? calculatedSpent;
  $("#dpi-balance").textContent = money(balance);
  $("#dpi-spent").textContent = money(spent);
  $("#dpi-expenses").innerHTML = checks.length ? checks.slice(0, 10).map((check) => { const node = state.nodes.find((item) => item.uuid === check.node_uuid); return `
    <div class="dpi-expense-row">
      ${flag(node?.country_code || check.node_country_code)}<span><b>${esc(node?.name || check.node_name || "Удалённая нода")}</b><small>${formatTime(check.created_at)}</small></span>
      <strong>${money(check.usd_cost ?? check.cost ?? check.estimated_cost)}</strong>
    </div>
  `; }).join("") : '<div class="empty">Расходов пока нет</div>';
  $("#russia-pops").innerHTML = dpiOverview.pops.length ? dpiOverview.pops.map((pop) => popChoiceMarkup(pop)).join("") : '<span class="muted">Регионы РФ не получены от DPI API</span>';
  const scheduled = state.settings.dpi_schedule_pop_ids || [];
  $("#schedule-pops").innerHTML = dpiOverview.pops.map((pop) => popChoiceMarkup(pop, scheduled.includes(Number(pop.id)))).join("");
  if (selected) { $("#node-regions").innerHTML = dpiOverview.pops.map((pop) => popChoiceMarkup(pop, (selected.dpi_pop_ids || []).includes(Number(pop.id)))).join(""); filterNodeRegions(); }
  filterRegions();
  filterScheduleRegions();
}

function availabilityVerdict(result) {
  if (result.is_direct === true) return null;
  if (result.connected === true) return true;
  if (result.connected === false && result.untestable !== true) return false;
  if (result.accessible === true) return true;
  if (result.accessible === false) return false;
  return null;
}

function filterRegions() {
  const query = $("#region-search").value.trim().toLowerCase();
  $$("#russia-pops label").forEach((label) => {
    label.hidden = Boolean(query) && !label.textContent.toLowerCase().includes(query);
  });
  const count = checked("#russia-pops").length;
  $("#region-picker-count").textContent = count ? `Выбрано: ${count}` : "Оптимальный набор";
}

function filterScheduleRegions() {
  const query = $("#schedule-region-search").value.trim().toLowerCase();
  $$("#schedule-pops label").forEach((label) => { label.hidden = Boolean(query) && !label.textContent.toLowerCase().includes(query); });
  const count = checked("#schedule-pops").length;
  $("#schedule-region-count").textContent = count ? `Выбрано: ${count}` : "Оптимальный набор";
}
function filterNodeRegions() { const query = $("#node-region-search").value.trim().toLowerCase(); $$("#node-regions label").forEach((label) => { label.hidden = Boolean(query) && !label.textContent.toLowerCase().includes(query); }); const count = checked("#node-regions").length; $("#node-region-count").textContent = count ? `Выбрано: ${count}` : "Оптимальный набор"; }

async function loadDpiOverview() {
  if (!dpiAvailable()) return;
  try {
    const response = await fetch("/api/dpi/overview", { cache: "no-store" });
    if (!response.ok) throw new Error(await jsonError(response));
    const payload = await response.json();
    dpiOverview = {
      profile: payload.profile || {},
      checks: collection(payload.checks, "checks", "items", "results", "data"),
      pops: collection(payload.pops, "pops", "items", "results", "data"),
      stats: collection(payload.stats, "stats", "items", "data"),
    };
    renderDpiOverview();
  } catch (error) {
    $("#dpi-expenses").innerHTML = `<div class="empty">Не удалось загрузить данные аккаунта: ${esc(error.message)}</div>`;
  }
}

async function loadDpi() {
  if (!dpiAvailable()) return;
  const sequence = ++dpiLoadSequence;
  clearTimeout(dpiTimer);
  dpiTimer = null;
  try {
    const response = await fetch("/api/dpi/history", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const jobs = await response.json();
    if (sequence !== dpiLoadSequence) return;
    $("#dpi-result").innerHTML = jobs.length ? jobs.map((job) => {
      const node = state.nodes.find((item) => item.uuid === job.node_uuid) || {
        name: job.node_name,
        country_code: job.node_country_code,
        address: job.node_address,
      };
      const result = job.result || {};
      const progress = result.progress || {};
      const status = dpiStatus(job);
      const percentDone = finite(progress.percent) ?? (["completed", "failed", "cancelled", "timeout"].includes(status) ? 100 : 0);
      const region = REGIONS[job.location]?.name || job.location;
      const resultItems = collection(result.results, "items", "results", "data").filter((item) => item.is_direct !== true);
      const hasResultError = Boolean(result.error) || ["failed", "cancelled", "timeout"].includes(status);
      const note = hasResultError
        ? "Подробности ошибки доступны в полной информации"
        : result.timed_out
          ? "API превысил ожидаемое время, результаты могут продолжать поступать"
          : `${progress.done ?? 0} из ${progress.total ?? "—"} точек`;
      const available = resultItems.filter((item) => availabilityVerdict(item) === true).length;
      const unavailable = resultItems.filter((item) => availabilityVerdict(item) === false).length;
      const resultClass = hasResultError ? "error" : unavailable && available ? "partial" : unavailable ? "blocked" : available ? "available" : "unknown";
      const resultLabel = hasResultError ? "Ошибка" : resultItems.length ? `${available} доступно · ${unavailable} недоступно` : "Результат ожидается";
      const resultSummary = hasResultError
        ? esc(resultLabel)
        : resultItems.length
          ? `<span class="dpi-available-count">${available} доступно</span><span class="dpi-result-separator">·</span><span class="dpi-unavailable-count">${unavailable} недоступно</span>`
          : esc(resultLabel);
      const statusLabel = hasResultError ? "Ошибка" : status;
      const popNames = [...new Set(resultItems.map((item) => item.pop_name || item.region || item.city).filter(Boolean))];
      const checkLabel = popNames.length ? popNames.join(", ") : (REGIONS[job.location]?.name || job.location);
      return `
        <div class="dpi-job">
          <div class="dpi-job-head">${flag(node.country_code)}<span class="dpi-job-node"><b>${esc(node.name || "Нода")}</b><small>${esc(job.target || node.address || "—")} · ${esc(checkLabel)}</small></span><span class="dpi-result-state ${resultClass}" title="${esc(resultLabel)}"><i></i>${resultSummary}</span><span class="dpi-job-status">${esc(statusLabel)}</span></div>
          <div class="progress-track ${["active", "pending"].includes(status) ? "is-running" : ""}"><i style="width:${Math.max(3, Math.min(100, percentDone))}%"></i></div>
          <div class="dpi-job-foot"><span>${esc(note)}</span><time>${formatTime(job.updated_at || job.created_at)}</time></div>
          <details class="dpi-json"><summary>Открыть полную информацию</summary><div class="dpi-detail-grid"><span><small>Нода</small><b>${esc(node.name || "—")}</b></span><span><small>IP</small><b>${esc(job.target || "—")}</b></span><span><small>Регион</small><b>${esc(checkLabel)}</b></span><span><small>Стоимость</small><b>${money(result.usd_cost || result.estimated_cost || 0)}</b></span></div><pre>${jsonHighlight(result)}</pre></details>
        </div>
      `;
    }).join("") : '<div class="empty">Проверок пока нет</div>';
    clearTimeout(dpiTimer);
    if (jobs.some((job) => ["active", "pending"].includes(dpiStatus(job)))) {
      dpiTimer = setTimeout(loadDpi, 5000);
    }
  } catch (error) {
    if (sequence !== dpiLoadSequence) return;
    $("#dpi-result").innerHTML = `<div class="empty">Не удалось загрузить проверки: ${esc(error.message)}</div>`;
  }
}

async function loadLogs() {
  const query = $("#log-search").value.trim();
  const level = $("#log-level").value;
  $("#logs-list").innerHTML = '<div class="empty">Загрузка логов…</div>';
  try {
    const response = await fetch(`/api/logs?level=${encodeURIComponent(level)}&query=${encodeURIComponent(query)}`, { cache: "no-store" });
    if (!response.ok) throw new Error(await jsonError(response));
    const logs = await response.json();
    $("#logs-list").innerHTML = logs.length ? logs.map((item) => `<article class="log-row ${esc(item.level.toLowerCase())}"><div class="log-head"><span class="log-level">${esc(item.level)}</span><b>${esc(item.source)}</b><time>${formatTime(item.created_at)}</time></div><p>${esc(item.message)}</p>${item.details ? `<details><summary>Подробности</summary><pre>${highlightedText(item.details)}</pre></details>` : ""}</article>`).join("") : '<div class="empty">Логов по выбранному фильтру нет</div>';
  } catch (error) { $("#logs-list").innerHTML = `<div class="empty">Не удалось загрузить логи: ${esc(error.message)}</div>`; }
}
let activeDomainProvider = "";
function renderDomains() { const box = $("#domains-list"), query = $("#domain-search").value.trim().toLowerCase(); const items = domainData.filter((item) => { const domainMatch = [item.zone, ...(item.hosts || [])].some((value) => String(value).toLowerCase().includes(query)); const nodeMatch = (item.nodes || []).some((node) => [node.name, node.address].some((value) => String(value || "").toLowerCase().includes(query))); item._nodeMatch = Boolean(query && nodeMatch); return !query || domainMatch || nodeMatch; }); box.innerHTML = items.length ? items.map((item) => { const providerIcon = item.provider === "Cloudflare" ? "cloudflare" : item.provider === "REG.RU" ? "regru" : ""; return `<details class="domain-card" ${item._nodeMatch ? "open" : ""}><summary><span class="provider-badge">${providerIcon ? `<img src="/api/integration-icon/${providerIcon}" alt="">` : ""}${esc(item.provider || "Не найден")}</span><div><b>${esc(item.zone)}</b><small>${item.hosts.map(esc).join(' · ')}</small></div><strong>${item.hosts.length}</strong></summary><div class="domain-nodes">${(item.nodes || []).length ? item.nodes.map((node) => `<span>${flag(node.country_code)}<b>${esc(node.name)}</b><small>${esc(node.address || "IP не указан")}</small></span>`).join('') : '<span>Нода не привязана</span>'}</div></details>`; }).join('') : '<div class="empty">Ничего не найдено</div>'; }
async function loadDomains() { const box = $("#domains-list"); box.innerHTML = '<div class="empty">Сопоставление доменов…</div>'; try { const response = await fetch('/api/domains', { cache: 'no-store' }); if (!response.ok) throw new Error(await jsonError(response)); const data = await response.json(); domainData = activeDomainProvider ? data.domains.filter((item) => item.provider === activeDomainProvider) : data.domains; $("#domains-title").textContent = activeDomainProvider || "DNS интеграции"; renderDomains(); } catch (error) { box.innerHTML = `<div class="empty">${esc(error.message)}</div>`; } }

function toast(message, bad = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("bad", bad);
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), 2800);
}

async function beep() {
  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return;
    notificationAudioContext ||= new AudioContextClass();
    const context = notificationAudioContext;
    if (context.state === "suspended") await context.resume();
    const duration = Math.max(0.15, Math.min(2, Number(localStorage.getItem("remnadown-sound-duration") || 0.4)));
    const sound = localStorage.getItem("remnadown-sound-type") || "signal";
    const patterns = {
      signal: [[690, 690, "triangle", 0.09]],
      double: [[620, 620, "triangle", 0.09], [820, 820, "triangle", 0.09]],
      soft: [[440, 440, "sine", 0.055]],
      alarm: [[880, 880, "square", 0.09], [660, 660, "square", 0.09], [880, 880, "square", 0.09]],
      siren: [[520, 1120, "sawtooth", 0.1], [1120, 520, "sawtooth", 0.1]],
      raid: [[180, 760, "sawtooth", 0.11], [760, 180, "sawtooth", 0.11]],
      klaxon: [[390, 390, "square", 0.11], [310, 310, "square", 0.11], [390, 390, "square", 0.11]],
      pulse: [[980, 980, "square", 0.1], [540, 540, "square", 0.08], [980, 980, "square", 0.1], [540, 540, "square", 0.08]],
      emergency: [[740, 980, "sawtooth", 0.1], [980, 740, "sawtooth", 0.1], [520, 520, "square", 0.11]],
    };
    const pattern = patterns[sound] || patterns.signal;
    pattern.forEach(([startFrequency, endFrequency, oscillatorType, level], index) => {
      const oscillator = context.createOscillator(), gain = context.createGain();
      const part = duration / pattern.length, start = context.currentTime + index * part;
      oscillator.type = oscillatorType;
      oscillator.connect(gain); gain.connect(context.destination);
      oscillator.frequency.setValueAtTime(startFrequency, start);
      oscillator.frequency.linearRampToValueAtTime(endFrequency, start + part * 0.88);
      const volume = Math.max(0, Math.min(1, Number(localStorage.getItem("remnadown-sound-volume") || 60) / 100));
      gain.gain.setValueAtTime(Math.max(0.0001, level * volume), start);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + part * 0.9);
      oscillator.start(start); oscillator.stop(start + part);
    });
  } catch {
    // Browser sound is optional; failing silently keeps monitoring operational.
  }
}

async function jsonError(response) {
  try {
    const payload = await response.json();
    return payload.detail || `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}

function closePopovers(except = null) {
  ["search", "filter"].forEach((name) => {
    if (name === except) return;
    const popover = $(`#${name}-popover`);
    const trigger = $(`#${name}-trigger`);
    popover.hidden = true;
    trigger.classList.remove("active");
    trigger.setAttribute("aria-expanded", "false");
  });
}

function bindPopover(name) {
  const control = $(`#${name}-control`);
  const trigger = $(`#${name}-trigger`);
  const popover = $(`#${name}-popover`);
  trigger.addEventListener("click", (event) => {
    event.stopPropagation();
    const opening = popover.hidden;
    closePopovers(opening ? name : null);
    popover.hidden = !opening;
    trigger.classList.toggle("active", opening);
    trigger.setAttribute("aria-expanded", String(opening));
    if (opening && name === "search") requestAnimationFrame(() => $("#search").focus());
  });
  control.addEventListener("click", (event) => event.stopPropagation());
}

$$('.nav-item').forEach((button) => {
  button.addEventListener("click", () => {
    $$(".nav-item").forEach((item) => item.classList.remove("active"));
    $$(".view").forEach((view) => view.classList.remove("active"));
    button.classList.add("active");
    $(`#${button.dataset.view}`).classList.add("active");
    if (button.dataset.view === "events") markEventsRead();
    document.body.classList.remove("sidebar-open");
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (button.dataset.view === "dpi-checker") {
      const first = checked("#dpi-node-options")[0];
      selected = state.nodes.find((node) => node.uuid === first) || selected || state.nodes[0] || null;
      Promise.all([loadDpiOverview(), loadDpi()]);
    }
    if (button.dataset.view === "logs") loadLogs();
    if (button.dataset.view === "domains") { activeDomainProvider = button.dataset.domainProvider || ""; loadDomains(); }
  });
});

bindPopover("search");
bindPopover("filter");
document.addEventListener("click", () => closePopovers());
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closePopovers();
});

$("#menu-toggle").addEventListener("click", () => document.body.classList.toggle("sidebar-open"));
$("#sidebar-backdrop").addEventListener("click", () => document.body.classList.remove("sidebar-open"));
$("#search").addEventListener("input", renderNodes);
$("#status-filter").addEventListener("change", renderNodes);
$("#country-filter").addEventListener("change", renderNodes);
$("#node-sort").addEventListener("change", renderNodes);
$("#event-search").addEventListener("input", renderEvents);
$("#event-filter").addEventListener("change", renderEvents);
$("#clear-events").addEventListener("click", async (event) => {
  if (!state.events.length) {
    toast("Событий для очистки нет");
    return;
  }
  if (!window.confirm("Очистить всю историю событий?")) return;
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const response = await fetch("/api/events", {
      method: "DELETE",
      headers: { "X-CSRF-Token": csrf },
    });
    if (!response.ok) throw new Error(await jsonError(response));
    state.events = [];
    renderEvents();
    toast("История событий очищена");
  } catch (error) {
    toast(`Не удалось очистить события: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
});
$("#log-level").addEventListener("change", loadLogs);
$("#log-search").addEventListener("input", loadLogs);
$("#refresh-logs").addEventListener("click", loadLogs);
$("#refresh-domains").addEventListener("click", () => loadDomains(true));
$("#domain-search").addEventListener("input", renderDomains);
$("#refresh").addEventListener("click", () => load({ force: true }));

$("#nodes").addEventListener("click", (event) => {
  const row = event.target.closest("[data-node]");
  if (row) openNode(row.dataset.node);
});

$(".dialog-close").addEventListener("click", () => $("#node-dialog").close());
$("#open-node-dpi").addEventListener("click", () => {
  if (!selected) return;
  $$("#dpi-node-options input").forEach((input) => { input.checked = input.value === selected.uuid; });
  updateDpiNodeCount();
  $("#node-dialog").close();
  $('.nav-item[data-view="dpi-checker"]').click();
});
$("#node-dialog").addEventListener("click", (event) => {
  if (event.target === $("#node-dialog")) $("#node-dialog").close();
});
$("#node-dialog").addEventListener("close", () => {
  clearTimeout(dpiTimer);
  dpiTimer = null;
});
$$('[data-node-panel]').forEach((button) => button.addEventListener("click", () => {
  const panel = button.dataset.nodePanel;
  $$('[data-node-panel]').forEach((item) => item.classList.toggle("active", item === button));
  $$('.node-panel').forEach((item) => item.classList.toggle("active", item.dataset.panel === panel));
}));

$$(".range-tabs button").forEach((button) => {
  button.addEventListener("click", async () => {
    $$(".range-tabs button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    days = Number(button.dataset.days);
    await loadHistory();
  });
});

$$(".metric-tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    $$(".metric-tabs button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    metric = button.dataset.metric;
    drawChart();
  });
});

$("#node-settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selected) return;
  const form = event.currentTarget;
  const body = {
    drop_percent: Number(form.drop_percent.value),
    retention_days: Number(form.retention_days.value),
    poll_interval_seconds: form.poll_interval_seconds.value ? Number(form.poll_interval_seconds.value) : null,
    webhooks_enabled: form.webhooks_enabled.checked,
    auto_dpi: form.auto_dpi.checked,
    vless_mode: form.vless_mode.value,
    vless_key: form.vless_key.value.trim(),
    vless_username: form.vless_username.value.trim(),
    action_chain: $$("input[type='checkbox']", $("#node-action-chain")).map((input) => ({ type: input.value, enabled: input.checked })),
    dpi_squad_uuids: [],
    auto_dns_replace: $('#node-action-chain input[value="replace_dns"]').checked,
    cpu_threshold: Number(form.cpu_threshold.value || 0), ram_threshold: Number(form.ram_threshold.value || 0),
    rx_threshold_mbps: Number(form.rx_threshold_mbps.value || 0), tx_threshold_mbps: Number(form.tx_threshold_mbps.value || 0),
    dpi_locations: ["russia"],
    dpi_pop_ids: checked("#node-regions").map(Number),
    webhook_ids: checked("#node-hooks").map(Number),
    csrf_token: csrf,
  };
  const response = await fetch(`/api/nodes/${encodeURIComponent(selected.uuid)}/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.ok) {
    const { csrf_token: _csrfToken, ...savedSettings } = body;
    Object.assign(selected, {
      ...savedSettings,
      webhook_ids: body.webhook_ids,
      vless_key_configured: body.vless_key ? 1 : selected.vless_key_configured,
    });
    toast("Настройки ноды сохранены");
  } else {
    toast(await jsonError(response), true);
  }
});

$("#ip-pool-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selected) return;
  const form = event.currentTarget;
  const button = $("button[type='submit']", form);
  button.disabled = true;
  button.textContent = "Проверка 15 секунд…";
  const response = await fetch(`/api/nodes/${encodeURIComponent(selected.uuid)}/ips`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address: form.address.value.trim(), priority: Number(form.priority.value), csrf_token: csrf }),
  });
  button.disabled = false;
  button.textContent = "Добавить и проверить";
  if (response.ok) {
    const result = await response.json();
    toast(result.valid ? "IP привязан к ноде" : `IP не прошёл проверку: ${result.error}`, !result.valid);
    form.address.value = "";
    await load({ force: true });
    if (selected) renderIpPool(selected);
  } else {
    toast(await jsonError(response), true);
  }
});
$("#toggle-ip-add").addEventListener("click", () => { const form = $("#ip-pool-form"); form.hidden = !form.hidden; $("#ip-check-warning").hidden = form.hidden; if (!form.hidden) form.address.focus(); });

let draggedIp = null;
$("#ip-pool-list").addEventListener("dragstart", (event) => { draggedIp = event.target.closest("[data-ip-id]"); if (draggedIp) draggedIp.classList.add("dragging"); });
$("#ip-pool-list").addEventListener("dragover", (event) => { event.preventDefault(); const target = event.target.closest("[data-ip-id]"); if (draggedIp && target && target !== draggedIp) { const rect = target.getBoundingClientRect(); target.parentNode.insertBefore(draggedIp, event.clientY < rect.top + rect.height / 2 ? target : target.nextSibling); } });
$("#ip-pool-list").addEventListener("dragend", async () => { if (!draggedIp || !selected) return; draggedIp.classList.remove("dragging"); draggedIp = null; const ids = $$("[data-ip-id]", $("#ip-pool-list")).map((item) => Number(item.dataset.ipId)); await fetch(`/api/nodes/${encodeURIComponent(selected.uuid)}/ips/actions/reorder`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids, csrf_token: csrf }) }); await load({ force: true }); });

$("#ip-pool-list").addEventListener("click", async (event) => {
  if (!selected) return;
  const saveButton = event.target.closest("[data-recheck-ip]");
  const deleteButton = event.target.closest("[data-delete-ip]");
  const activateButton = event.target.closest("[data-activate-ip]");
  if (!saveButton && !deleteButton && !activateButton) return;
  if (deleteButton && !window.confirm("Удалить резервный IP из списка?")) return;
  if (activateButton) {
    pendingActivateId = Number(activateButton.dataset.activateIp);
    const item = (selected.ip_pool || []).find((entry) => entry.id === pendingActivateId);
    $("#activate-ip-address").textContent = item?.address || "—";
    $("#activate-ip-form").reset();
    $("#activate-ip-dialog").showModal();
    return;
  }
  const actionButton = saveButton || deleteButton;
  const ipId = actionButton.dataset.recheckIp || actionButton.dataset.deleteIp;
  const response = saveButton
    ? await fetch(`/api/nodes/${encodeURIComponent(selected.uuid)}/ips/${ipId}/recheck`, { method: "POST", headers: { "X-CSRF-Token": csrf } })
    : await fetch(`/api/nodes/${encodeURIComponent(selected.uuid)}/ips/${ipId}`, { method: "DELETE", headers: { "X-CSRF-Token": csrf } });
  if (response.ok) {
    await load({ force: true });
    if (selected) renderIpPool(selected);
  } else {
    toast(await jsonError(response), true);
  }
});

$$('[data-close-ip-dialog]').forEach((button) => button.addEventListener("click", () => { pendingActivateId = null; $("#activate-ip-dialog").close(); }));
$("#activate-ip-form").addEventListener("submit", async (event) => { event.preventDefault(); if (!selected || !pendingActivateId) return; const form = event.currentTarget, confirmIp = form.confirm_ip.value, confirmDns = form.confirm_dns.value; if (!confirmIp || !confirmDns) { toast("Ответьте на оба вопроса", true); return; } if (confirmIp !== "yes") { toast("Установка IP отменена"); pendingActivateId = null; $("#activate-ip-dialog").close(); return; } const response = await fetch(`/api/nodes/${encodeURIComponent(selected.uuid)}/ips/${pendingActivateId}/activate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ update_domains: confirmDns === "yes", csrf_token: csrf }) }); if (!response.ok) { toast(await jsonError(response), true); return; } const result = await response.json(); $("#activate-ip-dialog").close(); pendingActivateId = null; toast(result.domains_changed ? `IP установлен, обновлено доменов: ${result.domains_changed}` : "IP ноды установлен"); await load({ force: true }); if (selected) renderIpPool(selected); });

$("#save-ip-rotation").addEventListener("click", async () => {
  if (!selected) return;
  const response = await fetch(`/api/nodes/${encodeURIComponent(selected.uuid)}/ip-rotation`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: $('#node-action-chain input[value="rotate_ip"]').checked, max_replacements: Number($("#max-ip-replacements").value), csrf_token: csrf }),
  });
  if (response.ok) {
    selected.auto_ip_replace = $('#node-action-chain input[value="rotate_ip"]').checked;
    selected.max_ip_replacements = Number($("#max-ip-replacements").value);
    toast("Лимит замен сохранён");
  } else {
    toast(await jsonError(response), true);
  }
});

$("#dpi-run").addEventListener("click", async () => {
  const nodeUuids = checked("#dpi-node-options");
  if (!nodeUuids.length) { toast("Выберите хотя бы одну ноду", true); return; }
  selected = state.nodes.find((node) => node.uuid === nodeUuids[0]) || null;
  const locations = ["russia"];
  const button = $("#dpi-run");
  button.disabled = true;
  button.textContent = "Запуск…";
  const responses = await Promise.all(nodeUuids.map((nodeUuid) => fetch(`/api/nodes/${encodeURIComponent(nodeUuid)}/dpi`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ locations, pop_ids: checked("#russia-pops").map(Number), kind: $("#dpi-check-kind").value, csrf_token: csrf }) })));
  button.disabled = false;
  button.textContent = "Запустить проверку";
  const failed = responses.find((response) => !response.ok);
  if (!failed) {
    toast("DPI-проверки поставлены в очередь");
    await Promise.all([loadDpiOverview(), loadDpi()]);
  } else {
    toast(await jsonError(failed), true);
  }
});

$("#region-search").addEventListener("input", filterRegions);
$("#russia-pops").addEventListener("change", filterRegions);
$("#schedule-region-search").addEventListener("input", filterScheduleRegions);
$("#schedule-pops").addEventListener("change", filterScheduleRegions);
$("#schedule-nodes").addEventListener("change", updateScheduleNodeCount);
$("#node-region-search").addEventListener("input", filterNodeRegions);
$("#node-regions").addEventListener("change", filterNodeRegions);
$("#dpi-node-options").addEventListener("change", () => { updateDpiNodeCount(); const first = checked("#dpi-node-options")[0]; selected = state.nodes.find((node) => node.uuid === first) || null; if (selected) loadDpi(); });
$("#dpi-schedule-mode").addEventListener("change", syncScheduleMode);
$("#dpi-schedule-enabled").addEventListener("change", syncScheduleEnabled);
$$('#node-action-chain input[type="checkbox"]').forEach((input) => input.addEventListener("change", syncAutomationOptions));

$("#app-settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = {
    history_days: Number(form.history_days.value),
    poll_interval_seconds: Number(form.poll_interval_seconds.value),
    timezone: form.timezone.value.trim(),
    remnawave_api_version: form.remnawave_api_version.value,
    dpi_schedule_enabled: form.dpi_schedule_enabled.checked,
    dpi_schedule_mode: form.dpi_schedule_mode.value,
    dpi_schedule_time: form.dpi_schedule_time.value,
    dpi_interval_minutes: Number(form.dpi_interval_minutes.value),
    dpi_schedule_nodes: checked("#schedule-nodes"),
    dpi_schedule_locations: ["russia"],
    dpi_schedule_pop_ids: checked("#schedule-pops").map(Number),
    dpi_weekly_schedule: $$('[data-weektime]').map((input) => ({ day: Number(input.dataset.weektime), enabled: true, time: input.value })),
    dpi_api_key: form.dpi_api_key.value.trim(),
    dpi_infra_billing_enabled: form.dpi_infra_billing_enabled.checked,
    dpi_balance_threshold: Number(form.dpi_balance_threshold.value),
    telegram_enabled: form.telegram_enabled.checked,
    telegram_bot_token: form.telegram_bot_token.value.trim(),
    telegram_chat_id: form.telegram_chat_id.value.trim(),
    telegram_events_topic: form.telegram_events_topic.value.trim(),
    telegram_auth_topic: form.telegram_auth_topic.value.trim(),
    telegram_event_types: checked("#telegram-event-types"),
    regru_username: form.regru_username.value.trim(),
    regru_password: form.regru_password.value,
    cloudflare_api_token: form.cloudflare_api_token.value.trim(),
    regru_ttl: Number(form.regru_ttl.value || 600),
    cloudflare_ttl: Number(form.cloudflare_ttl.value || 600),
    csrf_token: csrf,
  };
  const response = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.ok) {
    toast("Настройки сохранены");
    settingsHydrated = false;
    await load({ force: true });
    fillAppSettings(true);
  } else {
    toast(await jsonError(response), true);
  }
});

$("#webhook-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const response = await fetch("/api/webhooks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: form.name.value,
      url: form.url.value,
      secret: form.secret.value,
      events: checked("#new-webhook-events"),
      csrf_token: csrf,
    }),
  });
  if (response.ok) {
    form.reset();
    toast("Вебхук добавлен");
    await load({ force: true });
  } else {
    toast(await jsonError(response), true);
  }
});

$("#hooks-list").addEventListener("click", async (event) => {
  const saveEvents = event.target.closest("[data-save-hook-events]");
  if (saveEvents) {
    const row = saveEvents.closest("[data-hook-id]");
    const response = await fetch(`/api/webhooks/${row.dataset.hookId}/events`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: $("[data-hook-enabled]", row).checked, events: $$('input[type="checkbox"][value]', row).filter((item) => item.checked).map((item) => item.value), csrf_token: csrf }) });
    toast(response.ok ? "События вебхука сохранены" : await jsonError(response), !response.ok);
    if (response.ok) await load({ force: true });
    return;
  }
  const button = event.target.closest("[data-delete-hook]");
  if (!button) return;
  event.preventDefault();
  event.stopPropagation();
  const webhookId = Number(button.dataset.deleteHook);
  button.disabled = true;
  try {
    const response = await fetch(`/api/webhooks/${webhookId}/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csrf_token: csrf }),
    });
    if (!response.ok) throw new Error(await jsonError(response));
    state.webhooks = state.webhooks.filter((hook) => Number(hook.id) !== webhookId);
    state.nodes.forEach((node) => {
      node.webhook_ids = (node.webhook_ids || []).filter((id) => Number(id) !== webhookId);
    });
    renderHooks();
    if (selected && $("#node-dialog").open) populateNodeDetails(selected, true);
    toast("Вебхук удалён");
  } catch (error) {
    button.disabled = false;
    toast(`Не удалось удалить вебхук: ${error.message}`, true);
  }
});

$("#hooks-list").addEventListener("pointerdown", (event) => {
  if (event.target.closest("[data-delete-hook]")) event.stopPropagation();
});

$("#hooks-list").addEventListener("change", async (event) => {
  const toggle = event.target.closest("[data-hook-enabled]");
  if (!toggle) return;
  const row = toggle.closest("[data-hook-id]");
  const hookId = Number(row.dataset.hookId);
  const enabled = toggle.checked;
  toggle.disabled = true;
  try {
    const events = $$("input[type='checkbox'][value]", row)
      .filter((item) => item.checked)
      .map((item) => item.value);
    const response = await fetch(`/api/webhooks/${hookId}/events`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, events, csrf_token: csrf }),
    });
    if (!response.ok) throw new Error(await jsonError(response));
    const hook = state.webhooks.find((item) => Number(item.id) === hookId);
    if (hook) hook.enabled = enabled;
    toast(enabled ? "Вебхук включён" : "Вебхук отключён");
  } catch (error) {
    toggle.checked = !enabled;
    toast(`Не удалось изменить вебхук: ${error.message}`, true);
  } finally {
    toggle.disabled = false;
  }
});

$("#events-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy-json]");
  if (button) navigator.clipboard.writeText(button.dataset.copyJson).then(() => toast("JSON скопирован"));
});


function syncSoundButton() {
  const enabled = localStorage.getItem("remnadown-sound") === "on";
  const button = $("#sound-toggle");
  button.classList.toggle("sound-muted", !enabled);
  button.setAttribute("aria-pressed", String(enabled));
  button.setAttribute("aria-label", enabled ? "Выключить звуковые уведомления" : "Включить звуковые уведомления");
}

$("#sound-toggle").addEventListener("click", () => {
  const enabled = localStorage.getItem("remnadown-sound") !== "on";
  localStorage.setItem("remnadown-sound", enabled ? "on" : "off");
  syncSoundButton();
  if (enabled) beep();
});
$("#support-project").addEventListener("click", () => $("#support-dialog").showModal());
$("#support-dialog .dialog-close").addEventListener("click", () => $("#support-dialog").close());
$("#support-dialog").addEventListener("click", (event) => { const wallet = event.target.closest("[data-wallet]"); if (wallet) navigator.clipboard.writeText(wallet.dataset.wallet).then(() => toast("Адрес скопирован")); else if (event.target === event.currentTarget) event.currentTarget.close(); });
$("#vless-mode").addEventListener("change", syncVlessSource);
$("#change-dpi-token").addEventListener("click", () => { $("#dpi-token-dialog").showModal(); requestAnimationFrame(() => $("#dpi-token-dialog input").focus()); });
$$('[data-close-dpi-token]').forEach((button) => button.addEventListener('click', () => $("#dpi-token-dialog").close()));
$("#dpi-token-form").addEventListener("submit", (event) => { event.preventDefault(); $("#dpi-token-dialog").close(); $("#app-settings").requestSubmit(); });

if (localStorage.getItem("remnadown-sound-type") === "custom") localStorage.setItem("remnadown-sound-type", "signal");
$("#notification-sound").value = localStorage.getItem("remnadown-sound-type") || "signal";
$("#notification-sound-duration").value = localStorage.getItem("remnadown-sound-duration") || "0.4";
$("#notification-sound-volume").value = localStorage.getItem("remnadown-sound-volume") || "60";
$("#sound-volume-value").textContent = `${$("#notification-sound-volume").value}%`;
function syncSoundFields() { $("#sound-duration-field").hidden = false; }
$("#notification-sound").addEventListener("change", (event) => localStorage.setItem("remnadown-sound-type", event.target.value));
$("#notification-sound-duration").addEventListener("change", (event) => localStorage.setItem("remnadown-sound-duration", event.target.value));
$("#notification-sound-volume").addEventListener("input", (event) => { localStorage.setItem("remnadown-sound-volume", event.target.value); $("#sound-volume-value").textContent = `${event.target.value}%`; });
$("#preview-sound").addEventListener("click", () => {
  localStorage.setItem("remnadown-sound-type", $("#notification-sound").value);
  localStorage.setItem("remnadown-sound-duration", $("#notification-sound-duration").value);
  beep();
});
$("#new-webhook-events").innerHTML = WEBHOOK_EVENTS.map((name) => `<label><input type="checkbox" value="${name}" checked><span>${name}</span></label>`).join("");
$$('input', $("#new-webhook-events")).forEach((input) => input.addEventListener("change", () => { const count = checked("#new-webhook-events").length; $("#new-webhook-event-count").textContent = count === WEBHOOK_EVENTS.length ? "Все события" : `${count} из ${WEBHOOK_EVENTS.length}`; }));
$$('.event-reference pre, .webhook-help-popover pre').forEach((pre) => { try { pre.innerHTML = jsonHighlight(JSON.parse(pre.textContent)); } catch { pre.innerHTML = highlightedText(pre.textContent); } });
syncSoundFields();

syncSoundButton();
load({ force: true });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimeout(refreshTimer);
  } else {
    load({ force: true });
  }
});
