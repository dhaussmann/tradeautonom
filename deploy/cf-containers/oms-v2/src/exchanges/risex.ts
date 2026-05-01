/**
 * RisexOms — singleton Durable Object that holds ONE outbound WebSocket
 * to RISEx (rise.trade) and keeps top-10 orderbooks for every tracked
 * market in memory. RISEx is a fully-onchain perp DEX on RISE Chain.
 *
 * Endpoint: wss://ws.rise.trade/ws (mainnet) — we run mainnet only here;
 * testnet support can be added if needed.
 *
 * Subscription protocol (from RISEx docs §6, verified via the Python
 * reference implementation in app/data_layer.py::_run_ob_ws_risex):
 *
 *   client → server  {"method":"subscribe",
 *                     "params":{"channel":"orderbook","market_ids":[1,2,...]}}
 *   server → client  {"channel":"orderbook","data":{bids,asks},...}
 *                       (first message after subscribe = full snapshot)
 *   server → client  {"channel":"orderbook","type":"update","data":{bids,asks}}
 *                       (subsequent incremental deltas)
 *   client → server  {"op":"ping"}                  (every 15s heartbeat)
 *   server → client  {"type":"pong"}                (response — ignored)
 *
 * Per-level fields are PLAIN decimal strings (NOT x18 like Nado):
 *   {"price":"75960.9","quantity":"0.000263","order_count":1}
 *
 * In an update message a level with quantity=="0" means "remove".
 *
 * Markets are addressed by integer market_id, not by string symbol —
 * the discovery loader provides {symbol, market_id} pairs where the
 * symbol is the canonical RISEx form like "BTC/USDC". That same string
 * is what app/risex_client.py uses internally and what the bot config
 * carries as instrument_a / instrument_b, so books are stored and
 * served under the full pair name without any token-level rewriting.
 */

import { DurableObject } from "cloudflare:workers";
import type { Env, BookSnapshot } from "../types";

interface SideBook {
  bids: Array<[number, number]>; // sorted descending
  asks: Array<[number, number]>; // sorted ascending
  ts_ms: number;
  connected: boolean;
  updates: number;
}

function emptyBook(): SideBook {
  return { bids: [], asks: [], ts_ms: 0, connected: false, updates: 0 };
}

const TOP_N = 10;
const ALARM_INTERVAL_MS = 30_000;
const HEARTBEAT_INTERVAL_MS = 15_000;
// Cloudflare Workers WebSocket client expects an https:// URL on fetch();
// the runtime negotiates the upgrade for us. wss:// would hand back a 403/501.
const WS_URL = "https://ws.rise.trade/ws";
const USER_AGENT = "tradeautonom-oms-v2/0.1";
/** If we get nothing from the upstream for this long, force a reconnect. */
const STALE_WS_MS = 90_000;

interface HealthSnapshot {
  status: string;
  ws_state: "connected" | "disconnected" | "connecting";
  reconnect_attempts: number;
  last_message_ms: number | null;
  last_alarm_ms: number | null;
  tracked_markets: number;
  markets_with_data: number;
  total_updates: number;
  uptime_ms: number;
}

interface RisexLevel {
  price?: string | number;
  quantity?: string | number;
  order_count?: number;
}

interface RisexMessage {
  channel?: string;
  type?: string;
  data?: {
    market_id?: number | string;
    bids?: RisexLevel[];
    asks?: RisexLevel[];
  };
}

export class RisexOms extends DurableObject<Env> {
  /** Symbol (token, e.g. "BTC") → SideBook. */
  private books: Map<string, SideBook> = new Map();
  /** "BTC" → 1, "ETH" → 2, ... */
  private symbolToMarketId: Map<string, number> = new Map();
  /** 1 → "BTC", 2 → "ETH", ... */
  private marketIdToSymbol: Map<number, string> = new Map();

  private ws: WebSocket | null = null;
  private wsState: "connected" | "disconnected" | "connecting" = "disconnected";
  private reconnectAttempts = 0;
  private lastMessageMs: number | null = null;
  private lastAlarmMs: number | null = null;
  private startedAt: number = Date.now();
  /** Heartbeat timer handle — cleared on close/reconnect. */
  private heartbeatHandle: number | null = null;
  /** True when ensureTracking has added new markets and we need to resubscribe. */
  private pendingResync: boolean = false;

  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    state.blockConcurrencyWhile(async () => {
      // Restore tracked market_id map across hibernation. The discovery cron
      // re-pushes the full set every 15 min anyway, so a stale set here
      // self-heals quickly if upstream changes the market list.
      //
      // Migration: an earlier revision of this DO keyed the map by base
      // token only ("BTC" → 1) instead of the canonical pair ("BTC/USDC").
      // Drop any legacy single-token entries on cold start; the next
      // discovery tick rebuilds the map with the correct keys.
      const stored =
        (await state.storage.get<Record<string, number>>("market_ids")) ?? {};
      for (const [sym, mid] of Object.entries(stored)) {
        if (!sym.includes("/")) continue; // legacy token-only key, skip
        this.symbolToMarketId.set(sym, mid);
        this.marketIdToSymbol.set(mid, sym);
      }
      const hadLegacy =
        Object.keys(stored).length > 0 && this.symbolToMarketId.size === 0;
      if (hadLegacy) {
        await state.storage.delete("market_ids");
        console.log("RisexOms: cleared legacy token-only market_ids storage");
      }
      if (this.symbolToMarketId.size > 0) {
        await this.ensureWs();
      }
      const current = await state.storage.getAlarm();
      if (current === null) {
        await state.storage.setAlarm(Date.now() + ALARM_INTERVAL_MS);
      }
    });
  }

  // ── RPC methods (called by AggregatorDO / discovery cron) ────────

  async getBook(market: string): Promise<BookSnapshot | null> {
    const book = this.books.get(market);
    if (!book) return null;
    return this.toSnapshot(market, book);
  }

  /**
   * Register additional markets to track. Mirrors the Nado pattern
   * (symbol + product_id) — the discovery loader resolves market_id
   * from /v1/markets so the OMS doesn't have to.
   */
  async ensureTracking(
    entries: Array<{ symbol: string; market_id: number }>,
  ): Promise<{ ok: true; added: number }> {
    let added = 0;
    for (const { symbol, market_id } of entries) {
      if (!this.symbolToMarketId.has(symbol)) {
        this.symbolToMarketId.set(symbol, market_id);
        this.marketIdToSymbol.set(market_id, symbol);
        added += 1;
      }
    }
    if (added > 0) {
      const map: Record<string, number> = {};
      for (const [s, m] of this.symbolToMarketId) map[s] = m;
      await this.ctx.storage.put("market_ids", map);
      this.pendingResync = true;
      await this.ensureWs();
    }
    return { ok: true, added };
  }

  async listMarkets(): Promise<string[]> {
    return Array.from(this.books.keys()).sort();
  }

  // ── HTTP debug surface ───────────────────────────────────────────

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;
    if (path === "/health") return this.json(this.healthSnapshot());
    if (path === "/markets") return this.json(this.marketsSnapshot());
    const bookMatch = path.match(/^\/book\/([A-Z0-9._-]+)$/i);
    if (bookMatch) {
      const market = bookMatch[1].toUpperCase();
      const snap = await this.getBook(market);
      if (!snap) return this.json({ error: "market not tracked", market }, 404);
      return this.json({ ...snap, age_ms: Date.now() - snap.timestamp_ms });
    }
    return this.json({ error: "not found", path }, 404);
  }

  // ── Alarm (30s heartbeat) ────────────────────────────────────────

  async alarm(): Promise<void> {
    this.lastAlarmMs = Date.now();
    let totalUpdates = 0;
    for (const b of this.books.values()) totalUpdates += b.updates;
    console.log("RisexOms alarm", {
      ws_state: this.wsState,
      tracked: this.symbolToMarketId.size,
      with_data: this.books.size,
      total_updates: totalUpdates,
      age_ms: this.lastMessageMs ? Date.now() - this.lastMessageMs : null,
    });
    await this.ensureWs();
    await this.ctx.storage.setAlarm(Date.now() + ALARM_INTERVAL_MS);
  }

  // ── WebSocket lifecycle ──────────────────────────────────────────

  private async ensureWs(): Promise<void> {
    if (this.symbolToMarketId.size === 0) return;
    if (this.wsState === "connecting") return;

    if (this.ws && this.wsState === "connected" && !this.pendingResync) {
      if (
        this.lastMessageMs !== null &&
        Date.now() - this.lastMessageMs > STALE_WS_MS
      ) {
        console.warn("RisexOms WS stale, reconnecting");
        try {
          this.ws.close();
        } catch {
          /* ignore */
        }
        this.ws = null;
        this.wsState = "disconnected";
      } else {
        return;
      }
    }

    if (this.pendingResync && this.ws) {
      try {
        this.ws.close();
      } catch {
        /* ignore */
      }
      this.ws = null;
      this.wsState = "disconnected";
      this.pendingResync = false;
    }

    this.wsState = "connecting";
    this.reconnectAttempts += 1;
    console.log("RisexOms opening WS", {
      attempt: this.reconnectAttempts,
      url: WS_URL,
      markets: this.symbolToMarketId.size,
    });

    try {
      const resp = await fetch(WS_URL, {
        headers: { Upgrade: "websocket", "User-Agent": USER_AGENT },
      });
      if (resp.status !== 101 || !resp.webSocket) {
        console.error("RisexOms WS upgrade failed", {
          status: resp.status,
          statusText: resp.statusText,
        });
        this.wsState = "disconnected";
        return;
      }
      const ws = resp.webSocket;
      ws.accept();
      this.ws = ws;
      this.wsState = "connected";

      // RISEx gives us only one fresh snapshot per market right after
      // subscribe; reset everything so deltas don't merge with stale state.
      this.books.clear();

      ws.addEventListener("message", (event) => this.onMessage(event));
      ws.addEventListener("close", (event) => this.onClose(event));
      ws.addEventListener("error", (event) => this.onError(event));

      // Subscribe to all tracked markets in a single message — the API
      // accepts an array of market_ids per call.
      const marketIds = Array.from(this.symbolToMarketId.values());
      ws.send(
        JSON.stringify({
          method: "subscribe",
          params: { channel: "orderbook", market_ids: marketIds },
        }),
      );

      // Start the 15s heartbeat. Cancelled by onClose / next ensureWs.
      this.startHeartbeat();
      console.log("RisexOms WS subscribed", { count: marketIds.length });
    } catch (err) {
      console.error(
        "RisexOms WS open threw",
        err instanceof Error ? err.message : err,
      );
      this.wsState = "disconnected";
    }
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatHandle = setInterval(() => {
      if (this.ws && this.wsState === "connected") {
        try {
          this.ws.send(JSON.stringify({ op: "ping" }));
        } catch (err) {
          console.warn(
            "RisexOms heartbeat send failed",
            err instanceof Error ? err.message : err,
          );
        }
      }
    }, HEARTBEAT_INTERVAL_MS) as unknown as number;
  }

  private stopHeartbeat(): void {
    if (this.heartbeatHandle !== null) {
      clearInterval(this.heartbeatHandle);
      this.heartbeatHandle = null;
    }
  }

  private onMessage(event: MessageEvent): void {
    this.lastMessageMs = Date.now();
    const raw =
      typeof event.data === "string"
        ? event.data
        : new TextDecoder().decode(event.data as ArrayBuffer);
    let msg: RisexMessage;
    try {
      msg = JSON.parse(raw) as RisexMessage;
    } catch {
      return;
    }

    // Skip subscription acks and pongs.
    const t = msg.type;
    if (t === "subscribed" || t === "pong") return;

    // Some pong replies come without a channel — anything that's not
    // an orderbook event we ignore quietly.
    if (msg.channel !== "orderbook" || !msg.data) return;

    const midRaw = msg.data.market_id;
    const marketId =
      typeof midRaw === "string" ? Number.parseInt(midRaw, 10) : midRaw ?? -1;
    if (!Number.isFinite(marketId) || marketId < 0) return;
    const symbol = this.marketIdToSymbol.get(marketId);
    if (!symbol) return; // Untracked market.

    let book = this.books.get(symbol);
    if (!book) {
      book = emptyBook();
      this.books.set(symbol, book);
    }

    const isUpdate = t === "update";
    if (isUpdate) {
      if (msg.data.bids?.length)
        applyRisexDelta(book.bids, msg.data.bids, /* descending= */ true);
      if (msg.data.asks?.length)
        applyRisexDelta(book.asks, msg.data.asks, /* descending= */ false);
    } else {
      // First message after subscribe (or any non-update event with data) is
      // a full snapshot — replace both sides.
      book.bids = parseSnapshot(msg.data.bids, /* descending= */ true);
      book.asks = parseSnapshot(msg.data.asks, /* descending= */ false);
    }
    book.bids = book.bids.slice(0, TOP_N);
    book.asks = book.asks.slice(0, TOP_N);
    book.ts_ms = Date.now();
    book.updates += 1;
    book.connected = true;

    // Fan-out to AggregatorDO + ArbScannerDO, fire-and-forget.
    const snap = this.toSnapshot(symbol, book);
    this.fanOut(snap);
  }

  private fanOut(snap: BookSnapshot): void {
    const agg = this.env.AGGREGATOR_DO.get(
      this.env.AGGREGATOR_DO.idFromName("aggregator"),
    );
    const scanner = this.env.ARB_SCANNER.get(
      this.env.ARB_SCANNER.idFromName("singleton"),
    );
    void Promise.allSettled([
      agg.onBookUpdate(snap),
      scanner.onBookUpdate(snap),
    ]).then((results) => {
      for (const r of results) {
        if (r.status === "rejected" && Math.random() < 0.001) {
          const m =
            r.reason instanceof Error ? r.reason.message : String(r.reason);
          console.warn("RisexOms fanout failed", m);
        }
      }
    });
  }

  private onClose(event: CloseEvent): void {
    console.warn("RisexOms WS closed", {
      code: event.code,
      reason: event.reason,
    });
    this.ws = null;
    this.wsState = "disconnected";
    this.stopHeartbeat();
    for (const b of this.books.values()) b.connected = false;
  }

  private onError(event: Event): void {
    console.error("RisexOms WS error", event.type);
  }

  // ── Snapshot helpers ─────────────────────────────────────────────

  private toSnapshot(market: string, book: SideBook): BookSnapshot {
    return {
      exchange: "risex",
      symbol: market,
      bids: book.bids,
      asks: book.asks,
      timestamp_ms: book.ts_ms,
      connected: book.connected && this.wsState === "connected",
      updates: book.updates,
      // RISEx does not expose a sequence number on its WS feed, so we
      // mirror updates here just to keep the field populated.
      last_seq: book.updates,
    };
  }

  private healthSnapshot(): HealthSnapshot {
    let total = 0;
    for (const b of this.books.values()) total += b.updates;
    return {
      status: "ok",
      ws_state: this.wsState,
      reconnect_attempts: this.reconnectAttempts,
      last_message_ms: this.lastMessageMs,
      last_alarm_ms: this.lastAlarmMs,
      tracked_markets: this.symbolToMarketId.size,
      markets_with_data: this.books.size,
      total_updates: total,
      uptime_ms: Date.now() - this.startedAt,
    };
  }

  private marketsSnapshot() {
    const now = Date.now();
    const markets: unknown[] = [];
    for (const [symbol, book] of this.books) {
      markets.push({
        symbol,
        market_id: this.symbolToMarketId.get(symbol),
        connected: book.connected,
        updates: book.updates,
        age_ms: book.ts_ms ? now - book.ts_ms : null,
        bid: book.bids[0]?.[0] ?? null,
        ask: book.asks[0]?.[0] ?? null,
        bid_levels: book.bids.length,
        ask_levels: book.asks.length,
      });
    }
    markets.sort(
      (a, b) =>
        (a as { symbol: string }).symbol.localeCompare(
          (b as { symbol: string }).symbol,
        ),
    );
    return { total: markets.length, markets };
  }

  private json(data: unknown, status = 200): Response {
    return new Response(JSON.stringify(data, null, 2), {
      status,
      headers: { "content-type": "application/json" },
    });
  }
}

// ── Pure helpers ──────────────────────────────────────────────────

function parseSnapshot(
  src: RisexLevel[] | undefined,
  descending: boolean,
): Array<[number, number]> {
  if (!src) return [];
  const out: Array<[number, number]> = [];
  for (const lv of src) {
    if (lv.price === undefined || lv.price === null) continue;
    if (lv.quantity === undefined || lv.quantity === null) continue;
    const price = Number(lv.price);
    const qty = Number(lv.quantity);
    if (!Number.isFinite(price) || !Number.isFinite(qty)) continue;
    if (qty <= 0) continue;
    out.push([price, qty]);
  }
  out.sort((a, b) => (descending ? b[0] - a[0] : a[0] - b[0]));
  return out;
}

function applyRisexDelta(
  side: Array<[number, number]>,
  updates: RisexLevel[],
  descending: boolean,
): void {
  // Rebuild via a price → level map so duplicates are handled correctly.
  const map = new Map<number, number>();
  for (const lv of side) map.set(lv[0], lv[1]);
  for (const u of updates) {
    if (u.price === undefined || u.price === null) continue;
    if (u.quantity === undefined || u.quantity === null) continue;
    const price = Number(u.price);
    const qty = Number(u.quantity);
    if (!Number.isFinite(price) || !Number.isFinite(qty)) continue;
    if (qty <= 0) {
      map.delete(price);
    } else {
      map.set(price, qty);
    }
  }
  side.length = 0;
  for (const [p, q] of map) side.push([p, q]);
  side.sort((a, b) => (descending ? b[0] - a[0] : a[0] - b[0]));
}
