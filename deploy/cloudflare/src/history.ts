/**
 * History module — D1 ingest + read logic for position snapshots,
 * equity curves, and trade detection.
 */

// ── Types ────────────────────────────────────────────────────

interface PositionPayload {
  exchange: string;
  token: string;
  instrument: string;
  side: string;
  size: number;
  entry_price: number;
  mark_price: number;
  unrealized_pnl: number;
  realized_pnl: number;
  cumulative_funding: number;
  funding_rate: number;
  leverage: number;
}

interface ExchangePayload {
  exchange: string;
  equity: number;
  unrealized_pnl: number;
  positions: PositionPayload[];
  error?: string | null;
}

interface IngestPayload {
  exchanges: Record<string, ExchangePayload>;
  timestamp: number; // Unix seconds from backend
}

// Key for identifying a unique position
type PosKey = string; // "exchange:instrument:side"

function posKey(p: { exchange: string; instrument: string; side: string }): PosKey {
  return `${p.exchange}:${p.instrument}:${p.side}`;
}

// ── Ingest ───────────────────────────────────────────────────

export async function handleIngest(
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

  if (!payload.exchanges || !payload.timestamp) {
    return json({ error: "Missing exchanges or timestamp" }, 400);
  }

  // Resolve user_id: explicit param > payload > empty
  const effectiveUserId = userId || (payload as any).user_id || "";

  const ts = Math.round(payload.timestamp * 1000); // convert to ms
  const allPositions: (PositionPayload & { exchange: string })[] = [];

  // Batch statements
  const stmts: D1PreparedStatement[] = [];

  // 1. Equity snapshots + collect positions
  for (const [exchName, exch] of Object.entries(payload.exchanges)) {
    if (exch.error) continue;

    stmts.push(
      db
        .prepare(
          "INSERT INTO equity_snapshots (ts, exchange, equity, unrealized_pnl, user_id) VALUES (?, ?, ?, ?, ?)",
        )
        .bind(ts, exchName, exch.equity ?? 0, exch.unrealized_pnl ?? 0, effectiveUserId),
    );

    for (const p of exch.positions ?? []) {
      allPositions.push({ ...p, exchange: exchName });

      stmts.push(
        db
          .prepare(
            `INSERT INTO position_snapshots
             (ts, exchange, token, instrument, side, size, entry_price, mark_price,
              unrealized_pnl, realized_pnl, cumulative_funding, funding_rate, leverage, user_id)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          )
          .bind(
            ts,
            exchName,
            p.token ?? "",
            p.instrument ?? "",
            p.side ?? "",
            p.size ?? 0,
            p.entry_price ?? 0,
            p.mark_price ?? 0,
            p.unrealized_pnl ?? 0,
            p.realized_pnl ?? 0,
            p.cumulative_funding ?? 0,
            p.funding_rate ?? 0,
            p.leverage ?? 0,
            effectiveUserId,
          ),
      );
    }
  }

  // 2. Trade detection: compare with previous snapshot
  const tradeStmts = await detectClosedTrades(db, ts, allPositions, effectiveUserId);
  stmts.push(...tradeStmts);

  // 3. Execute all in a batch
  if (stmts.length > 0) {
    await db.batch(stmts);
  }

  return json({
    ok: true,
    equity_rows: Object.keys(payload.exchanges).length,
    position_rows: allPositions.length,
    trade_rows: tradeStmts.length,
  });
}

// ── Trade Detection ──────────────────────────────────────────

async function detectClosedTrades(
  db: D1Database,
  currentTs: number,
  currentPositions: (PositionPayload & { exchange: string })[],
  userId: string = "",
): Promise<D1PreparedStatement[]> {
  // Get the most recent snapshot timestamp before this one (for this user)
  const lastTsRow = await db
    .prepare(
      "SELECT MAX(ts) as last_ts FROM position_snapshots WHERE ts < ? AND user_id = ?",
    )
    .bind(currentTs, userId)
    .first<{ last_ts: number | null }>();

  if (!lastTsRow?.last_ts) return [];

  // Get all positions from the last snapshot (for this user)
  const prevRows = await db
    .prepare(
      "SELECT exchange, token, instrument, side, size, entry_price, mark_price, unrealized_pnl, realized_pnl, cumulative_funding FROM position_snapshots WHERE ts = ? AND user_id = ?",
    )
    .bind(lastTsRow.last_ts, userId)
    .all();

  if (!prevRows.results?.length) return [];

  // Build lookup of current positions
  const currentKeys = new Set<PosKey>();
  for (const p of currentPositions) {
    currentKeys.add(posKey(p));
  }

  // Positions that were in the previous snapshot but NOT in the current = closed
  const stmts: D1PreparedStatement[] = [];
  for (const prev of prevRows.results) {
    const key = posKey(prev as { exchange: string; instrument: string; side: string });
    if (!currentKeys.has(key)) {
      // This position was closed
      const p = prev as Record<string, unknown>;
      const realizedPnl = (p.realized_pnl as number) ?? 0;
      const cumFunding = (p.cumulative_funding as number) ?? 0;
      const totalPnl = realizedPnl + cumFunding;

      stmts.push(
        db
          .prepare(
            `INSERT INTO trades
             (exchange, token, instrument, side, size, entry_price, exit_price,
              opened_at, closed_at, realized_pnl, cumulative_funding, total_pnl, pair_token, user_id)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          )
          .bind(
            p.exchange as string,
            p.token as string,
            p.instrument as string,
            p.side as string,
            p.size as number,
            p.entry_price as number,
            p.mark_price as number, // last known mark price as exit price
            0, // opened_at — we don't have exact open time here
            currentTs,
            realizedPnl,
            cumFunding,
            totalPnl,
            p.token as string, // pair_token = token for grouping
            userId,
          ),
      );
    }
  }

  return stmts;
}

// ── Read Endpoints ───────────────────────────────────────────

export async function handleEquityHistory(
  url: URL,
  db: D1Database,
  userId: string = "",
): Promise<Response> {
  const exchange = url.searchParams.get("exchange");
  const from = parseInt(url.searchParams.get("from") || "0");
  const to = parseInt(url.searchParams.get("to") || `${Date.now()}`);
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "1000"), 5000);

  let query = "SELECT ts, exchange, equity, unrealized_pnl FROM equity_snapshots WHERE ts >= ? AND ts <= ? AND user_id = ?";
  const binds: unknown[] = [from, to, userId];

  if (exchange) {
    query += " AND exchange = ?";
    binds.push(exchange);
  }
  query += " ORDER BY ts ASC LIMIT ?";
  binds.push(limit);

  const result = await db.prepare(query).bind(...binds).all();
  return json({ data: result.results ?? [], count: result.results?.length ?? 0 });
}

export async function handleSnapshotHistory(
  url: URL,
  db: D1Database,
  userId: string = "",
): Promise<Response> {
  const token = url.searchParams.get("token");
  const exchange = url.searchParams.get("exchange");
  const from = parseInt(url.searchParams.get("from") || "0");
  const to = parseInt(url.searchParams.get("to") || `${Date.now()}`);
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "1000"), 5000);

  let query =
    "SELECT ts, exchange, token, instrument, side, size, entry_price, mark_price, unrealized_pnl, realized_pnl, cumulative_funding, funding_rate, leverage FROM position_snapshots WHERE ts >= ? AND ts <= ? AND user_id = ?";
  const binds: unknown[] = [from, to, userId];

  if (token) {
    query += " AND token = ?";
    binds.push(token);
  }
  if (exchange) {
    query += " AND exchange = ?";
    binds.push(exchange);
  }
  query += " ORDER BY ts ASC LIMIT ?";
  binds.push(limit);

  const result = await db.prepare(query).bind(...binds).all();
  return json({ data: result.results ?? [], count: result.results?.length ?? 0 });
}

export async function handleTradesHistory(
  url: URL,
  db: D1Database,
  userId: string = "",
): Promise<Response> {
  const token = url.searchParams.get("token");
  const exchange = url.searchParams.get("exchange");
  const from = parseInt(url.searchParams.get("from") || "0");
  const to = parseInt(url.searchParams.get("to") || `${Date.now()}`);
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "100"), 1000);

  let query =
    "SELECT id, exchange, token, instrument, side, size, entry_price, exit_price, opened_at, closed_at, realized_pnl, cumulative_funding, total_pnl, pair_token FROM trades WHERE closed_at >= ? AND closed_at <= ? AND user_id = ?";
  const binds: unknown[] = [from, to, userId];

  if (token) {
    query += " AND token = ?";
    binds.push(token);
  }
  if (exchange) {
    query += " AND exchange = ?";
    binds.push(exchange);
  }
  query += " ORDER BY closed_at DESC LIMIT ?";
  binds.push(limit);

  const result = await db.prepare(query).bind(...binds).all();
  return json({ data: result.results ?? [], count: result.results?.length ?? 0 });
}

// ── Retention cleanup (called from scheduled handler) ────────

export async function cleanupOldSnapshots(db: D1Database, retentionDays: number = 90): Promise<number> {
  const cutoff = Date.now() - retentionDays * 24 * 60 * 60 * 1000;

  const r1 = await db.prepare("DELETE FROM equity_snapshots WHERE ts < ?").bind(cutoff).run();
  const r2 = await db.prepare("DELETE FROM position_snapshots WHERE ts < ?").bind(cutoff).run();

  return (r1.meta?.changes ?? 0) + (r2.meta?.changes ?? 0);
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
