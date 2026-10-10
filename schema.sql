CREATE TABLE IF NOT EXISTS nodes (
  uuid TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL DEFAULT '', country_code TEXT NOT NULL DEFAULT '',
  provider_name TEXT NOT NULL DEFAULT '', provider_icon TEXT NOT NULL DEFAULT '', cpu_count INTEGER,
  cpu_model TEXT NOT NULL DEFAULT '', total_ram INTEGER, node_version TEXT NOT NULL DEFAULT '',
  xray_version TEXT NOT NULL DEFAULT '', uptime TEXT NOT NULL DEFAULT '', view_position INTEGER NOT NULL DEFAULT 0, last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT, node_uuid TEXT NOT NULL REFERENCES nodes(uuid) ON DELETE CASCADE,
  online INTEGER NOT NULL, connected INTEGER NOT NULL, cpu_percent REAL, ram_percent REAL,
  load_1 REAL, load_5 REAL, load_15 REAL, rx_bps REAL, tx_bps REAL, rx_total INTEGER, tx_total INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_node_time ON samples(node_uuid, created_at DESC);
CREATE TABLE IF NOT EXISTS node_settings (
  node_uuid TEXT PRIMARY KEY REFERENCES nodes(uuid) ON DELETE CASCADE,
  drop_percent REAL, webhooks_enabled INTEGER NOT NULL DEFAULT 1, auto_dpi INTEGER NOT NULL DEFAULT 0,
  retention_days INTEGER, poll_interval_seconds INTEGER, dpi_locations TEXT NOT NULL DEFAULT '["russia"]', auto_dns_replace INTEGER NOT NULL DEFAULT 0,
  cpu_threshold REAL NOT NULL DEFAULT 0, ram_threshold REAL NOT NULL DEFAULT 0, rx_threshold_mbps REAL NOT NULL DEFAULT 0, tx_threshold_mbps REAL NOT NULL DEFAULT 0,
  dpi_pop_ids TEXT NOT NULL DEFAULT '[]', vless_mode TEXT NOT NULL DEFAULT 'temporary',
  vless_key TEXT NOT NULL DEFAULT '', vless_username TEXT NOT NULL DEFAULT '', action_chain TEXT NOT NULL DEFAULT '[]',
  dpi_squad_uuids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS node_state (
  node_uuid TEXT PRIMARY KEY REFERENCES nodes(uuid) ON DELETE CASCADE,
  is_down INTEGER NOT NULL DEFAULT 0, bad_count INTEGER NOT NULL DEFAULT 0,
  good_count INTEGER NOT NULL DEFAULT 0, is_unavailable INTEGER NOT NULL DEFAULT 0, cpu_alert INTEGER NOT NULL DEFAULT 0,
  ram_alert INTEGER NOT NULL DEFAULT 0, rx_alert INTEGER NOT NULL DEFAULT 0, tx_alert INTEGER NOT NULL DEFAULT 0, changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, node_uuid TEXT NOT NULL REFERENCES nodes(uuid) ON DELETE CASCADE,
  event TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL,
  secret TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS node_webhooks (
  node_uuid TEXT NOT NULL REFERENCES nodes(uuid) ON DELETE CASCADE,
  webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
  PRIMARY KEY(node_uuid, webhook_id)
);
CREATE TABLE IF NOT EXISTS webhook_events (
  webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
  event TEXT NOT NULL,
  PRIMARY KEY(webhook_id, event)
);
CREATE TABLE IF NOT EXISTS dpi_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, node_uuid TEXT NOT NULL REFERENCES nodes(uuid) ON DELETE CASCADE,
  target TEXT NOT NULL, location TEXT NOT NULL DEFAULT 'russia', provider_id INTEGER, status TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT '', automatic INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'ip', source TEXT NOT NULL DEFAULT 'manual', temp_user_uuid TEXT NOT NULL DEFAULT '',
  context TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS dpi_billing_sync (
  provider_check_id INTEGER PRIMARY KEY, amount REAL NOT NULL, billed_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_ip_pool (
  id INTEGER PRIMARY KEY AUTOINCREMENT, node_uuid TEXT NOT NULL REFERENCES nodes(uuid) ON DELETE CASCADE,
  address TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 100, status TEXT NOT NULL DEFAULT 'untested',
  error TEXT NOT NULL DEFAULT '', last_tested TEXT NOT NULL DEFAULT '', UNIQUE(node_uuid, address)
);
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL, source TEXT NOT NULL,
  message TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_logs_time ON app_logs(created_at DESC);
