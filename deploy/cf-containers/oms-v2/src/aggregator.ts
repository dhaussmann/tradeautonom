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
 *   Server → Client: {type:"subscribed", exchange, symbol, meta?}    Phase E: meta on ACK
 *   Server → Client: {type:"book", exchange, symbol, bids, asks,      Phase E: enriched with
 *                     timestamp_ms, mid_price, bid_qty_cumsum, ...}    mid + cumsum
 *
 * Phase E (bot-entry enrichment) additional WS actions:
 *   quote         — subscribe to live single-leg quotes for a (exch, sym, side, qty/notional)
 *   unquote       — matching unsubscribe
 *   quote_cross   — subscribe to live dual-leg cross-exchange quote
 *   unquote_cross — matching unsubscribe
 * Push messages: {type:"quote", ...} / {type:"quote_cross", ...}
 *
 * Phase E REST endpoints:
 *   GET /meta                                — all symbols' static meta
 *   GET /meta/:exchange                      — per-exchange static meta
 *   GET /meta/:exchange/:symbol              — one symbol's static meta
 *   GET /quote/:exchange/:symbol?side=&...   — one-leg VWAP/depth/limit-price
 *   GET /quote/cross?token=&buy=&sell=&...   — dual-leg arb pre-trade quote
 *
 * Hibernation rationale: AggregatorDO has no outbound WebSocket (exchange WS
 * lives in ExchangeOms DOs). It can hibernate while bots stay connected.
 * onBookUpdate RPCs wake it up when fan-out is needed.
 */

import { DurableObject } from "cloudflare:workers";
import { discoverPairs, DiscoveryResult } from "./lib/discovery";
import { TAKER_FEE_PCT } from "./lib/arb";
import { computeBookStats } from "./lib/book-stats";
import { computeQuote, computeCrossQuote } from "./lib/quote";
import type {
  Env,
  BookSnapshot,
  ClientMessage,
  WsAttachment,
  QuoteSub,
  CrossQuoteSub,
  DiscoveredPairs,
  MarketMeta,
  SymbolMeta,
  Quote,
  CrossQuote,
} from "./types";

const MAX_SUBS_PER_WS = 200;
const MAX_QUOTE_SUBS_PER_WS = 50;
/** Coalesce quote pushes to at most once per this many ms. */
const QUOTE_PUSH_MIN_INTERVAL_MS = 100;

const SUPPORTED_EXCHANGES = ["extended", "grvt", "nado", "variational"] as const;
type SupportedExchange = (typeof SUPPORTED_EXCHANGES)[number];

/**
 * Re-shape the per-exchange meta arrays in DiscoveryResult into the
 * `exchange → token → MarketMeta` shape used by ArbScannerDO.
 */
function buildScannerMeta(
  result: DiscoveryResult,
): Record<string, Record<string, MarketMeta>> {
  const out: Record<string, Record<string, MarketMeta>> = {};
  const { maxLeverage, minOrderSize, qtyStep, minNotionalUsd } = result.meta;
  const exchanges = new Set<string>([
    ...Object.keys(maxLeverage),
    ...Object.keys(minOrderSize),
    ...Object.keys(qtyStep),
    ...Object.keys(minNotionalUsd ?? {}),
  ]);
  for (const exch of exchanges) {
    const tokens = new Set<string>([
      ...Object.keys(maxLeverage[exch] ?? {}),
      ...Object.keys(minOrderSize[exch] ?? {}),
      ...Object.keys(qtyStep[exch] ?? {}),
      ...Object.keys(minNotionalUsd?.[exch] ?? {}),
    ]);
    const perExch: Record<string, MarketMeta> = {};
    for (const tok of tokens) {
      perExch[tok] = {
        maxLeverage: maxLeverage[exch]?.[tok] ?? 1,
        minOrderSize: minOrderSize[exch]?.[tok] ?? 0,
        qtyStep: qtyStep[exch]?.[tok] ?? 0,
        minNotionalUsd: minNotionalUsd?.[exch]?.[tok] ?? 0,
      };
    }
    out[exch] = perExch;
  }
  return out;
}

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

    if (path === "/tracked") return this.json(await this.trackedSnapshot());
    if (path === "/status") return this.json(await this.statusSnapshot());

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
      const res = await this.runDiscoveryAndPropagate();
      return this.json(res);
    }

    // Phase E: /meta
    if (path === "/meta") {
      return this.handleMetaAll();
    }
    const metaExchMatch = path.match(/^\/meta\/([a-z]+)$/);
    if (metaExchMatch) {
      return this.handleMetaPerExchange(metaExchMatch[1]!);
    }
    const metaSymMatch = path.match(/^\/meta\/([a-z]+)\/([A-Za-z0-9._-]+)$/);
    if (metaSymMatch) {
      return this.handleMetaPerSymbol(metaSymMatch[1]!, metaSymMatch[2]!);
    }

    // Phase E: /quote
    if (path === "/quote/cross") {
      return this.handleQuoteCrossRest(url);
    }
    const quoteMatch = path.match(/^\/quote\/([a-z]+)\/([A-Za-z0-9._-]+)$/);
    if (quoteMatch) {
      return this.handleQuoteRest(quoteMatch[1]!, quoteMatch[2]!, url);
    }

    const bookMatch = path.match(/^\/book\/([a-z]+)\/([A-Za-z0-9._-]+)$/);
    if (bookMatch) {
      const exchange = bookMatch[1].toLowerCase();
      const symbol = bookMatch[2]!;
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
    // Phase E: precompute cheap stats once and attach to every fan-out.
    const stats = computeBookStats(snap);
    const bookPayload = JSON.stringify({
      type: "book",
      exchange: snap.exchange,
      symbol: snap.symbol,
      bids: snap.bids,
      asks: snap.asks,
      timestamp_ms: snap.timestamp_ms,
      ...stats,
    });

    // Fetch discovery once per onBookUpdate call (storage is in-memory after
    // first hit; cheap) — needed for quote_cross symbol resolution.
    const discovery = await this.ctx.storage.get<DiscoveryResult>("discovery");

    for (const ws of this.ctx.getWebSockets()) {
      const att = this.readAttachment(ws);
      if (!att) continue;

      // Standard book fan-out.
      if (att.subs.includes(key)) {
        try { ws.send(bookPayload); } catch { /* closed */ }
      }

      // Phase E: quote fan-out.
      if (att.quoteSubs.length > 0 || att.crossQuoteSubs.length > 0) {
        await this.fanOutQuotes(ws, att, snap, discovery);
      }
    }
  }

  // ── Discovery: cron entry point (invoked by worker scheduled handler) ─

  async runDiscoveryAndPropagate(): Promise<{ ok: true; tokens: number; per_exchange: Record<string, number> }> {
    const result = await discoverPairs();
    await this.ctx.storage.put("discovery", result);

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

    const scanner = this.env.ARB_SCANNER.get(
      this.env.ARB_SCANNER.idFromName("singleton"),
    );
    const scannerMeta = buildScannerMeta(result);
    tasks.push(scanner.updateDiscovery(result.pairs, scannerMeta));

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

  /**
   * Bootstrap RPC: ArbScannerDO calls this on cold start.
   */
  async getDiscoveryForScanner(): Promise<
    | { pairs: DiscoveredPairs; meta: Record<string, Record<string, MarketMeta>> }
    | null
  > {
    const stored = await this.ctx.storage.get<DiscoveryResult>("discovery");
    if (!stored) return null;
    return {
      pairs: stored.pairs,
      meta: buildScannerMeta(stored),
    };
  }

  // ── Phase E: /meta REST ──────────────────────────────────────────

  private async handleMetaAll(): Promise<Response> {
    const stored = await this.ctx.storage.get<DiscoveryResult>("discovery");
    if (!stored) return this.json({ error: "discovery not bootstrapped yet" }, 503);

    const out: SymbolMeta[] = [];
    for (const exch of SUPPORTED_EXCHANGES) {
      out.push(...this.collectMetaForExchange(stored, exch));
    }
    return this.json(out);
  }

  private async handleMetaPerExchange(exch: string): Promise<Response> {
    const stored = await this.ctx.storage.get<DiscoveryResult>("discovery");
    if (!stored) return this.json({ error: "discovery not bootstrapped yet" }, 503);
    if (!SUPPORTED_EXCHANGES.includes(exch as SupportedExchange)) {
      return this.json({ error: `unsupported exchange: ${exch}` }, 400);
    }
    return this.json(this.collectMetaForExchange(stored, exch as SupportedExchange));
  }

  private async handleMetaPerSymbol(exch: string, symbol: string): Promise<Response> {
    const stored = await this.ctx.storage.get<DiscoveryResult>("discovery");
    if (!stored) return this.json({ error: "discovery not bootstrapped yet" }, 503);
    const meta = this.resolveSymbolMeta(stored, exch, symbol);
    if (!meta) {
      return this.json({ error: `no meta for ${exch}:${symbol}` }, 404);
    }
    return this.json(meta);
  }

  private collectMetaForExchange(
    stored: DiscoveryResult,
    exch: SupportedExchange,
  ): SymbolMeta[] {
    const out: SymbolMeta[] = [];
    // Map symbol → base token for this exchange.
    for (const [token, exchMap] of Object.entries(stored.pairs)) {
      const sym = exchMap[exch];
      if (!sym) continue;
      const meta = this.buildSymbolMeta(stored, exch, sym, token);
      if (meta) out.push(meta);
    }
    return out;
  }

  private resolveSymbolMeta(
    stored: DiscoveryResult,
    exch: string,
    symbol: string,
  ): SymbolMeta | null {
    if (!SUPPORTED_EXCHANGES.includes(exch as SupportedExchange)) return null;
    // Find base token for this (exch, sym) via discovery.
    let token: string | null = null;
    for (const [tok, exchMap] of Object.entries(stored.pairs)) {
      if (exchMap[exch] === symbol) {
        token = tok;
        break;
      }
    }
    if (!token) return null;
    return this.buildSymbolMeta(stored, exch as SupportedExchange, symbol, token);
  }

  private buildSymbolMeta(
    stored: DiscoveryResult,
    exch: SupportedExchange,
    symbol: string,
    token: string,
  ): SymbolMeta | null {
    const lev = stored.meta.maxLeverage[exch]?.[token];
    const mn = stored.meta.minOrderSize[exch]?.[token];
    const st = stored.meta.qtyStep[exch]?.[token];
    const tk = stored.meta.tickSize[exch]?.[token];
    const nom = stored.meta.minNotionalUsd?.[exch]?.[token];
    return {
      exchange: exch,
      symbol,
      base_token: token,
      tick_size: typeof tk === "number" ? tk : 0,
      min_order_size: typeof mn === "number" ? mn : 0,
      min_notional_usd: typeof nom === "number" && nom > 0 ? nom : null,
      qty_step: typeof st === "number" ? st : 0,
      max_leverage: typeof lev === "number" ? lev : 1,
      taker_fee_pct: TAKER_FEE_PCT[exch] ?? 0.04,
      maker_fee_pct: null,
      funding_interval_s: exch === "variational"
        ? this.inferVariationalFundingInterval(symbol)
        : exch === "grvt" || exch === "extended" || exch === "nado"
          ? 3600
          : null,
    };
  }

  private inferVariationalFundingInterval(symbol: string): number | null {
    // P-BTC-USDC-3600 / P-BTC-USDC-28800 etc.
    const m = symbol.match(/^P-[A-Z0-9]+-[A-Z0-9]+-(\d+)$/);
    if (m) return Number(m[1]);
    return null;
  }

  // ── Phase E: /quote REST ─────────────────────────────────────────

  private async handleQuoteRest(
    exch: string,
    symbol: string,
    url: URL,
  ): Promise<Response> {
    const stored = await this.ctx.storage.get<DiscoveryResult>("discovery");
    if (!stored) return this.json({ error: "discovery not bootstrapped yet" }, 503);

    if (!SUPPORTED_EXCHANGES.includes(exch as SupportedExchange)) {
      return this.json({ error: `unsupported exchange: ${exch}` }, 400);
    }
    const side = (url.searchParams.get("side") ?? "").toLowerCase();
    if (side !== "buy" && side !== "sell") {
      return this.json({ error: "side must be buy|sell" }, 400);
    }

    const qtyParam = url.searchParams.get("qty");
    const notionalParam = url.searchParams.get("notional_usd");
    const bufferTicksParam = url.searchParams.get("buffer_ticks");
    const qty = qtyParam !== null ? Number(qtyParam) : undefined;
    const notional = notionalParam !== null ? Number(notionalParam) : undefined;
    const bufferTicks = bufferTicksParam !== null
      ? Math.max(0, Math.floor(Number(bufferTicksParam)))
      : undefined;
    if (qty === undefined && notional === undefined) {
      return this.json({ error: "one of qty or notional_usd is required" }, 400);
    }
    if ((qty !== undefined && !Number.isFinite(qty)) ||
        (notional !== undefined && !Number.isFinite(notional))) {
      return this.json({ error: "qty / notional_usd must be finite numbers" }, 400);
    }

    const meta = this.resolveSymbolMeta(stored, exch, symbol);
    if (!meta) {
      return this.json({ error: `no meta for ${exch}:${symbol}` }, 404);
    }
    const book = await this.fetchBookFromExchange(exch, symbol);
    const quote = computeQuote({
      exchange: exch,
      symbol,
      side,
      book,
      qty,
      notionalUsd: notional,
      meta: symbolMetaToMarketMeta(meta),
      takerFeePct: meta.taker_fee_pct,
      tickSize: meta.tick_size,
      bufferTicks,
    });
    return this.json(quote);
  }

  private async handleQuoteCrossRest(url: URL): Promise<Response> {
    const stored = await this.ctx.storage.get<DiscoveryResult>("discovery");
    if (!stored) return this.json({ error: "discovery not bootstrapped yet" }, 503);

    const token = (url.searchParams.get("token") ?? "").toUpperCase();
    const buyExch = (url.searchParams.get("buy_exchange") ?? "").toLowerCase();
    const sellExch = (url.searchParams.get("sell_exchange") ?? "").toLowerCase();
    if (!token || !buyExch || !sellExch) {
      return this.json(
        { error: "token, buy_exchange, sell_exchange required" },
        400,
      );
    }
    const qtyParam = url.searchParams.get("qty");
    const notionalParam = url.searchParams.get("notional_usd");
    const bufferTicksParam = url.searchParams.get("buffer_ticks");
    const qty = qtyParam !== null ? Number(qtyParam) : undefined;
    const notional = notionalParam !== null ? Number(notionalParam) : undefined;
    const bufferTicks = bufferTicksParam !== null
      ? Math.max(0, Math.floor(Number(bufferTicksParam)))
      : undefined;
    if (qty === undefined && notional === undefined) {
      return this.json({ error: "one of qty or notional_usd is required" }, 400);
    }

    const cross = await this.buildCrossQuote(
      stored,
      token,
      buyExch,
      sellExch,
      qty,
      notional,
      bufferTicks,
    );
    if ("error" in cross) {
      return this.json({ error: cross.error }, 400);
    }
    return this.json(cross);
  }

  private async buildCrossQuote(
    stored: DiscoveryResult,
    token: string,
    buyExch: string,
    sellExch: string,
    qty: number | undefined,
    notional: number | undefined,
    bufferTicks: number | undefined,
  ): Promise<CrossQuote | { error: string }> {
    const exchMap = stored.pairs[token];
    if (!exchMap) return { error: `token ${token} not in discovery` };
    const buySymbol = exchMap[buyExch];
    const sellSymbol = exchMap[sellExch];
    if (!buySymbol) return { error: `token ${token} not on ${buyExch}` };
    if (!sellSymbol) return { error: `token ${token} not on ${sellExch}` };

    const buyMeta = this.resolveSymbolMeta(stored, buyExch, buySymbol);
    const sellMeta = this.resolveSymbolMeta(stored, sellExch, sellSymbol);
    if (!buyMeta || !sellMeta) {
      return { error: "missing meta for at least one leg" };
    }

    const [buyBook, sellBook] = await Promise.all([
      this.fetchBookFromExchange(buyExch, buySymbol),
      this.fetchBookFromExchange(sellExch, sellSymbol),
    ]);

    return computeCrossQuote({
      token,
      buy: {
        exchange: buyExch,
        symbol: buySymbol,
        book: buyBook,
        meta: symbolMetaToMarketMeta(buyMeta),
        takerFeePct: buyMeta.taker_fee_pct,
        tickSize: buyMeta.tick_size,
        bufferTicks,
      },
      sell: {
        exchange: sellExch,
        symbol: sellSymbol,
        book: sellBook,
        meta: symbolMetaToMarketMeta(sellMeta),
        takerFeePct: sellMeta.taker_fee_pct,
        tickSize: sellMeta.tick_size,
        bufferTicks,
      },
      qty,
      notionalUsd: notional,
    });
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
    const att: WsAttachment = {
      subs: [],
      quoteSubs: [],
      crossQuoteSubs: [],
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
    try { msg = JSON.parse(raw) as ClientMessage; }
    catch {
      this.sendJson(ws, { error: "invalid JSON" });
      return;
    }

    if (!msg || typeof msg !== "object" || !("action" in msg)) {
      this.sendJson(ws, { error: "missing action" });
      return;
    }

    const att = this.readAttachment(ws) ?? {
      subs: [], quoteSubs: [], crossQuoteSubs: [], connected_at: Date.now(),
    };

    switch (msg.action) {
      case "subscribe":
        await this.onSubscribe(ws, att, msg);
        return;
      case "unsubscribe":
        this.onUnsubscribe(ws, att, msg);
        return;
      case "quote":
        await this.onQuoteSubscribe(ws, att, msg);
        return;
      case "unquote":
        this.onQuoteUnsubscribe(ws, att, msg, /*cross=*/false);
        return;
      case "quote_cross":
        await this.onCrossQuoteSubscribe(ws, att, msg);
        return;
      case "unquote_cross":
        this.onQuoteUnsubscribe(ws, att, msg, /*cross=*/true);
        return;
      default:
        this.sendJson(ws, {
          error: `unknown action: ${(msg as { action: string }).action}`,
        });
    }
  }

  async webSocketClose(
    _ws: WebSocket, _code: number, _reason: string, _wasClean: boolean,
  ): Promise<void> { /* runtime auto-replies */ }

  // ── WS action handlers ──────────────────────────────────────────

  private async onSubscribe(
    ws: WebSocket,
    att: WsAttachment,
    msg: Extract<ClientMessage, { action: "subscribe" }>,
  ): Promise<void> {
    const exchange = msg.exchange?.toLowerCase();
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

    // Phase E: include static meta on the ACK so bots never need /meta once
    // subscribed. Only present if discovery has populated it.
    const stored = await this.ctx.storage.get<DiscoveryResult>("discovery");
    const meta = stored ? this.resolveSymbolMeta(stored, exchange, symbol) : null;
    this.sendJson(ws, { type: "subscribed", exchange, symbol, meta });

    const snap = await this.fetchBookFromExchange(exchange, symbol);
    if (snap && (snap.bids.length > 0 || snap.asks.length > 0)) {
      const stats = computeBookStats(snap);
      this.sendJson(ws, {
        type: "book",
        exchange: snap.exchange,
        symbol: snap.symbol,
        bids: snap.bids,
        asks: snap.asks,
        timestamp_ms: snap.timestamp_ms,
        ...stats,
      });
    }
  }

  private onUnsubscribe(
    ws: WebSocket,
    att: WsAttachment,
    msg: Extract<ClientMessage, { action: "unsubscribe" }>,
  ): void {
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
  }

  private async onQuoteSubscribe(
    ws: WebSocket,
    att: WsAttachment,
    msg: Extract<ClientMessage, { action: "quote" }>,
  ): Promise<void> {
    const exchange = msg.exchange?.toLowerCase();
    const symbol = msg.symbol;
    const side = msg.side;
    const qty = typeof msg.qty === "number" ? msg.qty : null;
    const notional = typeof msg.notional_usd === "number" ? msg.notional_usd : null;
    const bufferTicks = typeof msg.buffer_ticks === "number"
      ? Math.max(0, Math.floor(msg.buffer_ticks))
      : 2;
    if (!exchange || !symbol || (side !== "buy" && side !== "sell") ||
        (qty === null && notional === null)) {
      this.sendJson(ws, {
        error: "quote requires exchange, symbol, side, qty|notional_usd",
      });
      return;
    }
    if (!SUPPORTED_EXCHANGES.includes(exchange as SupportedExchange)) {
      this.sendJson(ws, { error: `unsupported exchange: ${exchange}` });
      return;
    }

    if (att.quoteSubs.length + att.crossQuoteSubs.length >= MAX_QUOTE_SUBS_PER_WS) {
      this.sendJson(ws, { error: `max ${MAX_QUOTE_SUBS_PER_WS} quote subs per ws` });
      return;
    }

    const sub: QuoteSub = {
      exchange, symbol, side,
      qty, notional_usd: notional, buffer_ticks: bufferTicks,
    };
    // De-duplicate: same key replaces previous.
    att.quoteSubs = att.quoteSubs.filter((q) => !quoteSubEq(q, sub));
    att.quoteSubs.push(sub);
    ws.serializeAttachment(att);

    // Immediate quote.
    const stored = await this.ctx.storage.get<DiscoveryResult>("discovery");
    if (stored) {
      const q = await this.computeQuoteForSub(stored, sub);
      if (q) {
        sub.last_sent_ms = Date.now();
        ws.serializeAttachment(att);
        this.sendJson(ws, { type: "quote", ...q });
      }
    }
  }

  private async onCrossQuoteSubscribe(
    ws: WebSocket,
    att: WsAttachment,
    msg: Extract<ClientMessage, { action: "quote_cross" }>,
  ): Promise<void> {
    const token = (msg.token ?? "").toUpperCase();
    const buyExch = msg.buy_exchange?.toLowerCase();
    const sellExch = msg.sell_exchange?.toLowerCase();
    const qty = typeof msg.qty === "number" ? msg.qty : null;
    const notional = typeof msg.notional_usd === "number" ? msg.notional_usd : null;
    const bufferTicks = typeof msg.buffer_ticks === "number"
      ? Math.max(0, Math.floor(msg.buffer_ticks))
      : 2;
    if (!token || !buyExch || !sellExch || (qty === null && notional === null)) {
      this.sendJson(ws, {
        error: "quote_cross requires token, buy_exchange, sell_exchange, qty|notional_usd",
      });
      return;
    }
    if (att.quoteSubs.length + att.crossQuoteSubs.length >= MAX_QUOTE_SUBS_PER_WS) {
      this.sendJson(ws, { error: `max ${MAX_QUOTE_SUBS_PER_WS} quote subs per ws` });
      return;
    }
    const sub: CrossQuoteSub = {
      token, buy_exchange: buyExch, sell_exchange: sellExch,
      qty, notional_usd: notional, buffer_ticks: bufferTicks,
    };
    att.crossQuoteSubs = att.crossQuoteSubs.filter(
      (c) => !crossQuoteSubEq(c, sub),
    );
    att.crossQuoteSubs.push(sub);
    ws.serializeAttachment(att);

    const stored = await this.ctx.storage.get<DiscoveryResult>("discovery");
    if (stored) {
      const cq = await this.buildCrossQuote(
        stored, token, buyExch, sellExch,
        sub.qty ?? undefined, sub.notional_usd ?? undefined, sub.buffer_ticks,
      );
      if (!("error" in cq)) {
        sub.last_sent_ms = Date.now();
        ws.serializeAttachment(att);
        this.sendJson(ws, { type: "quote_cross", ...cq });
      } else {
        this.sendJson(ws, { error: cq.error });
      }
    }
  }

  private onQuoteUnsubscribe(
    ws: WebSocket,
    att: WsAttachment,
    msg: Extract<ClientMessage, { action: "unquote" | "unquote_cross" }>,
    cross: boolean,
  ): void {
    // Treat as loose record for field access.
    const m = msg as unknown as Record<string, unknown>;
    if (cross) {
      const target: CrossQuoteSub = {
        token: String(m.token ?? "").toUpperCase(),
        buy_exchange: String(m.buy_exchange ?? "").toLowerCase(),
        sell_exchange: String(m.sell_exchange ?? "").toLowerCase(),
        qty: typeof m.qty === "number" ? m.qty : null,
        notional_usd: typeof m.notional_usd === "number" ? m.notional_usd : null,
        buffer_ticks: typeof m.buffer_ticks === "number"
          ? Math.max(0, Math.floor(m.buffer_ticks as number))
          : 2,
      };
      att.crossQuoteSubs = att.crossQuoteSubs.filter(
        (c) => !crossQuoteSubEq(c, target),
      );
    } else {
      const target: QuoteSub = {
        exchange: String(m.exchange ?? "").toLowerCase(),
        symbol: String(m.symbol ?? ""),
        side: String(m.side ?? "") as "buy" | "sell",
        qty: typeof m.qty === "number" ? m.qty : null,
        notional_usd: typeof m.notional_usd === "number" ? m.notional_usd : null,
        buffer_ticks: typeof m.buffer_ticks === "number"
          ? Math.max(0, Math.floor(m.buffer_ticks as number))
          : 2,
      };
      att.quoteSubs = att.quoteSubs.filter((q) => !quoteSubEq(q, target));
    }
    ws.serializeAttachment(att);
  }

  /**
   * On each relevant book update, push refreshed quotes to ws subscribers.
   * Coalesced to at most one push per QUOTE_PUSH_MIN_INTERVAL_MS per sub.
   */
  private async fanOutQuotes(
    ws: WebSocket,
    att: WsAttachment,
    snap: BookSnapshot,
    discovery: DiscoveryResult | null | undefined,
  ): Promise<void> {
    if (!discovery) return;
    const now = Date.now();
    const key = `${snap.exchange}:${snap.symbol}`;
    let dirty = false;

    for (const sub of att.quoteSubs) {
      if (`${sub.exchange}:${sub.symbol}` !== key) continue;
      if (sub.last_sent_ms && now - sub.last_sent_ms < QUOTE_PUSH_MIN_INTERVAL_MS) {
        continue;
      }
      const q = await this.computeQuoteForSub(discovery, sub);
      if (!q) continue;
      sub.last_sent_ms = now;
      dirty = true;
      try { ws.send(JSON.stringify({ type: "quote", ...q })); } catch { /* closed */ }
    }

    for (const sub of att.crossQuoteSubs) {
      const exchMap = discovery.pairs[sub.token];
      if (!exchMap) continue;
      const buySym = exchMap[sub.buy_exchange];
      const sellSym = exchMap[sub.sell_exchange];
      if (!buySym || !sellSym) continue;
      const buyKey = `${sub.buy_exchange}:${buySym}`;
      const sellKey = `${sub.sell_exchange}:${sellSym}`;
      if (key !== buyKey && key !== sellKey) continue;
      if (sub.last_sent_ms && now - sub.last_sent_ms < QUOTE_PUSH_MIN_INTERVAL_MS) {
        continue;
      }
      const cq = await this.buildCrossQuote(
        discovery, sub.token, sub.buy_exchange, sub.sell_exchange,
        sub.qty ?? undefined, sub.notional_usd ?? undefined, sub.buffer_ticks,
      );
      if ("error" in cq) continue;
      sub.last_sent_ms = now;
      dirty = true;
      try { ws.send(JSON.stringify({ type: "quote_cross", ...cq })); } catch { /* closed */ }
    }

    if (dirty) {
      try { ws.serializeAttachment(att); } catch { /* ignore */ }
    }
  }

  private async computeQuoteForSub(
    stored: DiscoveryResult,
    sub: QuoteSub,
  ): Promise<Quote | null> {
    const meta = this.resolveSymbolMeta(stored, sub.exchange, sub.symbol);
    if (!meta) return null;
    const book = await this.fetchBookFromExchange(sub.exchange, sub.symbol);
    return computeQuote({
      exchange: sub.exchange,
      symbol: sub.symbol,
      side: sub.side,
      book,
      qty: sub.qty ?? undefined,
      notionalUsd: sub.notional_usd ?? undefined,
      meta: symbolMetaToMarketMeta(meta),
      takerFeePct: meta.taker_fee_pct,
      tickSize: meta.tick_size,
      bufferTicks: sub.buffer_ticks,
    });
  }

  // ── Book + status helpers ────────────────────────────────────────

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
        // Backwards-compatible: older attachments may miss Phase E fields.
        const att = raw as WsAttachment;
        if (!Array.isArray(att.quoteSubs)) att.quoteSubs = [];
        if (!Array.isArray(att.crossQuoteSubs)) att.crossQuoteSubs = [];
        return att;
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

// ── Helpers ────────────────────────────────────────────────────────

function quoteSubEq(a: QuoteSub, b: QuoteSub): boolean {
  return a.exchange === b.exchange &&
    a.symbol === b.symbol &&
    a.side === b.side &&
    a.qty === b.qty &&
    a.notional_usd === b.notional_usd &&
    a.buffer_ticks === b.buffer_ticks;
}

function crossQuoteSubEq(a: CrossQuoteSub, b: CrossQuoteSub): boolean {
  return a.token === b.token &&
    a.buy_exchange === b.buy_exchange &&
    a.sell_exchange === b.sell_exchange &&
    a.qty === b.qty &&
    a.notional_usd === b.notional_usd &&
    a.buffer_ticks === b.buffer_ticks;
}

/**
 * Map the public `SymbolMeta` shape (kebab/snake_case JSON fields) back to
 * the internal `MarketMeta` shape consumed by computeQuote / computeCrossQuote.
 * `min_notional_usd: null` on the public side maps to 0 internally.
 */
function symbolMetaToMarketMeta(m: SymbolMeta): MarketMeta {
  return {
    maxLeverage: m.max_leverage,
    minOrderSize: m.min_order_size,
    qtyStep: m.qty_step,
    minNotionalUsd: m.min_notional_usd ?? 0,
  };
}
