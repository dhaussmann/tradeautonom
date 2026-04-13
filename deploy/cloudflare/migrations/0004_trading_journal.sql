-- Migration number: 0004    Trading Journal: order_history, fill_history, funding_payments, points_history

-- Orders fetched from exchanges (deduplicated by exchange_order_id + exchange)
CREATE TABLE IF NOT EXISTS order_history (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  exchange_order_id  TEXT    NOT NULL,
  exchange           TEXT    NOT NULL,
  instrument         TEXT    NOT NULL,
  token              TEXT    NOT NULL,
  side               TEXT    NOT NULL,
  order_type         TEXT    NOT NULL DEFAULT 'LIMIT',
  status             TEXT    NOT NULL DEFAULT 'FILLED',
  price              REAL    NOT NULL DEFAULT 0,
  average_price      REAL    NOT NULL DEFAULT 0,
  qty                REAL    NOT NULL DEFAULT 0,
  filled_qty         REAL    NOT NULL DEFAULT 0,
  fee                REAL    NOT NULL DEFAULT 0,
  reduce_only        INTEGER NOT NULL DEFAULT 0,
  post_only          INTEGER NOT NULL DEFAULT 0,
  created_at         INTEGER NOT NULL,
  updated_at         INTEGER NOT NULL,
  bot_id             TEXT,
  user_id            TEXT    NOT NULL DEFAULT '',
  UNIQUE(exchange, exchange_order_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_oh_user_created ON order_history(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_oh_exchange_created ON order_history(exchange, created_at);
CREATE INDEX IF NOT EXISTS idx_oh_token ON order_history(token, created_at);
CREATE INDEX IF NOT EXISTS idx_oh_bot ON order_history(bot_id, created_at);

-- Individual fills (partial executions) from exchanges
CREATE TABLE IF NOT EXISTS fill_history (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  exchange_fill_id   TEXT    NOT NULL,
  exchange_order_id  TEXT    NOT NULL,
  exchange           TEXT    NOT NULL,
  instrument         TEXT    NOT NULL,
  token              TEXT    NOT NULL,
  side               TEXT    NOT NULL,
  price              REAL    NOT NULL,
  qty                REAL    NOT NULL,
  value              REAL    NOT NULL DEFAULT 0,
  fee                REAL    NOT NULL DEFAULT 0,
  is_taker           INTEGER NOT NULL DEFAULT 1,
  trade_type         TEXT    NOT NULL DEFAULT 'TRADE',
  created_at         INTEGER NOT NULL,
  bot_id             TEXT,
  user_id            TEXT    NOT NULL DEFAULT '',
  UNIQUE(exchange, exchange_fill_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_fh_user_created ON fill_history(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_fh_exchange_created ON fill_history(exchange, created_at);
CREATE INDEX IF NOT EXISTS idx_fh_token ON fill_history(token, created_at);
CREATE INDEX IF NOT EXISTS idx_fh_order ON fill_history(exchange_order_id, exchange);
CREATE INDEX IF NOT EXISTS idx_fh_bot ON fill_history(bot_id, created_at);

-- Funding payments per exchange (individual payments, not cumulative)
CREATE TABLE IF NOT EXISTS funding_payments (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  exchange_payment_id TEXT   NOT NULL DEFAULT '',
  exchange           TEXT    NOT NULL,
  instrument         TEXT    NOT NULL,
  token              TEXT    NOT NULL,
  side               TEXT    NOT NULL DEFAULT '',
  size               REAL    NOT NULL DEFAULT 0,
  funding_fee        REAL    NOT NULL,
  funding_rate       REAL    NOT NULL DEFAULT 0,
  mark_price         REAL    NOT NULL DEFAULT 0,
  paid_at            INTEGER NOT NULL,
  bot_id             TEXT,
  user_id            TEXT    NOT NULL DEFAULT '',
  UNIQUE(exchange, exchange_payment_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_fp_user_paid ON funding_payments(user_id, paid_at);
CREATE INDEX IF NOT EXISTS idx_fp_exchange_paid ON funding_payments(exchange, paid_at);
CREATE INDEX IF NOT EXISTS idx_fp_token ON funding_payments(token, paid_at);

-- Extended Points per season/epoch
CREATE TABLE IF NOT EXISTS points_history (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  exchange           TEXT    NOT NULL DEFAULT 'extended',
  season_id          INTEGER NOT NULL,
  epoch_id           INTEGER NOT NULL,
  start_date         TEXT    NOT NULL,
  end_date           TEXT    NOT NULL,
  points             REAL    NOT NULL DEFAULT 0,
  fetched_at         INTEGER NOT NULL,
  user_id            TEXT    NOT NULL DEFAULT '',
  UNIQUE(exchange, season_id, epoch_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_ph_user ON points_history(user_id);
