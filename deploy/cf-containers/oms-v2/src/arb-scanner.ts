/**
 * ArbScannerDO — cross-exchange arbitrage scanner + DNA-bot endpoint.
 *
 * Role:
 *  - Receive onBookUpdate RPCs from each ExchangeOms (parallel to AggregatorDO)
 *  - Maintain an in-memory book mirror keyed by "exchange:symbol"
 *  - Event-driven: recompute arb opportunities for the affected token on every
 *    book update (instead of a fixed 200ms poll loop like V1 Photon)
 *  - Real-time per-position spread monitoring for DNA-bot watched arbs
 *  - Serve /ws/arb (V1-compatible) and /arb/* REST endpoints
 *
 * V1 source of truth: deploy/monitor/monitor_service.py
 *   - _find_arb_for_token     (line 502-593) → src/lib/arb.ts
 *   - _scan_arbitrage         (line 596-660) → event-driven equivalent below
 *   - /ws/arb endpoint        (line 824-927)
 *   - _notify_arb_watchers    (line 958-1003)
 *   - /arb/opportunities      (line 691-720)
 *
 * Hibernation: this DO has no outbound WS. The continuous push of book
 * updates from exchange DOs means it stays warm in practice, but it can
 * hibernate between traffic bursts with in-memory state reloaded as needed.
 */

import { DurableObject } from "cloudflare:workers";
import type {
  Env,
  ArbOpportunity,
  ArbStatusMessage,
  ArbWsAttachment,
  BookSnapshot,
  DiscoveredPairs,
  MarketMeta,
} from "./types";
import {
  ARB_EXCHANGES,
  ARB_EXCLUDED_TOKENS,
  ARB_FEE_BUFFER_BPS,
  ARB_MAX_NOTIONAL_USD,
  ARB_SCAN_INTERVAL_S,
  TAKER_FEE_PCT,
  findArbForToken,
  minProfitBps,
} from "./lib/arb";
import {
  tradeabilityKey,
  type TradeabilityMap,
} from "./lib/tradeability";

/** Max /ws/arb connections per client — defensive cap. */
const MAX_WATCH_PER_WS = 200;

interface DiscoveryPayload {
  pairs: DiscoveredPairs;
  /** exchange → token → meta */
  meta: Record<string, Record<string, MarketMeta>>;
}

export class ArbScannerDO extends DurableObject<Env> {
  /** In-memory book cache keyed by "exchange:symbol". */
  private books: Map<string, BookSnapshot> = new Map();

  /** Last discovery snapshot (pairs + meta). Bootstrapped from AggregatorDO. */
  private discovery: DiscoveryPayload = { pairs: {}, meta: {} };
  private discoveryBootstrapped = false;

  /**
   * Per-leg tradeability map: keyed `"exchange:symbol"`. Pushed by
   * AggregatorDO after each tradeability cron tick (every 15 min) via
   * updateTradeability(). Used to filter "listed but unfillable" legs
   * (one-sided book, crossed book, stale, etc.) out of arb opportunities.
   *
   * Empty on cold start → `isLegTradeable` fails open so we don't silently
   * block all opportunities while waiting for the first cron pass.
   * See lib/tradeability.ts for the evaluation rules.
   */
  private tradeability: TradeabilityMap = {};

  /** Reverse index: "exchange:symbol" → base token. */
  private symbolToToken: Map<string, string> = new Map();

  /**
   * Current opportunities per base token. Rebuilt event-driven.
   * Key = token. Value = opportunities sorted by net_profit_bps desc.
   */
  private opportunities: Map<string, ArbOpportunity[]> = new Map();

  private lastScanMs: number | null = null;
  private totalScans = 0;
  private totalPushedOpps = 0;
  private totalPushedStatus = 0;

  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    // Bootstrap discovery + tradeability from AggregatorDO once per cold
    // start. This populates books-to-token mapping and the leg-liveness
    // map before the first fan-out arrives, so post-hibernation cold starts
    // see consistent filtering instead of waiting up to 15 min for the next
    // cron tick.
    state.blockConcurrencyWhile(async () => {
      await this.bootstrapDiscovery().catch((e) => {
        console.warn(
          JSON.stringify({
            evt: "arb_bootstrap_failed",
            err: e instanceof Error ? e.message : String(e),
          }),
        );
      });
      await this.bootstrapTradeability().catch((e) => {
        console.warn(
          JSON.stringify({
            evt: "arb_tradeability_bootstrap_failed",
            err: e instanceof Error ? e.message : String(e),
          }),
        );
      });
    });
  }

  // ── HTTP / WS entrypoint ─────────────────────────────────────────

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;

    if (path === "/ws/arb") return this.handleWsUpgrade(req);

    if (path === "/arb/health") {
      let untradeableCount = 0;
      for (const v of Object.values(this.tradeability)) {
        if (!v.tradeable) untradeableCount += 1;
      }
      return this.json({
        status: "ok",
        tokens_tracked: Object.keys(this.discovery.pairs).length,
        books_cached: this.books.size,
        tokens_with_opportunities: this.opportunities.size,
        total_current_opportunities: this.totalCurrentOpps(),
        total_scans: this.totalScans,
        last_scan_ms: this.lastScanMs,
        connected_clients: this.ctx.getWebSockets().length,
        discovery_bootstrapped: this.discoveryBootstrapped,
        tradeability_entries: Object.keys(this.tradeability).length,
        tradeability_untradeable: untradeableCount,
        uptime_pushes: {
          opportunities: this.totalPushedOpps,
          status: this.totalPushedStatus,
        },
      });
    }

    if (path === "/arb/config") {
      const eligibleExchanges = Array.from(ARB_EXCHANGES).sort();
      const fees: Record<string, number> = {};
      for (const e of eligibleExchanges) fees[`${e}_${e}`] = 0;
      const pairwise: Record<string, number> = {};
      for (let i = 0; i < eligibleExchanges.length; i++) {
        for (let j = i + 1; j < eligibleExchanges.length; j++) {
          const a = eligibleExchanges[i]!;
          const b = eligibleExchanges[j]!;
          pairwise[`${a}_${b}`] = round1(minProfitBps(a, b));
        }
      }
      return this.json({
        scan_interval_s: ARB_SCAN_INTERVAL_S,
        max_notional_usd: ARB_MAX_NOTIONAL_USD,
        exchanges: eligibleExchanges,
        excluded_tokens: Array.from(ARB_EXCLUDED_TOKENS).sort(),
        taker_fees_pct: TAKER_FEE_PCT,
        fee_buffer_bps: ARB_FEE_BUFFER_BPS,
        min_profit_bps_by_pair: pairwise,
        note: "scan_interval_s is advisory; V2 is event-driven per book update",
      });
    }

    if (path === "/arb/opportunities") {
      return this.handleOpportunitiesRest(url);
    }

    return this.json({ error: "not found", path }, 404);
  }

  // ── RPCs ─────────────────────────────────────────────────────────

  /**
   * Called from every ExchangeOms.fanOut(). Same signature as
   * AggregatorDO.onBookUpdate. Updates local cache, triggers recompute
   * for the affected token, pushes deltas to subscribers, notifies
   * per-position watchers.
   */
  async onBookUpdate(snap: BookSnapshot): Promise<void> {
    const key = `${snap.exchange}:${snap.symbol}`;
    this.books.set(key, snap);

    if (!this.discoveryBootstrapped) {
      // No discovery yet — accept the update for cache but skip scan work.
      return;
    }

    const token = this.symbolToToken.get(key);
    if (!token) return;
    if (ARB_EXCLUDED_TOKENS.has(token)) return;
    if (!ARB_EXCHANGES.has(snap.exchange)) return;

    // Recompute opportunities for this token (event-driven).
    this.rescanToken(token);

    // Real-time watcher notifications for any position involving this book.
    this.notifyWatchers(snap);
  }

  /**
   * Called by AggregatorDO after each discovery cron run.
   * Replaces internal discovery + meta in one atomic update.
   */
  async updateDiscovery(
    pairs: DiscoveredPairs,
    meta: Record<string, Record<string, MarketMeta>>,
  ): Promise<{ ok: true; tokens: number }> {
    this.applyDiscovery({ pairs, meta });
    this.discoveryBootstrapped = true;
    // Invalidate all caches — new pairs might imply new opportunities.
    this.opportunities.clear();
    // Kick a scan over everything we already have books for.
    this.rescanAll();
    return { ok: true, tokens: Object.keys(pairs).length };
  }

  /**
   * Called by AggregatorDO after each tradeability cron tick. Replaces the
   * leg-liveness map and re-scans every token so any opportunity that
   * relies on a leg now flagged untradeable is removed in the same cycle.
   *
   * Fail-open semantics live in `isLegTradeable`: legs missing from the
   * map are treated as tradeable, so transient cron failures or cold-start
   * windows cannot block all opportunities.
   */
  async updateTradeability(
    map: TradeabilityMap,
  ): Promise<{ ok: true; entries: number; untradeable: number }> {
    this.tradeability = map ?? {};
    let untradeable = 0;
    for (const v of Object.values(this.tradeability)) {
      if (!v.tradeable) untradeable += 1;
    }
    // Force a re-scan only if discovery is already bootstrapped — otherwise
    // the books-to-token map is incomplete.
    if (this.discoveryBootstrapped) {
      this.opportunities.clear();
      this.rescanAll();
    }
    return {
      ok: true,
      entries: Object.keys(this.tradeability).length,
      untradeable,
    };
  }

  // ── Internals ────────────────────────────────────────────────────

  private async bootstrapDiscovery(): Promise<void> {
    const agg = this.env.AGGREGATOR_DO.get(
      this.env.AGGREGATOR_DO.idFromName("aggregator"),
    );
    const discovery = (await (agg as any).getDiscoveryForScanner?.()) as
      | DiscoveryPayload
      | null
      | undefined;
    if (discovery && discovery.pairs) {
      this.applyDiscovery(discovery);
      this.discoveryBootstrapped = true;
    }
  }

  private applyDiscovery(d: DiscoveryPayload): void {
    this.discovery = d;
    this.symbolToToken.clear();
    for (const [token, exchMap] of Object.entries(d.pairs)) {
      for (const [exch, sym] of Object.entries(exchMap)) {
        this.symbolToToken.set(`${exch}:${sym}`, token);
      }
    }
  }

  private async bootstrapTradeability(): Promise<void> {
    const agg = this.env.AGGREGATOR_DO.get(
      this.env.AGGREGATOR_DO.idFromName("aggregator"),
    );
    const map = (await (agg as any).getTradeabilityForScanner?.()) as
      | TradeabilityMap
      | null
      | undefined;
    if (map && typeof map === "object") {
      this.tradeability = map;
    }
  }

  /**
   * Fail-open per-leg tradeability check. Missing entries → assume
   * tradeable so a cold start or transient cron failure does not
   * silently filter out every opportunity. The `tradeable: false` decision
   * comes from lib/tradeability.ts (one-sided book, crossed book, stale,
   * disconnected, wide spread, or invalid price).
   */
  private isLegTradeable(exchange: string, symbol: string): boolean {
    const t = this.tradeability[tradeabilityKey(exchange, symbol)];
    if (!t) return true;
    return t.tradeable;
  }

  /** Reduce an exchange→symbol map to legs that are currently tradeable. */
  private filterTradeableLegs(
    exchangeMap: Record<string, string>,
  ): Record<string, string> {
    const out: Record<string, string> = {};
    for (const [exch, sym] of Object.entries(exchangeMap)) {
      if (this.isLegTradeable(exch, sym)) {
        out[exch] = sym;
      }
    }
    return out;
  }

  private rescanAll(): void {
    for (const token of Object.keys(this.discovery.pairs)) {
      if (ARB_EXCLUDED_TOKENS.has(token)) continue;
      this.rescanToken(token);
    }
  }

  /**
   * Recompute and broadcast opportunities for a single token.
   * Compares new opportunities against the previous set for this token and
   * pushes the full new set to filtered subscribers (matches V1 semantics:
   * V1 broadcasts *all* current opportunities per /ws/arb subscriber, we do
   * the same but only when the set actually changed).
   */
  private rescanToken(token: string): void {
    this.totalScans++;
    this.lastScanMs = Date.now();

    const exchangeMap = this.discovery.pairs[token];
    if (!exchangeMap) return;

    // Drop any leg that the most recent tradeability check flagged as
    // untradeable (one-sided book, crossed book, stale, etc.). lib/arb.ts
    // already filters legs whose live snapshot is empty/disconnected, but
    // it cannot detect crossed_book or wide-spread anomalies — that's
    // what this layer adds. Fail-open: legs without a tradeability entry
    // are kept (cold-start tolerance).
    const tradeableMap = this.filterTradeableLegs(exchangeMap);

    const newOpps = findArbForToken(token, tradeableMap, this.books, {
      meta: this.discovery.meta,
    });

    const prev = this.opportunities.get(token) ?? [];
    if (opportunitiesEqual(prev, newOpps)) {
      // Still update prev with new timestamps so stored values freshen,
      // but skip the broadcast churn.
      if (newOpps.length > 0) this.opportunities.set(token, newOpps);
      else this.opportunities.delete(token);
      return;
    }
    if (newOpps.length > 0) {
      newOpps.sort((a, b) => b.net_profit_bps - a.net_profit_bps);
      this.opportunities.set(token, newOpps);
    } else {
      this.opportunities.delete(token);
    }

    // Broadcast each new/changed opportunity to matching subscribers.
    this.broadcastOpportunities(newOpps);
  }

  private broadcastOpportunities(opps: ArbOpportunity[]): void {
    if (opps.length === 0) return;
    const clients = this.ctx.getWebSockets();
    for (const ws of clients) {
      const att = this.readAttachment(ws);
      if (!att || !att.oppFilter.subscribed) continue;
      for (const opp of opps) {
        if (!matchesFilter(opp, att)) continue;
        try {
          ws.send(JSON.stringify({ type: "arb_opportunity", ...opp }));
          this.totalPushedOpps++;
        } catch {
          /* closed */
        }
      }
    }
  }

  private notifyWatchers(updated: BookSnapshot): void {
    const clients = this.ctx.getWebSockets();
    if (clients.length === 0) return;
    const nowMs = Date.now();

    for (const ws of clients) {
      const att = this.readAttachment(ws);
      if (!att || att.watch.length === 0) continue;

      for (const watchKey of att.watch) {
        const parsed = parseWatchKey(watchKey);
        if (!parsed) continue;
        const { token, buyExch, sellExch } = parsed;

        const exchMap = this.discovery.pairs[token];
        if (!exchMap) continue;
        const buySym = exchMap[buyExch];
        const sellSym = exchMap[sellExch];
        if (!buySym || !sellSym) continue;

        // Is this book relevant to this watch?
        const updatedKey = `${updated.exchange}:${updated.symbol}`;
        const buyKey = `${buyExch}:${buySym}`;
        const sellKey = `${sellExch}:${sellSym}`;
        if (updatedKey !== buyKey && updatedKey !== sellKey) continue;

        const buySnap = this.books.get(buyKey);
        const sellSnap = this.books.get(sellKey);
        if (!buySnap || !sellSnap) continue;
        if (buySnap.asks.length === 0 || sellSnap.bids.length === 0) continue;

        const buyAsk = buySnap.asks[0]![0];
        const sellBid = sellSnap.bids[0]![0];
        if (buyAsk <= 0) continue;

        const spreadBps = ((sellBid - buyAsk) / buyAsk) * 10000;
        const threshold = minProfitBps(buyExch, sellExch);
        const profitable = spreadBps >= threshold;

        const msg: ArbStatusMessage = {
          type: profitable ? "arb_status" : "arb_close",
          token,
          buy_exchange: buyExch,
          sell_exchange: sellExch,
          buy_ask: buyAsk,
          sell_bid: sellBid,
          spread_bps: round2(spreadBps),
          fee_threshold_bps: round1(threshold),
          profitable,
          timestamp_ms: nowMs,
        };
        if (!profitable) msg.reason = "spread_below_fees";

        try {
          ws.send(JSON.stringify(msg));
          this.totalPushedStatus++;
        } catch {
          /* closed */
        }
      }
    }
  }

  // ── WebSocket lifecycle (hibernation) ────────────────────────────

  private handleWsUpgrade(req: Request): Response {
    const upgrade = req.headers.get("Upgrade");
    if (!upgrade || upgrade.toLowerCase() !== "websocket") {
      return new Response("Expected WebSocket upgrade", { status: 426 });
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair) as [WebSocket, WebSocket];
    this.ctx.acceptWebSocket(server);
    const att: ArbWsAttachment = {
      watch: [],
      oppFilter: { subscribed: false, min_profit_bps: null, exchanges: null },
      connected_at: Date.now(),
    };
    server.serializeAttachment(att);
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(
    ws: WebSocket,
    message: string | ArrayBuffer,
  ): Promise<void> {
    const raw =
      typeof message === "string"
        ? message
        : new TextDecoder().decode(message);

    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      this.sendJson(ws, { error: "invalid JSON" });
      return;
    }

    const action = String(msg.action ?? "");
    const att = this.readAttachment(ws) ?? {
      watch: [],
      oppFilter: { subscribed: false, min_profit_bps: null, exchanges: null },
      connected_at: Date.now(),
    };

    if (action === "watch") {
      const token = String(msg.token ?? "");
      const buyExch = String(msg.buy_exchange ?? "");
      const sellExch = String(msg.sell_exchange ?? "");
      if (!token || !buyExch || !sellExch) {
        this.sendJson(ws, {
          error: "watch requires token, buy_exchange, sell_exchange",
        });
        return;
      }
      const key = watchKey(token, buyExch, sellExch);
      if (!att.watch.includes(key)) {
        if (att.watch.length >= MAX_WATCH_PER_WS) {
          this.sendJson(ws, { error: `max ${MAX_WATCH_PER_WS} watches` });
          return;
        }
        att.watch.push(key);
        ws.serializeAttachment(att);
      }

      // Immediate snapshot / status.
      const exchMap = this.discovery.pairs[token];
      const buySym = exchMap?.[buyExch];
      const sellSym = exchMap?.[sellExch];
      if (buySym && sellSym) {
        const buySnap = this.books.get(`${buyExch}:${buySym}`);
        const sellSnap = this.books.get(`${sellExch}:${sellSym}`);
        if (
          buySnap && sellSnap &&
          buySnap.asks.length > 0 && sellSnap.bids.length > 0
        ) {
          const buyAsk = buySnap.asks[0]![0];
          const sellBid = sellSnap.bids[0]![0];
          const spreadBps = buyAsk > 0
            ? ((sellBid - buyAsk) / buyAsk) * 10000
            : 0.0;
          const threshold = minProfitBps(buyExch, sellExch);
          this.sendJson(ws, {
            type: "arb_status",
            token,
            buy_exchange: buyExch,
            sell_exchange: sellExch,
            buy_ask: buyAsk,
            sell_bid: sellBid,
            spread_bps: round2(spreadBps),
            fee_threshold_bps: round1(threshold),
            profitable: spreadBps >= threshold,
            timestamp_ms: Date.now(),
          });
          return;
        }
      }
      // No data yet.
      this.sendJson(ws, {
        type: "watching",
        token,
        buy_exchange: buyExch,
        sell_exchange: sellExch,
        has_data: false,
      });
      return;
    }

    if (action === "unwatch") {
      const token = String(msg.token ?? "");
      const buyExch = String(msg.buy_exchange ?? "");
      const sellExch = String(msg.sell_exchange ?? "");
      if (!token || !buyExch || !sellExch) {
        this.sendJson(ws, {
          error: "unwatch requires token, buy_exchange, sell_exchange",
        });
        return;
      }
      const key = watchKey(token, buyExch, sellExch);
      att.watch = att.watch.filter((k) => k !== key);
      ws.serializeAttachment(att);
      return;
    }

    if (action === "subscribe_opportunities") {
      att.oppFilter.subscribed = true;
      att.oppFilter.min_profit_bps =
        typeof msg.min_profit_bps === "number" ? msg.min_profit_bps : null;
      att.oppFilter.exchanges = Array.isArray(msg.exchanges)
        ? (msg.exchanges as string[]).map((s) => String(s))
        : null;
      ws.serializeAttachment(att);

      // Immediate snapshot of all current matching opportunities.
      for (const opps of this.opportunities.values()) {
        for (const opp of opps) {
          if (!matchesFilter(opp, att)) continue;
          try {
            ws.send(JSON.stringify({ type: "arb_opportunity", ...opp }));
            this.totalPushedOpps++;
          } catch {
            return;
          }
        }
      }
      return;
    }

    if (action === "unsubscribe_opportunities") {
      att.oppFilter.subscribed = false;
      att.oppFilter.min_profit_bps = null;
      att.oppFilter.exchanges = null;
      ws.serializeAttachment(att);
      return;
    }

    this.sendJson(ws, { error: `unknown action: ${action}` });
  }

  async webSocketClose(
    _ws: WebSocket,
    _code: number,
    _reason: string,
    _wasClean: boolean,
  ): Promise<void> {
    /* runtime auto-replies to close frames */
  }

  // ── REST handlers ────────────────────────────────────────────────

  private async handleOpportunitiesRest(url: URL): Promise<Response> {
    const tokenFilter = url.searchParams.get("token");
    const minProfitBpsParam = url.searchParams.get("min_profit_bps");

    if (minProfitBpsParam !== null) {
      const override = Number(minProfitBpsParam);
      if (!Number.isFinite(override)) {
        return this.json({ error: "min_profit_bps must be numeric" }, 400);
      }
      // Live re-scan with custom threshold. Apply the same tradeability
      // filter as rescanToken() so REST and WS callers see consistent
      // results — otherwise an explorer hitting /arb/opportunities?token=X
      // would still see opportunities on legs that the cached scanner
      // already filtered out.
      const result: ArbOpportunity[] = [];
      for (const [token, exchMap] of Object.entries(this.discovery.pairs)) {
        if (ARB_EXCLUDED_TOKENS.has(token)) continue;
        if (tokenFilter && token !== tokenFilter.toUpperCase()) continue;
        const tradeableMap = this.filterTradeableLegs(exchMap);
        const opps = findArbForToken(token, tradeableMap, this.books, {
          meta: this.discovery.meta,
          overrideMinBps: override,
        });
        result.push(...opps);
      }
      result.sort((a, b) => b.net_profit_bps - a.net_profit_bps);
      return this.json(result);
    }

    if (tokenFilter) {
      const opps = this.opportunities.get(tokenFilter.toUpperCase()) ?? [];
      return this.json(opps);
    }

    const result: ArbOpportunity[] = [];
    for (const opps of this.opportunities.values()) {
      result.push(...opps);
    }
    result.sort((a, b) => b.net_profit_bps - a.net_profit_bps);
    return this.json(result);
  }

  // ── Utilities ────────────────────────────────────────────────────

  private totalCurrentOpps(): number {
    let n = 0;
    for (const v of this.opportunities.values()) n += v.length;
    return n;
  }

  private readAttachment(ws: WebSocket): ArbWsAttachment | null {
    try {
      const raw = ws.deserializeAttachment();
      if (
        raw &&
        typeof raw === "object" &&
        Array.isArray((raw as ArbWsAttachment).watch) &&
        typeof (raw as ArbWsAttachment).oppFilter === "object"
      ) {
        return raw as ArbWsAttachment;
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

// ── Helpers ────────────────────────────────────────────────────────

function watchKey(token: string, buyExch: string, sellExch: string): string {
  return `${token}:${buyExch}:${sellExch}`;
}

function parseWatchKey(
  key: string,
): { token: string; buyExch: string; sellExch: string } | null {
  const parts = key.split(":");
  if (parts.length !== 3) return null;
  return { token: parts[0]!, buyExch: parts[1]!, sellExch: parts[2]! };
}

function matchesFilter(
  opp: ArbOpportunity,
  att: ArbWsAttachment,
): boolean {
  const f = att.oppFilter;
  if (!f.subscribed) return false;
  if (f.exchanges && f.exchanges.length > 0) {
    if (
      !f.exchanges.includes(opp.buy_exchange) ||
      !f.exchanges.includes(opp.sell_exchange)
    ) {
      return false;
    }
  }
  if (f.min_profit_bps !== null && opp.net_profit_bps < f.min_profit_bps) {
    return false;
  }
  return true;
}

/**
 * Two opportunity lists are "equal" if they reference the same
 * (buy_exch, sell_exch) pairs with spreads within 0.5 bps of each other.
 * We don't broadcast micro-changes to avoid client firehose.
 */
function opportunitiesEqual(
  a: ArbOpportunity[],
  b: ArbOpportunity[],
): boolean {
  if (a.length !== b.length) return false;
  if (a.length === 0) return true;
  const byKey = new Map<string, ArbOpportunity>();
  for (const o of a) byKey.set(`${o.buy_exchange}:${o.sell_exchange}`, o);
  for (const o of b) {
    const prev = byKey.get(`${o.buy_exchange}:${o.sell_exchange}`);
    if (!prev) return false;
    if (Math.abs(prev.net_profit_bps - o.net_profit_bps) > 0.5) return false;
  }
  return true;
}

function round1(v: number): number { return Math.round(v * 10) / 10; }
function round2(v: number): number { return Math.round(v * 100) / 100; }
