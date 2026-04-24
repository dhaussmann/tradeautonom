/**
 * ExtendedOms — singleton Durable Object that holds ONE outbound WebSocket
 * to Extended's shared all-markets order book stream and keeps top-10
 * orderbooks for every active market in memory.
 *
 * Spec: https://api.docs.extended.exchange/#order-book-stream
 * Photon OMS reference: deploy/monitor/monitor_service.py::_run_extended_ws_all
 *
 * Ingestion pattern (production-stable in Photon OMS for months):
 *   wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks
 *   (no market segment → server pushes every active market on one connection)
 *
 * Fan-out: every book update is pushed via RPC to AggregatorDO for
 * subscriber fan-out. Future phases will add ArbScannerDO as a second
 * RPC consumer.
 */

import { DurableObject } from "cloudflare:workers";
import {
  emptyBook,
  applyExtendedMessage,
  Orderbook,
  ExtendedMessage,
} from "../lib/orderbook";
import type { Env, BookSnapshot } from "../types";

interface HealthSnapshot {
  status: string;
  ws_state: "connected" | "disconnected" | "connecting";
  reconnect_attempts: number;
  last_message_ms: number | null;
  last_alarm_ms: number | null;
  markets_tracked: number;
  total_updates: number;
  uptime_ms: number;
}

const ALARM_INTERVAL_MS = 30_000;
const WS_URL =
  "https://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks";
const USER_AGENT = "tradeautonom-oms-v2/0.1";

export class ExtendedOms extends DurableObject<Env> {
  private books: Map<string, Orderbook> = new Map();
  private ws: WebSocket | null = null;
  private wsState: "connected" | "disconnected" | "connecting" = "disconnected";
  private reconnectAttempts = 0;
  private lastMessageMs: number | null = null;
  private lastAlarmMs: number | null = null;
  private startedAt: number = Date.now();

  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    state.blockConcurrencyWhile(async () => {
      await this.ensureWs();
      const current = await state.storage.getAlarm();
      if (current === null) {
        await state.storage.setAlarm(Date.now() + ALARM_INTERVAL_MS);
      }
    });
  }

  // ── RPC methods (called by AggregatorDO / Worker) ────────────────

  /** Return the current book for a market, or null if we haven't seen it. */
  async getBook(market: string): Promise<BookSnapshot | null> {
    const book = this.books.get(market);
    if (!book) return null;
    return this.toSnapshot(market, book);
  }

  /**
   * Accept a list of markets the caller cares about. For Extended's shared
   * stream this is a no-op (we get all markets anyway), but we keep the
   * interface so AggregatorDO can call it uniformly across all exchanges.
   */
  async ensureTracking(_markets: string[]): Promise<{ ok: true }> {
    return { ok: true };
  }

  /** List all markets we currently have data for. */
  async listMarkets(): Promise<string[]> {
    return Array.from(this.books.keys()).sort();
  }

  // ── HTTP endpoints (for direct debugging, not the main Bot path) ─

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;

    if (path === "/health") return this.json(this.healthSnapshot());
    if (path === "/markets") return this.json(this.marketsSnapshot());

    const bookMatch = path.match(/^\/book\/([A-Z0-9._-]+)$/i);
    if (bookMatch) {
      const market = bookMatch[1].toUpperCase();
      const snap = await this.getBook(market);
      if (!snap) {
        return this.json({ error: "market not tracked", market }, 404);
      }
      return this.json({ ...snap, age_ms: Date.now() - snap.timestamp_ms });
    }

    return this.json({ error: "not found", path }, 404);
  }

  // ── Alarm (30s heartbeat + reconnect) ───────────────────────────

  async alarm(): Promise<void> {
    this.lastAlarmMs = Date.now();
    const totalUpdates = Array.from(this.books.values()).reduce(
      (s, b) => s + b.updates,
      0,
    );
    console.log("alarm fired", {
      ws_state: this.wsState,
      markets: this.books.size,
      total_updates: totalUpdates,
      age_ms: this.lastMessageMs ? Date.now() - this.lastMessageMs : null,
    });
    await this.ensureWs();
    await this.ctx.storage.setAlarm(Date.now() + ALARM_INTERVAL_MS);
  }

  // ── WebSocket lifecycle ──────────────────────────────────────────

  private async ensureWs(): Promise<void> {
    if (this.wsState === "connecting") return;
    if (this.ws && this.wsState === "connected") {
      if (this.lastMessageMs && Date.now() - this.lastMessageMs > 90_000) {
        console.warn("WS stale, closing");
        try { this.ws.close(); } catch { /* ignore */ }
        this.ws = null;
        this.wsState = "disconnected";
      } else {
        return;
      }
    }

    this.wsState = "connecting";
    this.reconnectAttempts += 1;
    console.log("opening WS", { attempt: this.reconnectAttempts, url: WS_URL });

    try {
      const resp = await fetch(WS_URL, {
        headers: { Upgrade: "websocket", "User-Agent": USER_AGENT },
      });

      if (resp.status !== 101 || !resp.webSocket) {
        console.error("WS upgrade failed", {
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
      this.books.clear();
      console.log("WS connected (all-markets shared stream)");

      ws.addEventListener("message", (event) => this.onMessage(event));
      ws.addEventListener("close", (event) => this.onClose(event));
      ws.addEventListener("error", (event) => this.onError(event));
    } catch (err) {
      console.error("WS open threw", err instanceof Error ? err.message : err);
      this.wsState = "disconnected";
    }
  }

  private onMessage(event: MessageEvent): void {
    this.lastMessageMs = Date.now();
    const raw =
      typeof event.data === "string"
        ? event.data
        : new TextDecoder().decode(event.data as ArrayBuffer);

    let msg: ExtendedMessage;
    try {
      msg = JSON.parse(raw) as ExtendedMessage;
    } catch {
      return;
    }

    if (!msg.data || !msg.data.m) return;
    if (msg.type !== "SNAPSHOT" && msg.type !== "DELTA") return;

    const market = msg.data.m;
    let book = this.books.get(market);
    if (!book) {
      book = emptyBook();
      book.connected = true;
      this.books.set(market, book);
    }

    applyExtendedMessage(book, msg);

    // Fan-out to AggregatorDO. Fire-and-forget: don't let RPC errors block
    // the hot ingestion path.
    const snap = this.toSnapshot(market, book);
    this.fanOut(snap);
  }

  private fanOut(snap: BookSnapshot): void {
    // Parallel fan-out: AggregatorDO (bot /ws subscribers) + ArbScannerDO
    // (cross-exchange arb opportunities + /ws/arb). Both are fire-and-forget;
    // one DO being slow won't block the other. Phase C.
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
          const msg = r.reason instanceof Error ? r.reason.message : String(r.reason);
          console.warn("fanout failed", msg);
        }
      }
    });
  }

  private onClose(event: CloseEvent): void {
    console.warn("WS closed", { code: event.code, reason: event.reason });
    this.ws = null;
    this.wsState = "disconnected";
    for (const book of this.books.values()) book.connected = false;
  }

  private onError(event: Event): void {
    console.error("WS error", event.type);
  }

  // ── Snapshot helpers ─────────────────────────────────────────────

  private toSnapshot(market: string, book: Orderbook): BookSnapshot {
    return {
      exchange: "extended",
      symbol: market,
      bids: book.bids,
      asks: book.asks,
      timestamp_ms: book.ts_ms,
      connected: book.connected && this.wsState === "connected",
      updates: book.updates,
      last_seq: book.last_seq,
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
      markets_tracked: this.books.size,
      total_updates: total,
      uptime_ms: Date.now() - this.startedAt,
    };
  }

  private marketsSnapshot() {
    const now = Date.now();
    const markets = [];
    for (const [symbol, book] of this.books) {
      markets.push({
        symbol,
        connected: book.connected,
        updates: book.updates,
        last_seq: book.last_seq,
        age_ms: book.ts_ms ? now - book.ts_ms : null,
        bid: book.bids[0]?.[0] ?? null,
        ask: book.asks[0]?.[0] ?? null,
        bid_levels: book.bids.length,
        ask_levels: book.asks.length,
      });
    }
    markets.sort((a, b) => a.symbol.localeCompare(b.symbol));
    return { total: markets.length, markets };
  }

  private json(data: unknown, status = 200): Response {
    return new Response(JSON.stringify(data, null, 2), {
      status,
      headers: { "content-type": "application/json" },
    });
  }
}
