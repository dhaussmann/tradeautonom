/**
 * ExtendedOms — proof-of-concept Durable Object that maintains a live
 * orderbook for a single Extended market via outbound WebSocket.
 *
 * Goal: answer these questions (see README.md):
 *  - Does `fetch(url, { headers: { Upgrade: "websocket", "X-Api-Key": ... }})` work?
 *  - Does the DO stay alive with an outbound WS and no inbound requests?
 *  - What's end-to-end latency Exchange → DO → /book caller?
 *
 * NOT production-ready. See the OMS-v2 design in docs/v2-oms-cloudflare-native.md.
 */

import { DurableObject } from "cloudflare:workers";
import {
  emptyBook,
  applyExtendedMessage,
  Orderbook,
} from "../lib/orderbook";

interface Env {
  EXTENDED_OMS: DurableObjectNamespace<ExtendedOms>;
  EXTENDED_API_KEY: string;
}

// Minimal shape of the health snapshot we return on /health.
interface HealthSnapshot {
  status: string;
  market: string;
  ws_state: "connected" | "disconnected" | "connecting";
  reconnect_attempts: number;
  last_message_ms: number | null;
  last_alarm_ms: number | null;
  updates: number;
  uptime_ms: number;
}

const MARKET = "BTC-USD"; // single-market PoC
const ALARM_INTERVAL_MS = 30_000;
const WS_URL =
  "https://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks";

export class ExtendedOms extends DurableObject<Env> {
  private book: Orderbook = emptyBook();
  private ws: WebSocket | null = null;
  private wsState: "connected" | "disconnected" | "connecting" = "disconnected";
  private reconnectAttempts = 0;
  private lastMessageMs: number | null = null;
  private lastAlarmMs: number | null = null;
  private startedAt: number = Date.now();

  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    // Block concurrency while we ensure the WS is connected and the alarm is set.
    // Without this, the first incoming request could race with the WS setup.
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

    // /book/<market> — only BTC-USD supported in PoC
    const bookMatch = path.match(/^\/book\/([A-Z0-9-]+)$/i);
    if (bookMatch) {
      const market = bookMatch[1].toUpperCase();
      if (market !== MARKET) {
        return this.jsonResponse({ error: `PoC only supports ${MARKET}` }, 400);
      }
      return this.jsonResponse(this.bookSnapshot());
    }

    return this.jsonResponse({ error: "not found", path }, 404);
  }

  // Alarm fires at ALARM_INTERVAL_MS. We use it to:
  // 1. Reconnect the WS if closed
  // 2. Record the timestamp for visibility in /health
  async alarm(): Promise<void> {
    this.lastAlarmMs = Date.now();
    console.log("alarm fired", {
      ws_state: this.wsState,
      updates: this.book.updates,
      age_ms: this.lastMessageMs ? Date.now() - this.lastMessageMs : null,
    });
    await this.ensureWs();
    await this.ctx.storage.setAlarm(Date.now() + ALARM_INTERVAL_MS);
  }

  private async ensureWs(): Promise<void> {
    if (this.wsState === "connecting") return;
    if (this.ws && this.wsState === "connected") {
      // Readiness check: if we have had no messages in > 90s, assume broken.
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
    console.log("opening WS", { attempt: this.reconnectAttempts });

    try {
      const resp = await fetch(WS_URL, {
        headers: {
          Upgrade: "websocket",
          "X-Api-Key": this.env.EXTENDED_API_KEY,
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
      console.log("WS connected");

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

    let msg: any;
    try {
      msg = JSON.parse(raw);
    } catch {
      console.warn("non-JSON message", raw.slice(0, 120));
      return;
    }

    // Extended sometimes sends envelope objects; real orderbook messages have `m` + `type`.
    if (msg.m === MARKET && (msg.type === "SNAPSHOT" || msg.type === "DELTA")) {
      applyExtendedMessage(this.book, msg);
    }
    // Other markets are ignored in the PoC (Extended pushes every market on the shared WS).
  }

  private onClose(event: CloseEvent): void {
    console.warn("WS closed", { code: event.code, reason: event.reason });
    this.ws = null;
    this.wsState = "disconnected";
    // Reconnect will happen on next alarm (<= 30s).
  }

  private onError(event: Event): void {
    console.error("WS error", event.type);
  }

  private healthSnapshot(): HealthSnapshot {
    return {
      status: "ok",
      market: MARKET,
      ws_state: this.wsState,
      reconnect_attempts: this.reconnectAttempts,
      last_message_ms: this.lastMessageMs,
      last_alarm_ms: this.lastAlarmMs,
      updates: this.book.updates,
      uptime_ms: Date.now() - this.startedAt,
    };
  }

  private bookSnapshot() {
    const ageMs = this.book.ts_ms ? Date.now() - this.book.ts_ms : null;
    return {
      exchange: "extended",
      symbol: MARKET,
      bids: this.book.bids,
      asks: this.book.asks,
      timestamp_ms: this.book.ts_ms,
      age_ms: ageMs,
      connected: this.book.connected || this.wsState === "connected",
      updates: this.book.updates,
    };
  }

  private jsonResponse(data: unknown, status = 200): Response {
    return new Response(JSON.stringify(data, null, 2), {
      status,
      headers: { "content-type": "application/json" },
    });
  }
}
