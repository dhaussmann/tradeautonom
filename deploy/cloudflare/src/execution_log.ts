/**
 * Execution Log module — D1 ingest + read logic for per-chunk AI training data.
 *
 * Each row = one TWAP chunk with orderbook snapshot at decision time + fill result.
 */

// ── Types ────────────────────────────────────────────────────

interface ExecutionLogEntry {
  execution_id: string;
  chunk_index: number;
  action: string;
  timestamp_ms: number;
  bot_id?: string;
  pair: string;
  exchange_maker: string;
  exchange_taker: string;
  instrument_maker: string;
  instrument_taker: string;
  maker_side: string;
  // Orderbook snapshot
  snapshot_mid_maker?: number | null;
  snapshot_mid_taker?: number | null;
  snapshot_best_bid_maker?: number | null;
  snapshot_best_ask_maker?: number | null;
  snapshot_best_bid_taker?: number | null;
  snapshot_best_ask_taker?: number | null;
  snapshot_spread_bps?: number | null;
  snapshot_bid_ask_spread_maker_bps?: number | null;
  snapshot_bid_ask_spread_taker_bps?: number | null;
  snapshot_ohi_maker?: number | null;
  snapshot_ohi_taker?: number | null;
  snapshot_depth_5bps_maker?: number | null;
  snapshot_depth_5bps_taker?: number | null;
  snapshot_depth_20bps_maker?: number | null;
  snapshot_depth_20bps_taker?: number | null;
  // Execution result
  target_qty?: number | null;
  filled_qty_maker?: number | null;
  filled_qty_taker?: number | null;
  fill_price_maker?: number | null;
  fill_price_taker?: number | null;
  realized_slippage_maker_bps?: number | null;
  realized_slippage_taker_bps?: number | null;
  chase_rounds?: number | null;
  chunk_duration_s?: number | null;
  success?: number;
  error?: string | null;
  // Market context
  funding_rate_long?: number | null;
  funding_rate_short?: number | null;
  funding_spread?: number | null;
  v4_spread_consistency?: number | null;
  v4_confidence_score?: number | null;
  hour_of_day?: number | null;
  day_of_week?: number | null;
  btc_volatility_1h?: number | null;
  // Config context
  use_depth_spread?: number | null;
  taker_drift_guard?: number | null;
  max_slippage_bps_cfg?: number | null;
  maker_timeout_ms?: number | null;
  reduce_only?: number;
  simulation?: number;
}

interface IngestPayload {
  entries: ExecutionLogEntry[];
}

// ── Ingest ───────────────────────────────────────────────────

export async function handleExecutionLogIngest(
  request: Request,
  db: D1Database,
  ingestToken: string,
  userId?: string,
): Promise<Response> {
  // Auth check
  const auth = request.headers.get("Authorization") || "";
  if (auth !== `Bearer ${ingestToken}`) {
    return json({ error: "Unauthorized" }, 401);
  }

  let payload: IngestPayload;
  try {
    payload = await request.json() as IngestPayload;
  } catch {
    return json({ error: "Invalid JSON" }, 400);
  }

  if (!payload.entries || !Array.isArray(payload.entries) || payload.entries.length === 0) {
    return json({ error: "Missing or empty entries array" }, 400);
  }

  const effectiveUserId = userId || "";
  const stmts: D1PreparedStatement[] = [];

  for (const e of payload.entries) {
    if (!e.execution_id || !e.pair || !e.exchange_maker || !e.exchange_taker) {
      continue; // skip malformed entries
    }

    stmts.push(
      db.prepare(
        `INSERT OR IGNORE INTO execution_log (
          execution_id, chunk_index, action, timestamp_ms, user_id, bot_id,
          pair, exchange_maker, exchange_taker, instrument_maker, instrument_taker, maker_side,
          snapshot_mid_maker, snapshot_mid_taker,
          snapshot_best_bid_maker, snapshot_best_ask_maker,
          snapshot_best_bid_taker, snapshot_best_ask_taker,
          snapshot_spread_bps, snapshot_bid_ask_spread_maker_bps, snapshot_bid_ask_spread_taker_bps,
          snapshot_ohi_maker, snapshot_ohi_taker,
          snapshot_depth_5bps_maker, snapshot_depth_5bps_taker,
          snapshot_depth_20bps_maker, snapshot_depth_20bps_taker,
          target_qty, filled_qty_maker, filled_qty_taker,
          fill_price_maker, fill_price_taker,
          realized_slippage_maker_bps, realized_slippage_taker_bps,
          chase_rounds, chunk_duration_s, success, error,
          funding_rate_long, funding_rate_short, funding_spread,
          v4_spread_consistency, v4_confidence_score,
          hour_of_day, day_of_week, btc_volatility_1h,
          use_depth_spread, taker_drift_guard, max_slippage_bps_cfg,
          maker_timeout_ms, reduce_only, simulation
        ) VALUES (
          ?, ?, ?, ?, ?, ?,
          ?, ?, ?, ?, ?, ?,
          ?, ?,
          ?, ?,
          ?, ?,
          ?, ?, ?,
          ?, ?,
          ?, ?,
          ?, ?,
          ?, ?, ?,
          ?, ?,
          ?, ?,
          ?, ?, ?, ?,
          ?, ?, ?,
          ?, ?,
          ?, ?, ?,
          ?, ?, ?,
          ?, ?, ?
        )`
      ).bind(
        e.execution_id,
        e.chunk_index ?? 0,
        e.action ?? "ENTRY",
        e.timestamp_ms ?? Date.now(),
        effectiveUserId,
        e.bot_id ?? "",
        e.pair,
        e.exchange_maker,
        e.exchange_taker,
        e.instrument_maker ?? "",
        e.instrument_taker ?? "",
        e.maker_side ?? "",
        e.snapshot_mid_maker ?? null,
        e.snapshot_mid_taker ?? null,
        e.snapshot_best_bid_maker ?? null,
        e.snapshot_best_ask_maker ?? null,
        e.snapshot_best_bid_taker ?? null,
        e.snapshot_best_ask_taker ?? null,
        e.snapshot_spread_bps ?? null,
        e.snapshot_bid_ask_spread_maker_bps ?? null,
        e.snapshot_bid_ask_spread_taker_bps ?? null,
        e.snapshot_ohi_maker ?? null,
        e.snapshot_ohi_taker ?? null,
        e.snapshot_depth_5bps_maker ?? null,
        e.snapshot_depth_5bps_taker ?? null,
        e.snapshot_depth_20bps_maker ?? null,
        e.snapshot_depth_20bps_taker ?? null,
        e.target_qty ?? null,
        e.filled_qty_maker ?? null,
        e.filled_qty_taker ?? null,
        e.fill_price_maker ?? null,
        e.fill_price_taker ?? null,
        e.realized_slippage_maker_bps ?? null,
        e.realized_slippage_taker_bps ?? null,
        e.chase_rounds ?? null,
        e.chunk_duration_s ?? null,
        e.success ?? 1,
        e.error ?? null,
        e.funding_rate_long ?? null,
        e.funding_rate_short ?? null,
        e.funding_spread ?? null,
        e.v4_spread_consistency ?? null,
        e.v4_confidence_score ?? null,
        e.hour_of_day ?? null,
        e.day_of_week ?? null,
        e.btc_volatility_1h ?? null,
        e.use_depth_spread ?? null,
        e.taker_drift_guard ?? null,
        e.max_slippage_bps_cfg ?? null,
        e.maker_timeout_ms ?? null,
        e.reduce_only ?? 0,
        e.simulation ?? 0,
      ),
    );
  }

  if (stmts.length > 0) {
    await db.batch(stmts);
  }

  return json({ ok: true, rows: stmts.length });
}

// ── Query ────────────────────────────────────────────────────

export async function handleExecutionLogQuery(
  url: URL,
  db: D1Database,
  userId: string = "",
): Promise<Response> {
  const from = parseInt(url.searchParams.get("from") || "0");
  const to = parseInt(url.searchParams.get("to") || `${Date.now()}`);
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "500"), 5000);
  const pair = url.searchParams.get("pair");
  const botId = url.searchParams.get("bot_id");
  const executionId = url.searchParams.get("execution_id");
  const action = url.searchParams.get("action");

  let query =
    "SELECT * FROM execution_log WHERE timestamp_ms >= ? AND timestamp_ms <= ? AND user_id = ?";
  const binds: unknown[] = [from, to, userId];

  if (pair) {
    query += " AND pair = ?";
    binds.push(pair);
  }
  if (botId) {
    query += " AND bot_id = ?";
    binds.push(botId);
  }
  if (executionId) {
    query += " AND execution_id = ?";
    binds.push(executionId);
  }
  if (action) {
    query += " AND action = ?";
    binds.push(action);
  }
  query += " ORDER BY timestamp_ms DESC LIMIT ?";
  binds.push(limit);

  const result = await db.prepare(query).bind(...binds).all();
  return json({ data: result.results ?? [], count: result.results?.length ?? 0 });
}

// ── Helpers ──────────────────────────────────────────────────

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    },
  });
}
