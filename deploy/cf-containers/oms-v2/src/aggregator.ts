/**
 * AggregatorDO — hibernation-enabled WebSocket server for bot subscribers.
 *
 * Role:
 *  - Accept WebSocket connections from bot clients (V1-compatible protocol).
 *  - Maintain per-connection subscription list in serializable attachment.
 *  - Receive onBookUpdate RPCs from ExchangeOms DOs and fan out to subs.
 *  - Serve REST book/status/tracked endpoints.
 *  - Store latest discovery result (pairs + per-exchange symbol lists).
 *
 * V1-compatible bot wire protocol (deploy/monitor/monitor_service.py):
 *   Client → Server: {action:"subscribe"|"unsubscribe", exchange, symbol}
 *   Server → Client: {type:"subscribed", exchange, symbol}
 *   Server → Client: {type:"book", exchange, symbol, bids, asks, timestamp_ms}
 *
 * Hibernation rationale: AggregatorDO has no outbound WebSocket (exchange WS
 * lives in ExchangeOms DOs). It can hibernate while bots stay connected.
 * onBookUpdate RPCs wake it up when fan-out is needed.
 */

import { DurableObject } from "cloudflare:workers";
import { discoverPairs, DiscoveryResult } from "./lib/discovery";
import type {
  Env,
  BookSnapshot,
  ClientMessage,
  WsAttachment,
  DiscoveredPairs,
} from "./types";

const MAX_SUBS_PER_WS = 200;
const SUPPORTED_EXCHANGES = ["extended", "grvt", "nado", "variational"] as const;
type SupportedExchange = (typeof SUPPORTED_EXCHANGES)[number];

export class AggregatorDO extends DurableObject<Env> {
  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;

    if (path === "/ws") return this.handleWsUpgrade(req);

    if (path === "/health") {
      return this.json({
        status: "ok",
        connected_bots: this.ctx.getWebSockets().length,
        exchanges: SUPPORTED_EXCHANGES,
      });
    }

    if (path === "/tracked") {
      return this.json(await this.trackedSnapshot());
    }

    if (path === "/status") {
      return this.json(await this.statusSnapshot());
    }

    if (path === "/discovery") {
      const stored = (await this.ctx.storage.get<DiscoveryResult>("discovery")) ?? null;
      if (!stored) return this.json({ error: "no discovery run yet" }, 404);
      return this.json({
        tokens: Object.keys(stored.pairs).length,
        per_exchange: {
          extended: stored.symbolsByExchange.extended.length,
          grvt: stored.symbolsByExchange.grvt.length,
          nado: stored.symbolsByExchange.nado.length,
          variational: stored.symbolsByExchange.variational.length,
        },
        meta_summary: {
          extended_markets_with_lev: Object.keys(stored.meta.maxLeverage.extended ?? {}).length,
          grvt_markets_with_lev: Object.keys(stored.meta.maxLeverage.grvt ?? {}).length,
          nado_markets_with_lev: Object.keys(stored.meta.maxLeverage.nado ?? {}).length,
        },
      });
    }

    if (path === "/discovery/run") {
      // Trigger a fresh discovery + symbol propagation. POST normally, but
      // allow GET for easy manual triggering during development.
      const res = await this.runDiscoveryAndPropagate();
      return this.json(res);
    }

    const bookMatch = path.match(/^\/book\/([a-z]+)\/([A-Za-z0-9._-]+)$/);
    if (bookMatch) {
      const exchange = bookMatch[1].toLowerCase();
      // Keep symbol case as-is: GRVT uses "BTC_USDT_Perp", Variational uses
      // "P-BTC-USDC-3600", Extended uses "BTC-USD" — each exchange's native
      // form is stored verbatim in the DO's books map.
      const symbol = bookMatch[2];
      const book = await this.fetchBookFromExchange(exchange, symbol);
      if (!book) {
        return this.json({ error: `No feed for ${exchange}:${symbol}` }, 404);
      }
      return this.json(book);
    }

    return this.json({ error: "not found", path }, 404);
  }

  // ── RPC: Exchange DOs push book updates here ─────────────────────

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
        try { ws.send(payload); } catch { /* closed mid-write */ }
      }
    }
  }

  // ── Discovery: cron entry point (invoked by worker scheduled handler) ─

  async runDiscoveryAndPropagate(): Promise<{ ok: true; tokens: number; per_exchange: Record<string, number> }> {
    const result = await discoverPairs();
    await this.ctx.storage.put("discovery", result);

    // Propagate symbol lists to each ExchangeOms so it starts/refreshes
    // subscriptions. Extended does not need this (shared stream pulls all).
    const tasks: Array<Promise<unknown>> = [];

    if (result.symbolsByExchange.grvt.length > 0) {
      const grvt = this.env.GRVT_OMS.get(this.env.GRVT_OMS.idFromName("singleton"));
      tasks.push(grvt.ensureTracking(result.symbolsByExchange.grvt));
    }
    if (result.symbolsByExchange.nado.length > 0) {
      const nado = this.env.NADO_OMS.get(this.env.NADO_OMS.idFromName("singleton"));
      tasks.push(nado.ensureTracking(result.symbolsByExchange.nado));
    }
    if (result.symbolsByExchange.variational.length > 0) {
      const v = this.env.VARIATIONAL_OMS.get(
        this.env.VARIATIONAL_OMS.idFromName("singleton"),
      );
      tasks.push(v.ensureTracking(result.symbolsByExchange.variational));
    }
    await Promise.allSettled(tasks);

    return {
      ok: true,
      tokens: Object.keys(result.pairs).length,
      per_exchange: {
        extended: result.symbolsByExchange.extended.length,
        grvt: result.symbolsByExchange.grvt.length,
        nado: result.symbolsByExchange.nado.length,
        variational: result.symbolsByExchange.variational.length,
      },
    };
  }

  // ── WebSocket lifecycle (hibernation-compatible) ────────────────

  private handleWsUpgrade(req: Request): Response {
    const upgrade = req.headers.get("Upgrade");
    if (!upgrade || upgrade.toLowerCase() !== "websocket") {
      return new Response("Expected WebSocket upgrade", { status: 426 });
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair) as [WebSocket, WebSocket];
    this.ctx.acceptWebSocket(server);
    const att: WsAttachment = { subs: [], connected_at: Date.now() };
    server.serializeAttachment(att);
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
    const raw = typeof message === "string"
      ? message
      : new TextDecoder().decode(message);

    let msg: ClientMessage;
    try { msg = JSON.parse(raw) as ClientMessage; }
    catch {
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
        // Symbol is stored verbatim — each exchange has its own casing
        // convention (GRVT: BTC_USDT_Perp; Variational: P-BTC-USDC-3600).
        const symbol = msg.symbol;
        if (!exchange || !symbol) {
          this.sendJson(ws, { error: "subscribe requires exchange+symbol" });
          return;
        }
        if (!SUPPORTED_EXCHANGES.includes(exchange as SupportedExchange)) {
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

        // Send an immediate snapshot.
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
        const symbol = msg.symbol;
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
        this.sendJson(ws, {
          error: `unknown action: ${(msg as { action: string }).action}`,
        });
    }
  }

  async webSocketClose(
    _ws: WebSocket,
    _code: number,
    _reason: string,
    _wasClean: boolean,
  ): Promise<void> {
    /* runtime auto-replies to close frames in compat date >= 2026-04-07 */
  }

  // ── Snapshot helpers ─────────────────────────────────────────────

  private async fetchBookFromExchange(
    exchange: string,
    symbol: string,
  ): Promise<BookSnapshot | null> {
    let res: BookSnapshot | null = null;
    if (exchange === "extended") {
      const s = this.env.EXTENDED_OMS.get(
        this.env.EXTENDED_OMS.idFromName("singleton"),
      );
      res = (await s.getBook(symbol)) as BookSnapshot | null;
    } else if (exchange === "grvt") {
      const s = this.env.GRVT_OMS.get(this.env.GRVT_OMS.idFromName("singleton"));
      res = (await s.getBook(symbol)) as BookSnapshot | null;
    } else if (exchange === "nado") {
      const s = this.env.NADO_OMS.get(this.env.NADO_OMS.idFromName("singleton"));
      res = (await s.getBook(symbol)) as BookSnapshot | null;
    } else if (exchange === "variational") {
      const s = this.env.VARIATIONAL_OMS.get(
        this.env.VARIATIONAL_OMS.idFromName("singleton"),
      );
      res = (await s.getBook(symbol)) as BookSnapshot | null;
    }
    return res;
  }

  private async trackedSnapshot(): Promise<DiscoveredPairs> {
    const stored = await this.ctx.storage.get<DiscoveryResult>("discovery");
    return stored?.pairs ?? {};
  }

  private async statusSnapshot(): Promise<Record<string, unknown>> {
    const now = Date.now();
    const stored = await this.ctx.storage.get<DiscoveryResult>("discovery");
    if (!stored) return {};

    const out: Record<string, unknown> = {};
    const perExchange: Array<[SupportedExchange, string[]]> = [
      ["extended", stored.symbolsByExchange.extended],
      ["grvt", stored.symbolsByExchange.grvt],
      ["variational", stored.symbolsByExchange.variational],
      ["nado", stored.symbolsByExchange.nado.map((x) => x.symbol)],
    ];

    // Limit to 200 symbols total to avoid huge responses
    const MAX = 400;
    let count = 0;
    const allPairs: Array<[SupportedExchange, string]> = [];
    for (const [exch, syms] of perExchange) {
      for (const sym of syms) {
        if (count >= MAX) break;
        allPairs.push([exch, sym]);
        count += 1;
      }
    }

    // Fetch in parallel
    const results = await Promise.all(
      allPairs.map(([exch, sym]) => this.fetchBookFromExchange(exch, sym)),
    );
    for (let i = 0; i < allPairs.length; i++) {
      const snap = results[i];
      const [exch, sym] = allPairs[i]!;
      const key = `${exch}:${sym}`;
      if (!snap) {
        out[key] = { connected: false, has_data: false };
        continue;
      }
      out[key] = {
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
    } catch { /* ignore */ }
    return null;
  }

  private sendJson(ws: WebSocket, data: unknown): void {
    try { ws.send(JSON.stringify(data)); } catch { /* ignore */ }
  }

  private json(data: unknown, status = 200): Response {
    return new Response(JSON.stringify(data, null, 2), {
      status,
      headers: { "content-type": "application/json" },
    });
  }
}
