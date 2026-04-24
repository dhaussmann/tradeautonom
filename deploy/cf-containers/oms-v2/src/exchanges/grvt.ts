/**
 * GrvtOms — singleton DO holding ONE outbound WebSocket to GRVT market-data.
 *
 * Spec: https://api-docs.grvt.io + Photon OMS reference _run_grvt_ws_all.
 * WebSocket: wss://market-data.grvt.io/ws/full
 *
 * Protocol:
 *   - Public feed, no auth.
 *   - Single message subscribes to all symbols:
 *       {"jsonrpc":"2.0","method":"subscribe","params":{"stream":"v1.book.s","selectors":["BTC_USDT_Perp@500-10", ...]},"id":1}
 *   - Incoming messages under msg.feed.{instrument, bids:[{price,size}], asks:[{price,size}]}
 *   - Each message is a FULL snapshot (top 10), not deltas. Replace book entirely.
 *
 * Symbols are discovered on demand via ensureTracking() — no initial symbol list.
 * When symbols are added, the WS is reconnected with the updated selector list
 * (simplest correctness path; GRVT's subscribe message replaces the subscription).
 */

import { DurableObject } from "cloudflare:workers";
import type { Env, BookSnapshot } from "../types";

// CF Workers fetch() requires https:// even for WebSocket upgrades.
const WS_URL = "https://market-data.grvt.io/ws/full";
const ALARM_INTERVAL_MS = 30_000;

interface Book {
  bids: Array<[number, number]>;
  asks: Array<[number, number]>;
  ts_ms: number;
  connected: boolean;
  updates: number;
}

function emptyBook(): Book {
  return { bids: [], asks: [], ts_ms: 0, connected: false, updates: 0 };
}

export class GrvtOms extends DurableObject<Env> {
  private books: Map<string, Book> = new Map();
  private trackedSymbols: Set<string> = new Set();
  private ws: WebSocket | null = null;
  private wsState: "connected" | "disconnected" | "connecting" = "disconnected";
  private reconnectAttempts = 0;
  private lastMessageMs: number | null = null;
  private startedAt: number = Date.now();
  private pendingReconnect: boolean = false;

  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    state.blockConcurrencyWhile(async () => {
      const stored = (await state.storage.get<string[]>("tracked")) ?? [];
      for (const s of stored) this.trackedSymbols.add(s);
      if (this.trackedSymbols.size > 0) {
        await this.ensureWs();
      }
      const current = await state.storage.getAlarm();
      if (current === null) {
        await state.storage.setAlarm(Date.now() + ALARM_INTERVAL_MS);
      }
    });
  }

  // ── RPC ──────────────────────────────────────────────────────────

  async getBook(market: string): Promise<BookSnapshot | null> {
    const b = this.books.get(market);
    if (!b) return null;
    return this.toSnapshot(market, b);
  }

  async ensureTracking(markets: string[]): Promise<{ ok: true; added: number }> {
    let added = 0;
    for (const m of markets) {
      if (!this.trackedSymbols.has(m)) {
        this.trackedSymbols.add(m);
        added += 1;
      }
    }
    if (added > 0) {
      await this.ctx.storage.put("tracked", Array.from(this.trackedSymbols));
      // Reconnect to refresh subscription list.
      this.pendingReconnect = true;
      await this.ensureWs();
    }
    return { ok: true, added };
  }

  async listMarkets(): Promise<string[]> {
    return Array.from(this.books.keys()).sort();
  }

  // ── HTTP (debugging) ─────────────────────────────────────────────

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;
    if (path === "/health") {
      return this.json({
        status: "ok",
        ws_state: this.wsState,
        reconnect_attempts: this.reconnectAttempts,
        last_message_ms: this.lastMessageMs,
        tracked_symbols: this.trackedSymbols.size,
        markets_with_data: this.books.size,
        uptime_ms: Date.now() - this.startedAt,
      });
    }
    return this.json({ error: "not found", path }, 404);
  }

  async alarm(): Promise<void> {
    await this.ensureWs();
    await this.ctx.storage.setAlarm(Date.now() + ALARM_INTERVAL_MS);
  }

  // ── WS lifecycle ────────────────────────────────────────────────

  private async ensureWs(): Promise<void> {
    if (this.wsState === "connecting") return;
    if (this.trackedSymbols.size === 0) return;

    if (this.ws && this.wsState === "connected" && !this.pendingReconnect) {
      if (this.lastMessageMs && Date.now() - this.lastMessageMs > 120_000) {
        console.warn("GRVT WS stale, closing");
        try { this.ws.close(); } catch { /* ignore */ }
        this.ws = null;
        this.wsState = "disconnected";
      } else {
        return;
      }
    }

    // Close existing connection if we're reconnecting to refresh subscriptions.
    if (this.pendingReconnect && this.ws) {
      try { this.ws.close(); } catch { /* ignore */ }
      this.ws = null;
      this.wsState = "disconnected";
      this.pendingReconnect = false;
    }

    this.wsState = "connecting";
    this.reconnectAttempts += 1;
    console.log("GRVT opening WS", {
      attempt: this.reconnectAttempts,
      symbols: this.trackedSymbols.size,
    });

    try {
      const resp = await fetch(WS_URL, {
        headers: { Upgrade: "websocket", "User-Agent": "tradeautonom-oms-v2/0.1" },
      });

      if (resp.status !== 101 || !resp.webSocket) {
        const bodyPreview = await resp.text().catch(() => "");
        console.error("GRVT WS upgrade failed", {
          status: resp.status,
          statusText: resp.statusText,
          bodyPreview: bodyPreview.slice(0, 200),
        });
        this.wsState = "disconnected";
        return;
      }

      const ws = resp.webSocket;
      ws.accept();
      this.ws = ws;
      this.wsState = "connected";

      // Send subscribe for all tracked symbols in one message.
      const selectors = Array.from(this.trackedSymbols).map(s => `${s}@500-10`);
      const subMsg = JSON.stringify({
        jsonrpc: "2.0",
        method: "subscribe",
        params: { stream: "v1.book.s", selectors },
        id: 1,
      });
      ws.send(subMsg);
      console.log("GRVT subscribed", { selectors: selectors.length });

      ws.addEventListener("message", (e) => this.onMessage(e));
      ws.addEventListener("close", (e) => this.onClose(e));
      ws.addEventListener("error", (e) => this.onError(e));
    } catch (err) {
      console.error("GRVT WS open threw", err instanceof Error ? err.message : err);
      this.wsState = "disconnected";
    }
  }

  private onMessage(event: MessageEvent): void {
    this.lastMessageMs = Date.now();
    const raw = typeof event.data === "string"
      ? event.data
      : new TextDecoder().decode(event.data as ArrayBuffer);

    let msg: any;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }

    // Subscribe ack has no "feed" field.
    const feed = msg?.feed;
    if (!feed) return;

    const instrument: string | undefined = feed.instrument;
    if (!instrument) return;
    if (!this.trackedSymbols.has(instrument)) return;

    const bids = parseLevels(feed.bids);
    const asks = parseLevels(feed.asks);
    if (bids.length === 0 && asks.length === 0) return;

    let book = this.books.get(instrument);
    if (!book) {
      book = emptyBook();
      this.books.set(instrument, book);
    }
    // GRVT publishes full top-N snapshots; replace book entirely.
    book.bids = bids.sort((a, b) => b[0] - a[0]);
    book.asks = asks.sort((a, b) => a[0] - b[0]);
    book.ts_ms = Date.now();
    book.connected = true;
    book.updates += 1;

    this.fanOut(this.toSnapshot(instrument, book));
  }

  private fanOut(snap: BookSnapshot): void {
    // Parallel fan-out to AggregatorDO (bots) + ArbScannerDO (arb). See
    // extended.ts::fanOut for rationale.
    const agg = this.env.AGGREGATOR_DO.get(
      this.env.AGGREGATOR_DO.idFromName("aggregator"),
    );
    const scanner = this.env.ARB_SCANNER.get(
      this.env.ARB_SCANNER.idFromName("singleton"),
    );
    void Promise.allSettled([
      agg.onBookUpdate(snap),
      scanner.onBookUpdate(snap),
    ]);
  }

  private onClose(event: CloseEvent): void {
    console.warn("GRVT WS closed", { code: event.code, reason: event.reason });
    this.ws = null;
    this.wsState = "disconnected";
    for (const b of this.books.values()) b.connected = false;
  }

  private onError(event: Event): void {
    console.error("GRVT WS error", event.type);
  }

  private toSnapshot(market: string, book: Book): BookSnapshot {
    return {
      exchange: "grvt",
      symbol: market,
      bids: book.bids,
      asks: book.asks,
      timestamp_ms: book.ts_ms,
      connected: book.connected && this.wsState === "connected",
      updates: book.updates,
      last_seq: 0,
    };
  }

  private json(data: unknown, status = 200): Response {
    return new Response(JSON.stringify(data, null, 2), {
      status,
      headers: { "content-type": "application/json" },
    });
  }
}

function parseLevels(raw: unknown): Array<[number, number]> {
  if (!Array.isArray(raw)) return [];
  const out: Array<[number, number]> = [];
  for (const lv of raw) {
    if (!lv || typeof lv !== "object") continue;
    const p = (lv as any).price;
    const s = (lv as any).size;
    if (p === undefined || s === undefined) continue;
    const price = Number(p);
    const size = Number(s);
    if (Number.isNaN(price) || Number.isNaN(size)) continue;
    out.push([price, size]);
  }
  return out;
}
