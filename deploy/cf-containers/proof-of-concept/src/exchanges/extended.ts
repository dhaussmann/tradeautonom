/**
 * ExtendedOms — proof-of-concept Durable Object that maintains live orderbooks
 * for all Extended markets on a single shared outbound WebSocket.
 *
 * Spec: https://api.docs.extended.exchange/#order-book-stream
 * Photon OMS reference: deploy/monitor/monitor_service.py::_run_extended_ws_all
 *
 * The Extended order book stream is public — no authentication, no X-Api-Key.
 * The URL without a market path segment is Extended's convention for a shared
 * stream that pushes every active market over a single connection. Photon OMS
 * has been running this pattern in production for months; we mirror it here.
 *
 * Book sizes are capped to TOP_N (10) per side to reduce memory per market.
 *
 * Goals answered by this PoC:
 *  - outbound WebSocket from a DO works reliably?
 *  - DO stays alive with an outbound WS and no inbound HTTP requests?
 *  - freshness Exchange → DO → /book caller?
 *  - cost (GB-s) of a 24/7 outbound-WS DO?
 *
 * Not production-ready. See docs/v2-oms-cloudflare-native.md.
 */

import { DurableObject } from "cloudflare:workers";
import {
  emptyBook,
  applyExtendedMessage,
  Orderbook,
  ExtendedMessage,
} from "../lib/orderbook";

export interface Env {
  EXTENDED_OMS: DurableObjectNamespace<ExtendedOms>;
}

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
// Shared stream URL (no `{market}` segment) — pushes every active market.
const WS_URL =
  "https://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks";

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

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;

    if (path === "/health") {
      return this.jsonResponse(this.healthSnapshot());
    }

    if (path === "/markets") {
      return this.jsonResponse(this.marketsSnapshot());
    }

    // /book/<market> — any active Extended market
    const bookMatch = path.match(/^\/book\/([A-Z0-9._-]+)$/i);
    if (bookMatch) {
      const market = bookMatch[1].toUpperCase();
      const snap = this.bookSnapshot(market);
      if (!snap) {
        return this.jsonResponse(
          { error: "market not tracked yet", market, hint: "check /markets for known symbols" },
          404,
        );
      }
      return this.jsonResponse(snap);
    }

    return this.jsonResponse({ error: "not found", path }, 404);
  }

  async alarm(): Promise<void> {
    this.lastAlarmMs = Date.now();
    const totalUpdates = Array.from(this.books.values()).reduce(
      (sum, b) => sum + b.updates,
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

  private async ensureWs(): Promise<void> {
    if (this.wsState === "connecting") return;
    if (this.ws && this.wsState === "connected") {
      if (this.lastMessageMs && Date.now() - this.lastMessageMs > 90_000) {
        console.warn("WS stale, closing");
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

    this.wsState = "connecting";
    this.reconnectAttempts += 1;
    console.log("opening WS", {
      attempt: this.reconnectAttempts,
      url: WS_URL,
    });

    try {
      const resp = await fetch(WS_URL, {
        headers: {
          Upgrade: "websocket",
          "User-Agent": "tradeautonom-oms-v2-poc/0.0.1",
        },
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
      // Reset all tracked books on reconnect — snapshots will re-seed them.
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
      console.warn("non-JSON message", raw.slice(0, 120));
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
    // Note: we don't enforce per-market seq continuity on the shared stream
    // because Extended interleaves seq numbers across all markets. The
    // one-per-minute SNAPSHOT self-heals any drift. Photon OMS takes the
    // same approach in production.
  }

  private onClose(event: CloseEvent): void {
    console.warn("WS closed", { code: event.code, reason: event.reason });
    this.ws = null;
    this.wsState = "disconnected";
    // Mark all books as disconnected until the next reconnect repopulates them.
    for (const book of this.books.values()) {
      book.connected = false;
    }
  }

  private onError(event: Event): void {
    console.error("WS error", event.type);
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
    const markets: Array<{
      symbol: string;
      connected: boolean;
      updates: number;
      last_seq: number;
      age_ms: number | null;
      bid: number | null;
      ask: number | null;
      bid_levels: number;
      ask_levels: number;
    }> = [];

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
    return {
      total: markets.length,
      markets,
    };
  }

  private bookSnapshot(market: string) {
    const book = this.books.get(market);
    if (!book) return null;
    const ageMs = book.ts_ms ? Date.now() - book.ts_ms : null;
    return {
      exchange: "extended",
      symbol: market,
      bids: book.bids,
      asks: book.asks,
      timestamp_ms: book.ts_ms,
      received_ms: book.received_ms,
      age_ms: ageMs,
      connected: book.connected && this.wsState === "connected",
      updates: book.updates,
      last_seq: book.last_seq,
    };
  }

  private jsonResponse(data: unknown, status = 200): Response {
    return new Response(JSON.stringify(data, null, 2), {
      status,
      headers: { "content-type": "application/json" },
    });
  }
}
