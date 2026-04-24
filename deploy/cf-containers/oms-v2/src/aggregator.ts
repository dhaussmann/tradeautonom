/**
 * AggregatorDO — hibernation-enabled WebSocket server for bot subscribers.
 *
 * Role:
 *  - Accept WebSocket connections from bot clients (V1-compatible protocol).
 *  - Maintain per-connection subscription list in serializable attachment.
 *  - Receive onBookUpdate RPCs from ExchangeOms DOs and fan out to subs.
 *  - Serve REST book/status/tracked endpoints.
 *
 * V1-compatible bot wire protocol (deploy/monitor/monitor_service.py):
 *   Client → Server: {action:"subscribe"|"unsubscribe", exchange, symbol}
 *   Server → Client: {type:"subscribed", exchange, symbol}
 *   Server → Client: {type:"book", exchange, symbol, bids, asks, timestamp_ms}
 *
 * Hibernation rationale: AggregatorDO has no outbound WebSocket (exchange WS
 * lives in ExchangeOms DOs). It can hibernate while bots stay connected.
 * onBookUpdate RPCs wake it up when fan-out is needed. Per-WS state
 * (subscriptions) survives hibernation via serializeAttachment (max 2 KB/ws).
 *
 * Routing between exchanges → ExchangeOms namespace binding:
 *   "extended" → env.EXTENDED_OMS
 *   (future: "grvt", "nado", "variational")
 */

import { DurableObject } from "cloudflare:workers";
import type {
  Env,
  BookSnapshot,
  ClientMessage,
  WsAttachment,
} from "./types";

const MAX_SUBS_PER_WS = 200;
const HEALTHY_DO_EXCHANGES = ["extended"] as const;
type SupportedExchange = (typeof HEALTHY_DO_EXCHANGES)[number];

export class AggregatorDO extends DurableObject<Env> {
  // Nothing here is stored as instance state — attachments + SQLite are the
  // source of truth across hibernations. The `books` cache below is a
  // warm-path optimization only.

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;

    // Bot-client WebSocket entry point
    if (path === "/ws") {
      return this.handleWsUpgrade(req);
    }

    // REST endpoints
    if (path === "/health") {
      return this.json({
        status: "ok",
        connected_bots: this.ctx.getWebSockets().length,
        exchanges: HEALTHY_DO_EXCHANGES,
      });
    }

    if (path === "/tracked") {
      return this.json(await this.trackedSnapshot());
    }

    if (path === "/status") {
      return this.json(await this.statusSnapshot());
    }

    const bookMatch = path.match(/^\/book\/([a-z]+)\/([A-Z0-9._-]+)$/i);
    if (bookMatch) {
      const exchange = bookMatch[1].toLowerCase();
      const symbol = bookMatch[2].toUpperCase();
      const book = await this.fetchBookFromExchange(exchange, symbol);
      if (!book) {
        return this.json({ error: `No feed for ${exchange}:${symbol}` }, 404);
      }
      return this.json({
        exchange: book.exchange,
        symbol: book.symbol,
        bids: book.bids,
        asks: book.asks,
        timestamp_ms: book.timestamp_ms,
        connected: book.connected,
        updates: book.updates,
      });
    }

    return this.json({ error: "not found", path }, 404);
  }

  // ── RPC (called by ExchangeOms on every book update) ────────────

  /**
   * ExchangeOms pushes book snapshots here. We look up all subscribers for
   * this (exchange, symbol) key and forward the payload. No SQL write in
   * the hot path — subscriber lookup walks WebSocket attachments (in memory
   * while the DO is awake; restored on wake-up from attachment bytes).
   */
  async onBookUpdate(snap: BookSnapshot): Promise<void> {
    const key = `${snap.exchange}:${snap.symbol}`;
    const payload = JSON.stringify({
      type: "book",
      exchange: snap.exchange,
      symbol: snap.symbol,
      bids: snap.bids,
      asks: snap.asks,
      timestamp_ms: snap.timestamp_ms,
    });

    for (const ws of this.ctx.getWebSockets()) {
      const att = this.readAttachment(ws);
      if (att?.subs.includes(key)) {
        try {
          ws.send(payload);
        } catch {
          // Closed mid-write; ignore.
        }
      }
    }
  }

  // ── WebSocket lifecycle (hibernation-compatible) ────────────────

  private handleWsUpgrade(req: Request): Response {
    const upgrade = req.headers.get("Upgrade");
    if (!upgrade || upgrade.toLowerCase() !== "websocket") {
      return new Response("Expected WebSocket upgrade", { status: 426 });
    }

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair) as [WebSocket, WebSocket];

    // Hibernation-compatible accept
    this.ctx.acceptWebSocket(server);

    const att: WsAttachment = {
      subs: [],
      connected_at: Date.now(),
    };
    server.serializeAttachment(att);

    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
    const raw = typeof message === "string"
      ? message
      : new TextDecoder().decode(message);

    let msg: ClientMessage;
    try {
      msg = JSON.parse(raw) as ClientMessage;
    } catch {
      this.sendJson(ws, { error: "invalid JSON" });
      return;
    }

    if (!msg || typeof msg !== "object" || !("action" in msg)) {
      this.sendJson(ws, { error: "missing action" });
      return;
    }

    const att = this.readAttachment(ws) ?? { subs: [], connected_at: Date.now() };

    switch (msg.action) {
      case "subscribe": {
        const exchange = msg.exchange?.toLowerCase();
        const symbol = msg.symbol?.toUpperCase();
        if (!exchange || !symbol) {
          this.sendJson(ws, { error: "subscribe requires exchange+symbol" });
          return;
        }
        if (!HEALTHY_DO_EXCHANGES.includes(exchange as SupportedExchange)) {
          this.sendJson(ws, { error: `unsupported exchange: ${exchange}` });
          return;
        }

        const key = `${exchange}:${symbol}`;
        if (!att.subs.includes(key)) {
          if (att.subs.length >= MAX_SUBS_PER_WS) {
            this.sendJson(ws, { error: `max ${MAX_SUBS_PER_WS} subs per ws` });
            return;
          }
          att.subs.push(key);
          ws.serializeAttachment(att);
        }

        this.sendJson(ws, { type: "subscribed", exchange, symbol });

        // Send an immediate snapshot so the client doesn't have to wait for
        // the next delta.
        const snap = await this.fetchBookFromExchange(exchange, symbol);
        if (snap && (snap.bids.length > 0 || snap.asks.length > 0)) {
          this.sendJson(ws, {
            type: "book",
            exchange: snap.exchange,
            symbol: snap.symbol,
            bids: snap.bids,
            asks: snap.asks,
            timestamp_ms: snap.timestamp_ms,
          });
        }
        return;
      }

      case "unsubscribe": {
        const exchange = msg.exchange?.toLowerCase();
        const symbol = msg.symbol?.toUpperCase();
        if (!exchange || !symbol) {
          this.sendJson(ws, { error: "unsubscribe requires exchange+symbol" });
          return;
        }
        const key = `${exchange}:${symbol}`;
        att.subs = att.subs.filter((s) => s !== key);
        ws.serializeAttachment(att);
        this.sendJson(ws, { type: "unsubscribed", exchange, symbol });
        return;
      }

      default:
        this.sendJson(ws, { error: `unknown action: ${(msg as { action: string }).action}` });
    }
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string, _wasClean: boolean): Promise<void> {
    const att = this.readAttachment(ws);
    console.log("ws closed", {
      code,
      reason,
      had_subs: att?.subs.length ?? 0,
    });
    // runtime auto-closes close frame replies with compat date >= 2026-04-07
  }

  // ── Helpers ──────────────────────────────────────────────────────

  /**
   * Fetch a book via RPC from the correct ExchangeOms DO.
   * Returns null if the exchange isn't wired up yet or the market isn't tracked.
   */
  private async fetchBookFromExchange(
    exchange: string,
    symbol: string,
  ): Promise<BookSnapshot | null> {
    if (exchange === "extended") {
      const stub = this.env.EXTENDED_OMS.get(
        this.env.EXTENDED_OMS.idFromName("singleton"),
      );
      // RPC return type widens [number, number][] → number[][]; cast back.
      const res = await stub.getBook(symbol);
      return res as BookSnapshot | null;
    }
    // Future: grvt / nado / variational
    return null;
  }

  /**
   * /tracked endpoint: map every base token to its per-exchange symbol.
   * For now only Extended is wired up, so this just lists Extended markets
   * mapped under their own symbol (no cross-exchange pairing yet). Full
   * auto-discovery lands in Phase B when multiple exchanges are live.
   */
  private async trackedSnapshot() {
    const extStub = this.env.EXTENDED_OMS.get(
      this.env.EXTENDED_OMS.idFromName("singleton"),
    );
    const extendedMarkets = await extStub.listMarkets();
    const tracked: Record<string, Record<string, string>> = {};
    for (const m of extendedMarkets) {
      // Naive token extraction: split on "-", strip optional numeric-suffixed
      // equity symbols (e.g. "AAPL_24_5-USD"). Full normalization comes in
      // Phase B with cross-exchange auto-discovery.
      const base = m.split("-")[0]!;
      if (base.includes("_")) continue; // skip equity pre-markets
      const key = base.startsWith("1000") ? base.slice(4) : base;
      tracked[key] ??= {};
      tracked[key].extended = m;
    }
    return tracked;
  }

  private async statusSnapshot() {
    const extStub = this.env.EXTENDED_OMS.get(
      this.env.EXTENDED_OMS.idFromName("singleton"),
    );
    const markets = await extStub.listMarkets();
    const now = Date.now();
    const out: Record<string, unknown> = {};

    // Parallel getBook calls for all markets. Limit to 50 to avoid overshoot.
    const sample = markets.slice(0, 200);
    const results = await Promise.all(
      sample.map((m) => extStub.getBook(m)),
    );
    for (let i = 0; i < sample.length; i++) {
      const snap = results[i];
      const m = sample[i]!;
      if (!snap) continue;
      out[`extended:${m}`] = {
        connected: snap.connected,
        has_data: snap.bids.length > 0 || snap.asks.length > 0,
        age_ms: snap.timestamp_ms ? now - snap.timestamp_ms : null,
        updates: snap.updates,
        bid_levels: snap.bids.length,
        ask_levels: snap.asks.length,
      };
    }
    return out;
  }

  private readAttachment(ws: WebSocket): WsAttachment | null {
    try {
      const raw = ws.deserializeAttachment();
      if (raw && typeof raw === "object" && Array.isArray((raw as WsAttachment).subs)) {
        return raw as WsAttachment;
      }
    } catch {
      /* ignore */
    }
    return null;
  }

  private sendJson(ws: WebSocket, data: unknown): void {
    try {
      ws.send(JSON.stringify(data));
    } catch {
      /* ignore */
    }
  }

  private json(data: unknown, status = 200): Response {
    return new Response(JSON.stringify(data, null, 2), {
      status,
      headers: { "content-type": "application/json" },
    });
  }
}
