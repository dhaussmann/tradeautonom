/**
 * Journal Endpoints — ingest and read orders, fills, funding payments, and points
 * from the D1 database.
 *
 * Ingest: POST /api/journal/ingest (INGEST_TOKEN auth)
 * Read:   GET /api/journal/orders|fills|funding|points|summary (session auth)
 */

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

// ─────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────

interface JournalIngestPayload {
  user_id?: string;
  orders?: OrderRecord[];
  fills?: FillRecord[];
  funding_payments?: FundingRecord[];
  points?: PointsRecord[];
  timestamp?: number;
}

interface OrderRecord {
  exchange_order_id: string;
  exchange: string;
  instrument: string;
  token: string;
  side: string;
  order_type: string;
  status: string;
  price: number;
  average_price: number;
  qty: number;
  filled_qty: number;
  fee: number;
  reduce_only: number;
  post_only: number;
  created_at: number;
  updated_at: number;
  bot_id?: string | null;
}

interface FillRecord {
  exchange_fill_id: string;
  exchange_order_id: string;
  exchange: string;
  instrument: string;
  token: string;
  side: string;
  price: number;
  qty: number;
  value: number;
  fee: number;
  is_taker: number;
  trade_type: string;
  created_at: number;
  bot_id?: string | null;
}

interface FundingRecord {
  exchange_payment_id: string;
  exchange: string;
  instrument: string;
  token: string;
  side: string;
  size: number;
  funding_fee: number;
  funding_rate: number;
  mark_price: number;
  paid_at: number;
  bot_id?: string | null;
}

interface PointsRecord {
  exchange: string;
  season_id: number;
  epoch_id: number;
  start_date: string;
  end_date: string;
  points: number;
}

// ─────────────────────────────────────────────────────────────
// Ingest
// ─────────────────────────────────────────────────────────────

export async function handleJournalIngest(
  request: Request,
  db: D1Database,
  ingestToken: string,
  userId?: string,
): Promise<Response> {
  const auth = request.headers.get("Authorization") || "";
  if (auth !== `Bearer ${ingestToken}`) {
    return json({ error: "Unauthorized" }, 401);
  }

  let payload: JournalIngestPayload;
  try {
    payload = (await request.json()) as JournalIngestPayload;
  } catch {
    return json({ error: "Invalid JSON" }, 400);
  }

  let effectiveUserId = userId || payload.user_id || "";

  // Resolve user_id from user_container table if not provided
  if (!effectiveUserId) {
    const row = await db.prepare(
      "SELECT user_id FROM user_container WHERE status = 'running' LIMIT 1"
    ).first<{ user_id: string }>();
    if (row) effectiveUserId = row.user_id;
  }

  const now = Date.now();

  const stmts: D1PreparedStatement[] = [];
  let ordersCount = 0;
  let fillsCount = 0;
  let fundingCount = 0;
  let pointsCount = 0;

  // Orders (INSERT OR REPLACE using UNIQUE constraint)
  for (const o of payload.orders ?? []) {
    stmts.push(
      db
        .prepare(
          `INSERT OR REPLACE INTO order_history
           (exchange_order_id, exchange, instrument, token, side, order_type, status,
            price, average_price, qty, filled_qty, fee, reduce_only, post_only,
            created_at, updated_at, bot_id, user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        )
        .bind(
          o.exchange_order_id,
          o.exchange,
          o.instrument,
          o.token,
          o.side,
          o.order_type,
          o.status,
          o.price,
          o.average_price,
          o.qty,
          o.filled_qty,
          o.fee,
          o.reduce_only,
          o.post_only,
          o.created_at,
          o.updated_at,
          o.bot_id ?? null,
          effectiveUserId,
        ),
    );
    ordersCount++;
  }

  // Fills
  for (const f of payload.fills ?? []) {
    stmts.push(
      db
        .prepare(
          `INSERT OR REPLACE INTO fill_history
           (exchange_fill_id, exchange_order_id, exchange, instrument, token, side,
            price, qty, value, fee, is_taker, trade_type, created_at, bot_id, user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        )
        .bind(
          f.exchange_fill_id,
          f.exchange_order_id,
          f.exchange,
          f.instrument,
          f.token,
          f.side,
          f.price,
          f.qty,
          f.value,
          f.fee,
          f.is_taker,
          f.trade_type,
          f.created_at,
          f.bot_id ?? null,
          effectiveUserId,
        ),
    );
    fillsCount++;
  }

  // Funding payments
  for (const fp of payload.funding_payments ?? []) {
    stmts.push(
      db
        .prepare(
          `INSERT OR REPLACE INTO funding_payments
           (exchange_payment_id, exchange, instrument, token, side, size,
            funding_fee, funding_rate, mark_price, paid_at, bot_id, user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        )
        .bind(
          fp.exchange_payment_id,
          fp.exchange,
          fp.instrument,
          fp.token,
          fp.side,
          fp.size,
          fp.funding_fee,
          fp.funding_rate,
          fp.mark_price,
          fp.paid_at,
          fp.bot_id ?? null,
          effectiveUserId,
        ),
    );
    fundingCount++;
  }

  // Points (UPSERT by exchange + season + epoch + user)
  for (const p of payload.points ?? []) {
    stmts.push(
      db
        .prepare(
          `INSERT OR REPLACE INTO points_history
           (exchange, season_id, epoch_id, start_date, end_date, points, fetched_at, user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
        )
        .bind(
          p.exchange,
          p.season_id,
          p.epoch_id,
          p.start_date,
          p.end_date,
          p.points,
          now,
          effectiveUserId,
        ),
    );
    pointsCount++;
  }

  // Execute in batch (D1 supports up to 500 statements per batch)
  if (stmts.length > 0) {
    // Split into batches of 400 to stay within D1 limits
    for (let i = 0; i < stmts.length; i += 400) {
      const batch = stmts.slice(i, i + 400);
      await db.batch(batch);
    }
  }

  return json({
    ok: true,
    orders_upserted: ordersCount,
    fills_upserted: fillsCount,
    funding_upserted: fundingCount,
    points_upserted: pointsCount,
  });
}

// ─────────────────────────────────────────────────────────────
// Read: Orders
// ─────────────────────────────────────────────────────────────

export async function handleJournalOrders(
  url: URL,
  db: D1Database,
  userId: string,
): Promise<Response> {
  const exchange = url.searchParams.get("exchange");
  const token = url.searchParams.get("token");
  const botId = url.searchParams.get("bot_id");
  const from = parseInt(url.searchParams.get("from") || "0");
  const to = parseInt(url.searchParams.get("to") || `${Date.now()}`);
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "200"), 1000);

  let query =
    "SELECT * FROM order_history WHERE created_at >= ? AND created_at <= ? AND user_id = ?";
  const binds: unknown[] = [from, to, userId];

  if (exchange) {
    query += " AND exchange = ?";
    binds.push(exchange);
  }
  if (token) {
    query += " AND token = ?";
    binds.push(token);
  }
  if (botId) {
    query += " AND bot_id = ?";
    binds.push(botId);
  }
  query += " ORDER BY created_at DESC LIMIT ?";
  binds.push(limit);

  const result = await db.prepare(query).bind(...binds).all();
  return json({ data: result.results ?? [], count: result.results?.length ?? 0 });
}

// ─────────────────────────────────────────────────────────────
// Read: Fills
// ─────────────────────────────────────────────────────────────

export async function handleJournalFills(
  url: URL,
  db: D1Database,
  userId: string,
): Promise<Response> {
  const exchange = url.searchParams.get("exchange");
  const token = url.searchParams.get("token");
  const botId = url.searchParams.get("bot_id");
  const from = parseInt(url.searchParams.get("from") || "0");
  const to = parseInt(url.searchParams.get("to") || `${Date.now()}`);
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "200"), 1000);

  let query =
    "SELECT * FROM fill_history WHERE created_at >= ? AND created_at <= ? AND user_id = ?";
  const binds: unknown[] = [from, to, userId];

  if (exchange) {
    query += " AND exchange = ?";
    binds.push(exchange);
  }
  if (token) {
    query += " AND token = ?";
    binds.push(token);
  }
  if (botId) {
    query += " AND bot_id = ?";
    binds.push(botId);
  }
  query += " ORDER BY created_at DESC LIMIT ?";
  binds.push(limit);

  const result = await db.prepare(query).bind(...binds).all();
  return json({ data: result.results ?? [], count: result.results?.length ?? 0 });
}

// ─────────────────────────────────────────────────────────────
// Read: Funding Payments
// ─────────────────────────────────────────────────────────────

export async function handleJournalFunding(
  url: URL,
  db: D1Database,
  userId: string,
): Promise<Response> {
  const exchange = url.searchParams.get("exchange");
  const token = url.searchParams.get("token");
  const from = parseInt(url.searchParams.get("from") || "0");
  const to = parseInt(url.searchParams.get("to") || `${Date.now()}`);
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "200"), 1000);

  let query =
    "SELECT * FROM funding_payments WHERE paid_at >= ? AND paid_at <= ? AND user_id = ?";
  const binds: unknown[] = [from, to, userId];

  if (exchange) {
    query += " AND exchange = ?";
    binds.push(exchange);
  }
  if (token) {
    query += " AND token = ?";
    binds.push(token);
  }
  query += " ORDER BY paid_at DESC LIMIT ?";
  binds.push(limit);

  const result = await db.prepare(query).bind(...binds).all();
  return json({ data: result.results ?? [], count: result.results?.length ?? 0 });
}

// ─────────────────────────────────────────────────────────────
// Read: Points
// ─────────────────────────────────────────────────────────────

export async function handleJournalPoints(
  url: URL,
  db: D1Database,
  userId: string,
): Promise<Response> {
  const exchange = url.searchParams.get("exchange");

  let query = "SELECT * FROM points_history WHERE user_id = ?";
  const binds: unknown[] = [userId];

  if (exchange) {
    query += " AND exchange = ?";
    binds.push(exchange);
  }
  query += " ORDER BY season_id DESC, epoch_id DESC";

  const result = await db.prepare(query).bind(...binds).all();
  return json({ data: result.results ?? [], count: result.results?.length ?? 0 });
}

// ─────────────────────────────────────────────────────────────
// Read: Summary (aggregated PnL per bot/token/exchange)
// ─────────────────────────────────────────────────────────────

// ─────────────────────────────────────────────────────────────
// Read: Positions (FIFO aggregation of fills into positions)
// ─────────────────────────────────────────────────────────────

interface PositionResult {
  id: string;
  exchange: string;
  instrument: string;
  token: string;
  side: "LONG" | "SHORT";
  status: "CLOSED" | "OPEN";
  entry_qty: number;
  exit_qty: number;
  remaining_qty: number;
  entry_price: number;
  exit_price: number;
  realized_pnl: number;
  total_fees: number;
  total_funding: number;
  net_pnl: number;
  opened_at: number;
  closed_at: number | null;
  duration_ms: number;
  fill_count: number;
  bot_id: string | null;
}

interface FillRow {
  exchange: string;
  instrument: string;
  token: string;
  side: string;
  price: number;
  qty: number;
  fee: number;
  created_at: number;
  bot_id: string | null;
}

interface FundingRow {
  exchange: string;
  token: string;
  funding_fee: number;
  paid_at: number;
}

function aggregatePositions(
  fills: FillRow[],
  fundingRows: FundingRow[],
): PositionResult[] {
  // Group fills by (exchange, token), sorted by created_at ASC
  const groups = new Map<string, FillRow[]>();
  for (const f of fills) {
    const key = `${f.exchange}|${f.token}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(f);
  }

  // Group funding by (exchange, token), sorted by paid_at ASC
  const fundingMap = new Map<string, FundingRow[]>();
  for (const fp of fundingRows) {
    const key = `${fp.exchange}|${fp.token}`;
    if (!fundingMap.has(key)) fundingMap.set(key, []);
    fundingMap.get(key)!.push(fp);
  }

  const positions: PositionResult[] = [];
  let posIdx = 0;

  for (const [groupKey, groupFills] of groups) {
    // Ensure chronological order
    groupFills.sort((a, b) => a.created_at - b.created_at);

    let netQty = 0; // positive = long, negative = short
    let entryCost = 0; // sum of (price * qty) for entry fills
    let entryQty = 0;
    let exitCost = 0; // sum of (price * qty) for exit fills
    let exitQty = 0;
    let totalFees = 0;
    let fillCount = 0;
    let openedAt = 0;
    let botId: string | null = null;
    let side: "LONG" | "SHORT" = "LONG";

    for (const fill of groupFills) {
      const signedQty = fill.side === "BUY" ? fill.qty : -fill.qty;
      const prevNet = netQty;
      const newNet = netQty + signedQty;

      // Starting a new position? (epsilon for floating-point)
      if (Math.abs(netQty) < 1e-8) {
        // Reset accumulators for new position
        entryCost = 0;
        entryQty = 0;
        exitCost = 0;
        exitQty = 0;
        totalFees = 0;
        fillCount = 0;
        openedAt = fill.created_at;
        botId = fill.bot_id;
        side = signedQty > 0 ? "LONG" : "SHORT";
      }

      fillCount++;
      totalFees += Math.abs(fill.fee);

      // Determine if this fill is entry or exit
      const isEntry =
        (Math.abs(prevNet) < 1e-8) || // first fill (epsilon)
        (prevNet > 0 && signedQty > 0) || // adding to long
        (prevNet < 0 && signedQty < 0); // adding to short

      if (isEntry) {
        entryCost += fill.price * fill.qty;
        entryQty += fill.qty;
      } else if (Math.sign(prevNet) === Math.sign(newNet) || Math.abs(newNet) < 1e-8) {
        // Pure exit (reducing position, not flipping)
        exitCost += fill.price * fill.qty;
        exitQty += fill.qty;
      } else {
        // Position flip: part exit, part new entry
        const exitPart = Math.abs(prevNet);
        const entryPart = fill.qty - exitPart;
        exitCost += fill.price * exitPart;
        exitQty += exitPart;

        // Close current position
        const entryVwap = entryQty > 0 ? entryCost / entryQty : 0;
        const exitVwap = exitQty > 0 ? exitCost / exitQty : 0;
        const direction = side === "LONG" ? 1 : -1;
        const realizedPnl = (exitVwap - entryVwap) * exitQty * direction;

        // Match funding
        const funding = sumFunding(
          fundingMap.get(groupKey) ?? [],
          openedAt,
          fill.created_at,
        );

        positions.push({
          id: `pos-${posIdx++}`,
          exchange: fill.exchange,
          instrument: fill.instrument,
          token: fill.token,
          side,
          status: "CLOSED",
          entry_qty: entryQty,
          exit_qty: exitQty,
          remaining_qty: 0,
          entry_price: entryVwap,
          exit_price: exitVwap,
          realized_pnl: realizedPnl,
          total_fees: totalFees,
          total_funding: funding,
          net_pnl: realizedPnl - totalFees + funding,
          opened_at: openedAt,
          closed_at: fill.created_at,
          duration_ms: fill.created_at - openedAt,
          fill_count: fillCount,
          bot_id: botId,
        });

        // Start new position from the flip remainder
        entryCost = fill.price * entryPart;
        entryQty = entryPart;
        exitCost = 0;
        exitQty = 0;
        totalFees = 0;
        fillCount = 1; // this fill starts the new position
        openedAt = fill.created_at;
        side = newNet > 0 ? "LONG" : "SHORT";
      }

      netQty = newNet;

      // Position fully closed? (epsilon for floating-point)
      if (Math.abs(netQty) < 1e-8 && entryQty > 0) {
        const entryVwap = entryCost / entryQty;
        const exitVwap = exitQty > 0 ? exitCost / exitQty : 0;
        const direction = side === "LONG" ? 1 : -1;
        const realizedPnl = (exitVwap - entryVwap) * exitQty * direction;

        const funding = sumFunding(
          fundingMap.get(groupKey) ?? [],
          openedAt,
          fill.created_at,
        );

        positions.push({
          id: `pos-${posIdx++}`,
          exchange: fill.exchange,
          instrument: fill.instrument,
          token: fill.token,
          side,
          status: "CLOSED",
          entry_qty: entryQty,
          exit_qty: exitQty,
          remaining_qty: 0,
          entry_price: entryVwap,
          exit_price: exitVwap,
          realized_pnl: realizedPnl,
          total_fees: totalFees,
          total_funding: funding,
          net_pnl: realizedPnl - totalFees + funding,
          opened_at: openedAt,
          closed_at: fill.created_at,
          duration_ms: fill.created_at - openedAt,
          fill_count: fillCount,
          bot_id: botId,
        });

        // Reset for next position
        entryCost = 0;
        entryQty = 0;
        exitCost = 0;
        exitQty = 0;
        totalFees = 0;
        fillCount = 0;
        openedAt = 0;
      }
    }

    // Open position remaining? (epsilon for floating-point)
    if (Math.abs(netQty) >= 1e-8 && entryQty > 0) {
      const entryVwap = entryCost / entryQty;
      const exitVwap = exitQty > 0 ? exitCost / exitQty : 0;
      const direction = side === "LONG" ? 1 : -1;
      const partialPnl =
        exitQty > 0 ? (exitVwap - entryVwap) * exitQty * direction : 0;

      const now = Date.now();
      const funding = sumFunding(
        fundingMap.get(groupKey) ?? [],
        openedAt,
        now,
      );

      const lastFill = groupFills[groupFills.length - 1];
      positions.push({
        id: `pos-${posIdx++}`,
        exchange: lastFill.exchange,
        instrument: lastFill.instrument,
        token: lastFill.token,
        side,
        status: "OPEN",
        entry_qty: entryQty,
        exit_qty: exitQty,
        remaining_qty: Math.abs(netQty),
        entry_price: entryVwap,
        exit_price: exitVwap,
        realized_pnl: partialPnl,
        total_fees: totalFees,
        total_funding: funding,
        net_pnl: partialPnl - totalFees + funding,
        opened_at: openedAt,
        closed_at: null,
        duration_ms: now - openedAt,
        fill_count: fillCount,
        bot_id: botId,
      });
    }
  }

  // Sort positions: open first, then by opened_at DESC
  positions.sort((a, b) => {
    if (a.status !== b.status) return a.status === "OPEN" ? -1 : 1;
    return (b.opened_at || 0) - (a.opened_at || 0);
  });

  return positions;
}

function sumFunding(
  rows: FundingRow[],
  fromMs: number,
  toMs: number,
): number {
  let total = 0;
  for (const r of rows) {
    if (r.paid_at >= fromMs && r.paid_at <= toMs) {
      total += r.funding_fee;
    }
  }
  return total;
}

export async function handleJournalPositions(
  url: URL,
  db: D1Database,
  userId: string,
): Promise<Response> {
  const exchange = url.searchParams.get("exchange");
  const token = url.searchParams.get("token");
  const from = parseInt(url.searchParams.get("from") || "0");
  const to = parseInt(url.searchParams.get("to") || `${Date.now()}`);
  const statusFilter = url.searchParams.get("status") || "all"; // open, closed, all

  // Fetch all fills in time range (no limit — needed for correct FIFO)
  let fillsQuery =
    "SELECT exchange, instrument, token, side, price, qty, fee, created_at, bot_id FROM fill_history WHERE created_at >= ? AND created_at <= ? AND user_id = ?";
  const fillsBinds: unknown[] = [from, to, userId];

  if (exchange) {
    fillsQuery += " AND exchange = ?";
    fillsBinds.push(exchange);
  }
  if (token) {
    fillsQuery += " AND token = ?";
    fillsBinds.push(token);
  }
  fillsQuery += " ORDER BY created_at ASC";

  // Fetch all funding in time range
  let fundingQuery =
    "SELECT exchange, token, funding_fee, paid_at FROM funding_payments WHERE paid_at >= ? AND paid_at <= ? AND user_id = ?";
  const fundingBinds: unknown[] = [from, to, userId];

  if (exchange) {
    fundingQuery += " AND exchange = ?";
    fundingBinds.push(exchange);
  }
  if (token) {
    fundingQuery += " AND token = ?";
    fundingBinds.push(token);
  }
  fundingQuery += " ORDER BY paid_at ASC";

  const [fillsResult, fundingResult] = await Promise.all([
    db.prepare(fillsQuery).bind(...fillsBinds).all(),
    db.prepare(fundingQuery).bind(...fundingBinds).all(),
  ]);

  const fills = (fillsResult.results ?? []) as unknown as FillRow[];
  const funding = (fundingResult.results ?? []) as unknown as FundingRow[];

  let positions = aggregatePositions(fills, funding);

  // Apply status filter
  if (statusFilter === "open") {
    positions = positions.filter((p) => p.status === "OPEN");
  } else if (statusFilter === "closed") {
    positions = positions.filter((p) => p.status === "CLOSED");
  }

  // Compute stats
  const closed = positions.filter((p) => p.status === "CLOSED");
  const wins = closed.filter((p) => p.net_pnl > 0).length;
  const stats = {
    total_positions: positions.length,
    open_positions: positions.filter((p) => p.status === "OPEN").length,
    closed_positions: closed.length,
    total_realized_pnl: closed.reduce((s, p) => s + p.realized_pnl, 0),
    total_fees: positions.reduce((s, p) => s + p.total_fees, 0),
    total_funding: positions.reduce((s, p) => s + p.total_funding, 0),
    total_net_pnl: closed.reduce((s, p) => s + p.net_pnl, 0),
    win_rate: closed.length > 0 ? wins / closed.length : 0,
    wins,
    losses: closed.length - wins,
  };

  return json({ positions, stats });
}

// ─────────────────────────────────────────────────────────────
// Read: Paired Trades (delta-neutral pairs from per-exchange positions)
// ─────────────────────────────────────────────────────────────

interface PairedTrade {
  id: string;
  token: string;
  status: "OPEN" | "CLOSED";
  long: PositionResult | null;
  short: PositionResult | null;
  combined: {
    entry_spread: number;
    exit_spread: number;
    realized_pnl: number;
    total_fees: number;
    total_funding: number;
    net_pnl: number;
    size: number;
    opened_at: number;
    closed_at: number | null;
    duration_ms: number;
    fill_count: number;
  };
}

function pairPositions(positions: PositionResult[]): PairedTrade[] {
  // Group by token
  const tokenGroups = new Map<string, PositionResult[]>();
  for (const p of positions) {
    const token = p.token.toUpperCase();
    if (!tokenGroups.has(token)) tokenGroups.set(token, []);
    tokenGroups.get(token)!.push(p);
  }

  const trades: PairedTrade[] = [];
  let tradeIdx = 0;

  for (const [token, group] of tokenGroups) {
    // Split into longs and shorts, sorted by opened_at
    const longs = group
      .filter((p) => p.side === "LONG")
      .sort((a, b) => a.opened_at - b.opened_at);
    const shorts = group
      .filter((p) => p.side === "SHORT")
      .sort((a, b) => a.opened_at - b.opened_at);

    // Pair 1:1 by chronological order
    const paired = Math.min(longs.length, shorts.length);
    for (let i = 0; i < paired; i++) {
      const long = longs[i];
      const short = shorts[i];
      const bothClosed = long.status === "CLOSED" && short.status === "CLOSED";
      const openedAt = Math.min(long.opened_at, short.opened_at);
      const closedAt = bothClosed
        ? Math.max(long.closed_at || 0, short.closed_at || 0)
        : null;

      trades.push({
        id: `trade-${tradeIdx++}`,
        token,
        status: bothClosed ? "CLOSED" : "OPEN",
        long,
        short,
        combined: {
          entry_spread: long.entry_price - short.entry_price,
          exit_spread:
            long.exit_price && short.exit_price
              ? long.exit_price - short.exit_price
              : 0,
          realized_pnl: long.realized_pnl + short.realized_pnl,
          total_fees: long.total_fees + short.total_fees,
          total_funding: long.total_funding + short.total_funding,
          net_pnl: long.net_pnl + short.net_pnl,
          size: Math.max(long.entry_qty, short.entry_qty),
          opened_at: openedAt,
          closed_at: closedAt,
          duration_ms: closedAt ? closedAt - openedAt : Date.now() - openedAt,
          fill_count: long.fill_count + short.fill_count,
        },
      });
    }

    // Remaining unpaired positions → single-leg trades
    for (const extra of [
      ...longs.slice(paired),
      ...shorts.slice(paired),
    ]) {
      trades.push({
        id: `trade-${tradeIdx++}`,
        token,
        status: extra.status === "CLOSED" ? "CLOSED" : "OPEN",
        long: extra.side === "LONG" ? extra : null,
        short: extra.side === "SHORT" ? extra : null,
        combined: {
          entry_spread: 0,
          exit_spread: 0,
          realized_pnl: extra.realized_pnl,
          total_fees: extra.total_fees,
          total_funding: extra.total_funding,
          net_pnl: extra.net_pnl,
          size: extra.entry_qty,
          opened_at: extra.opened_at,
          closed_at: extra.closed_at,
          duration_ms: extra.duration_ms,
          fill_count: extra.fill_count,
        },
      });
    }
  }

  // Sort: open first, then by opened_at DESC
  trades.sort((a, b) => {
    if (a.status !== b.status) return a.status === "OPEN" ? -1 : 1;
    return b.combined.opened_at - a.combined.opened_at;
  });

  return trades;
}

export async function handleJournalPairedTrades(
  url: URL,
  db: D1Database,
  userId: string,
): Promise<Response> {
  const token = url.searchParams.get("token");
  const from = parseInt(url.searchParams.get("from") || "0");
  const to = parseInt(url.searchParams.get("to") || `${Date.now()}`);
  const statusFilter = url.searchParams.get("status") || "all";

  // Fetch all fills in time range
  let fillsQuery =
    "SELECT exchange, instrument, token, side, price, qty, fee, created_at, bot_id FROM fill_history WHERE created_at >= ? AND created_at <= ? AND user_id = ?";
  const fillsBinds: unknown[] = [from, to, userId];

  if (token) {
    fillsQuery += " AND token = ?";
    fillsBinds.push(token);
  }
  fillsQuery += " ORDER BY created_at ASC";

  // Fetch all funding in time range
  let fundingQuery =
    "SELECT exchange, token, funding_fee, paid_at FROM funding_payments WHERE paid_at >= ? AND paid_at <= ? AND user_id = ?";
  const fundingBinds: unknown[] = [from, to, userId];

  if (token) {
    fundingQuery += " AND token = ?";
    fundingBinds.push(token);
  }
  fundingQuery += " ORDER BY paid_at ASC";

  const [fillsResult, fundingResult] = await Promise.all([
    db.prepare(fillsQuery).bind(...fillsBinds).all(),
    db.prepare(fundingQuery).bind(...fundingBinds).all(),
  ]);

  const fills = (fillsResult.results ?? []) as unknown as FillRow[];
  const funding = (fundingResult.results ?? []) as unknown as FundingRow[];

  const positions = aggregatePositions(fills, funding);
  let trades = pairPositions(positions);

  // Apply status filter
  if (statusFilter === "open") {
    trades = trades.filter((t) => t.status === "OPEN");
  } else if (statusFilter === "closed") {
    trades = trades.filter((t) => t.status === "CLOSED");
  }

  // Compute stats
  const closed = trades.filter((t) => t.status === "CLOSED");
  const wins = closed.filter((t) => t.combined.net_pnl > 0).length;
  const stats = {
    total_trades: trades.length,
    open_trades: trades.filter((t) => t.status === "OPEN").length,
    closed_trades: closed.length,
    total_realized_pnl: closed.reduce((s, t) => s + t.combined.realized_pnl, 0),
    total_fees: trades.reduce((s, t) => s + t.combined.total_fees, 0),
    total_funding: trades.reduce((s, t) => s + t.combined.total_funding, 0),
    total_net_pnl: closed.reduce((s, t) => s + t.combined.net_pnl, 0),
    win_rate: closed.length > 0 ? wins / closed.length : 0,
    wins,
    losses: closed.length - wins,
  };

  return json({ trades, stats });
}

export async function handleJournalSummary(
  url: URL,
  db: D1Database,
  userId: string,
): Promise<Response> {
  const from = parseInt(url.searchParams.get("from") || "0");
  const to = parseInt(url.searchParams.get("to") || `${Date.now()}`);
  const groupBy = url.searchParams.get("group_by") || "exchange"; // exchange, token, bot_id

  // Fills summary (fees, volume)
  const fillsQuery = `
    SELECT ${groupBy}, side,
           COUNT(*) as fill_count,
           SUM(qty) as total_qty,
           SUM(value) as total_value,
           SUM(fee) as total_fee,
           SUM(CASE WHEN is_taker = 1 THEN 1 ELSE 0 END) as taker_fills,
           SUM(CASE WHEN is_taker = 0 THEN 1 ELSE 0 END) as maker_fills
    FROM fill_history
    WHERE created_at >= ? AND created_at <= ? AND user_id = ?
    GROUP BY ${groupBy}, side
    ORDER BY total_value DESC
  `;

  // Funding summary
  const fundingQuery = `
    SELECT ${groupBy === "bot_id" ? "exchange" : groupBy},
           SUM(funding_fee) as total_funding,
           COUNT(*) as payment_count
    FROM funding_payments
    WHERE paid_at >= ? AND paid_at <= ? AND user_id = ?
    GROUP BY ${groupBy === "bot_id" ? "exchange" : groupBy}
  `;

  // Orders summary
  const ordersQuery = `
    SELECT ${groupBy},
           COUNT(*) as order_count,
           SUM(CASE WHEN status = 'FILLED' THEN 1 ELSE 0 END) as filled_count,
           SUM(CASE WHEN status = 'CANCELLED' THEN 1 ELSE 0 END) as cancelled_count
    FROM order_history
    WHERE created_at >= ? AND created_at <= ? AND user_id = ?
    GROUP BY ${groupBy}
  `;

  const [fillsResult, fundingResult, ordersResult] = await Promise.all([
    db.prepare(fillsQuery).bind(from, to, userId).all(),
    db.prepare(fundingQuery).bind(from, to, userId).all(),
    db.prepare(ordersQuery).bind(from, to, userId).all(),
  ]);

  return json({
    fills: fillsResult.results ?? [],
    funding: fundingResult.results ?? [],
    orders: ordersResult.results ?? [],
    period: { from, to },
    group_by: groupBy,
  });
}
