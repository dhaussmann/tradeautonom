/**
 * NadoOms — Nado orderbook state + fan-out, with the upstream WebSocket
 * terminated in a Node.js relay container (not in the Worker).
 *
 * WHY A CONTAINER?
 *   Nado's subscription gateway (wss://gateway.prod.nado.xyz/v1/subscribe)
 *   REQUIRES `Sec-WebSocket-Extensions: permessage-deflate`. Cloudflare
 *   Workers' outbound WebSocket client does not negotiate extensions,
 *   so connecting directly returns HTTP 403:
 *     { "reason": "Invalid compression headers: 'Sec-WebSocket-Extensions'
 *                  must include 'permessage-deflate'", "block": true }
 *   See docs/v2-oms-cloudflare-native.md for the full rationale.
 *
 * DATA FLOW:
 *   [NadoOms DO]  --WS to container-- [NadoRelayContainer]  --WS (+deflate)-- [Nado]
 *
 *   1. On startup / on newly-tracked product_ids:
 *      - REST /query(market_liquidity) snapshot per product (x18 → float)
 *      - open WS to NadoRelayContainer at /ws
 *      - send {op:"resubscribe_all", product_ids:[...]}
 *   2. Container holds upstream WS with deflate + 30s ping, re-subscribes
 *      upstream, forwards every parsed event as {type:"event",event:<raw>}
 *   3. NadoOms applies delta logic to its local book state and fans out
 *      to AggregatorDO via RPC (same as other exchanges).
 *
 * The container is a "dumb" transport — no book state lives there.
 *
 * Reference (Python V1 source of truth): deploy/monitor/monitor_service.py
 * (lines 1635-1869). The delta / seq-gap / snapshot semantics below match
 * that implementation.
 */

import { DurableObject } from "cloudflare:workers";
import type { Env, BookSnapshot } from "../types";

const X18_DIVISOR = 1e18;
const ALARM_INTERVAL_MS = 30_000;
const STALE_RELAY_MS = 120_000;

const GATEWAY_URLS: Record<string, string> = {
  mainnet: "https://gateway.prod.nado.xyz",
  testnet: "https://gateway.sepolia.nado.xyz",
};

/** Relay container binding name; `.idFromName("singleton")` keeps one instance. */
const RELAY_SINGLETON = "singleton";
/** Path inside the container where it serves the internal WS endpoint. */
const RELAY_WS_PATH = "/ws";

interface SideBook {
  bidLevels: Map<string, [number, number]>;
  askLevels: Map<string, [number, number]>;
  ts_ms: number;
  connected: boolean;
  updates: number;
}

function emptySideBook(): SideBook {
  return {
    bidLevels: new Map(),
    askLevels: new Map(),
    ts_ms: 0,
    connected: false,
    updates: 0,
  };
}

export class NadoOms extends DurableObject<Env> {
  private nadoEnv: string;
  private books: Map<string, SideBook> = new Map();
  private trackedSymbols: Set<string> = new Set();
  private productIds: Map<string, number> = new Map(); // symbol → product_id
  private productToSymbol: Map<number, string> = new Map();
  private snapshotTs: Map<number, string> = new Map();
  private lastMaxTs: Map<number, string> = new Map();

  /** WebSocket to the NadoRelayContainer. */
  private relayWs: WebSocket | null = null;
  private relayState: "connected" | "connecting" | "disconnected" = "disconnected";
  /** Whether the relay container's UPSTREAM socket to Nado is up. */
  private upstreamConnected: boolean = false;
  private reconnectAttempts = 0;
  /** Last JSON event (including hello / upstream_connected) from relay. */
  private lastMessageMs: number | null = null;
  /** Last actual Nado book_depth event (for staleness). */
  private lastEventMs: number | null = null;
  private startedAt: number = Date.now();
  private pendingResync: boolean = false;

  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    this.nadoEnv = "mainnet";
    state.blockConcurrencyWhile(async () => {
      const stored = (await state.storage.get<string[]>("tracked")) ?? [];
      for (const s of stored) this.trackedSymbols.add(s);
      const storedPids =
        (await state.storage.get<Record<string, number>>("product_ids")) ?? {};
      for (const [sym, pid] of Object.entries(storedPids)) {
        this.productIds.set(sym, pid);
        this.productToSymbol.set(pid, sym);
      }
      if (this.trackedSymbols.size > 0) {
        await this.ensureRelay();
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

  /**
   * Add symbols to track. Passed from the auto-discovery Worker which has
   * already resolved product_ids via Nado `/symbols`.
   */
  async ensureTracking(
    entries: Array<{ symbol: string; product_id: number }>,
  ): Promise<{ ok: true; added: number }> {
    let added = 0;
    for (const { symbol, product_id } of entries) {
      if (!this.trackedSymbols.has(symbol)) {
        this.trackedSymbols.add(symbol);
        this.productIds.set(symbol, product_id);
        this.productToSymbol.set(product_id, symbol);
        added += 1;
      }
    }
    if (added > 0) {
      await this.ctx.storage.put("tracked", Array.from(this.trackedSymbols));
      const pidMap: Record<string, number> = {};
      for (const [s, p] of this.productIds) pidMap[s] = p;
      await this.ctx.storage.put("product_ids", pidMap);
      this.pendingResync = true;
      await this.ensureRelay();
    }
    return { ok: true, added };
  }

  async listMarkets(): Promise<string[]> {
    return Array.from(this.books.keys()).sort();
  }

  // ── HTTP ─────────────────────────────────────────────────────────

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;
    if (path === "/health") {
      return this.json({
        status: "ok",
        relay_state: this.relayState,
        upstream_connected: this.upstreamConnected,
        reconnect_attempts: this.reconnectAttempts,
        last_message_ms: this.lastMessageMs,
        last_event_ms: this.lastEventMs,
        tracked_symbols: this.trackedSymbols.size,
        markets_with_data: this.books.size,
        product_id_cache: this.productIds.size,
        uptime_ms: Date.now() - this.startedAt,
      });
    }
    return this.json({ error: "not found", path }, 404);
  }

  async alarm(): Promise<void> {
    await this.ensureRelay();
    await this.ctx.storage.setAlarm(Date.now() + ALARM_INTERVAL_MS);
  }

  // ── Relay lifecycle ─────────────────────────────────────────────

  /**
   * Ensure there's a live WebSocket to the relay container. Also performs
   * the REST snapshot step BEFORE the relay is connected, so the first
   * deltas can be applied on top of a fresh book.
   */
  private async ensureRelay(): Promise<void> {
    if (this.trackedSymbols.size === 0) return;
    if (this.relayState === "connecting") return;

    // Already connected and not stale → just (re)send the desired subscription.
    if (this.relayWs && this.relayState === "connected" && !this.pendingResync) {
      const stale =
        this.lastMessageMs !== null &&
        Date.now() - this.lastMessageMs > STALE_RELAY_MS;
      if (!stale) return;
      console.warn("NADO relay stale, reconnecting");
      try {
        this.relayWs.close();
      } catch {
        /* ignore */
      }
      this.relayWs = null;
      this.relayState = "disconnected";
    }

    if (this.pendingResync && this.relayWs) {
      try {
        this.relayWs.close();
      } catch {
        /* ignore */
      }
      this.relayWs = null;
      this.relayState = "disconnected";
      this.pendingResync = false;
    }

    this.relayState = "connecting";
    this.reconnectAttempts += 1;

    // 1) Snapshot every tracked product via REST before deltas start.
    await this.takeRestSnapshots();

    // 2) Open WS to the relay container.
    try {
      const id = this.env.NADO_RELAY.idFromName(RELAY_SINGLETON);
      const stub = this.env.NADO_RELAY.get(id);
      // Container Durable Objects accept plain http:// URLs; the scheme is
      // not validated.
      const req = new Request(`http://nado-relay${RELAY_WS_PATH}`, {
        headers: { Upgrade: "websocket" },
      });
      const resp = await stub.fetch(req);
      if (resp.status !== 101 || !resp.webSocket) {
        const body = await resp.text().catch(() => "");
        console.error("NADO relay upgrade failed", {
          status: resp.status,
          body: body.slice(0, 300),
        });
        this.relayState = "disconnected";
        return;
      }
      const ws = resp.webSocket;
      ws.accept();
      this.relayWs = ws;
      this.relayState = "connected";

      ws.addEventListener("message", (e) => this.onRelayMessage(e));
      ws.addEventListener("close", (e) => this.onRelayClose(e));
      ws.addEventListener("error", (e) => this.onRelayError(e));

      // 3) Tell the relay which product_ids to subscribe to.
      const pids = Array.from(this.productIds.values());
      ws.send(
        JSON.stringify({ op: "resubscribe_all", product_ids: pids }),
      );
      console.log("NADO relay connected", { pids: pids.length });
    } catch (err) {
      console.error(
        "NADO relay open threw",
        err instanceof Error ? err.message : err,
      );
      this.relayState = "disconnected";
    }
  }

  private async takeRestSnapshots(): Promise<void> {
    const gateway = GATEWAY_URLS[this.nadoEnv] ?? GATEWAY_URLS.mainnet;
    for (const symbol of this.trackedSymbols) {
      const pid = this.productIds.get(symbol);
      if (pid === undefined) continue;
      try {
        const resp = await fetch(`${gateway}/query`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
          },
          body: JSON.stringify({
            type: "market_liquidity",
            product_id: pid,
            depth: 100,
          }),
        });
        if (!resp.ok) {
          console.warn("NADO snapshot failed", {
            symbol,
            pid,
            status: resp.status,
          });
          this.snapshotTs.set(pid, "0");
          this.lastMaxTs.set(pid, "0");
          continue;
        }
        const body = (await resp.json()) as any;
        const data = body?.data ?? {};
        const snapTs: string = String(data.timestamp ?? "0");
        this.snapshotTs.set(pid, snapTs);
        this.lastMaxTs.set(pid, "0");

        let book = this.books.get(symbol);
        if (!book) {
          book = emptySideBook();
          this.books.set(symbol, book);
        }
        book.bidLevels.clear();
        book.askLevels.clear();
        applyNadoLevels(book.bidLevels, data.bids);
        applyNadoLevels(book.askLevels, data.asks);
        book.ts_ms = Date.now();
        book.connected = true;
        book.updates += 1;
        this.fanOut(this.toSnapshot(symbol, book));
      } catch (err) {
        console.warn("NADO snapshot threw", {
          symbol,
          err: String(err),
        });
        this.snapshotTs.set(pid, "0");
        this.lastMaxTs.set(pid, "0");
      }
    }
  }

  // ── Relay WS event handlers ─────────────────────────────────────

  private onRelayMessage(event: MessageEvent): void {
    this.lastMessageMs = Date.now();
    const raw =
      typeof event.data === "string"
        ? event.data
        : new TextDecoder().decode(event.data as ArrayBuffer);

    let msg: any;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }

    if (msg?.type === "hello") {
      console.log("NADO relay hello", {
        relay_version: msg.relay_version,
      });
      return;
    }
    if (msg?.type === "upstream_connected") {
      this.upstreamConnected = true;
      console.log("NADO upstream connected");
      return;
    }
    if (msg?.type === "upstream_disconnected") {
      this.upstreamConnected = false;
      console.warn("NADO upstream disconnected", { reason: msg.reason });
      for (const b of this.books.values()) b.connected = false;
      return;
    }
    if (msg?.type === "event") {
      this.lastEventMs = Date.now();
      this.onNadoEvent(msg.event);
      return;
    }
  }

  private onNadoEvent(event: unknown): void {
    if (!event || typeof event !== "object") return;
    const msg = event as Record<string, unknown>;

    // Normalize envelope: either top-level {bids,asks,product_id,...} or {data:{...}}
    const envelope =
      msg.data && typeof msg.data === "object"
        ? (msg.data as Record<string, unknown>)
        : msg;
    if (
      !envelope ||
      (envelope.bids === undefined && envelope.asks === undefined)
    ) {
      return;
    }

    const pid = typeof envelope.product_id === "number"
      ? envelope.product_id
      : undefined;
    if (pid === undefined) return;
    const symbol = this.productToSymbol.get(pid);
    if (!symbol || !this.trackedSymbols.has(symbol)) return;

    const msgMaxTs = String(envelope.max_timestamp ?? "0");
    const msgLastMaxTs = String(envelope.last_max_timestamp ?? "0");
    const snapTs = this.snapshotTs.get(pid) ?? "0";
    const last = this.lastMaxTs.get(pid) ?? "0";

    // Drop events at or before the snapshot timestamp (REST already covered them).
    if (msgMaxTs !== "0" && snapTs !== "0" && msgMaxTs <= snapTs) return;

    // Sequence continuity check.
    if (last !== "0" && msgLastMaxTs !== last) {
      console.warn("NADO seq gap", {
        symbol,
        expected_last_max: last,
        got_last_max: msgLastMaxTs,
      });
      // Full resync: close relay, reconnect, re-snapshot.
      this.pendingResync = true;
      try {
        this.relayWs?.close();
      } catch {
        /* ignore */
      }
      return;
    }

    let book = this.books.get(symbol);
    if (!book) {
      book = emptySideBook();
      this.books.set(symbol, book);
    }

    applyNadoLevels(book.bidLevels, envelope.bids);
    applyNadoLevels(book.askLevels, envelope.asks);
    book.ts_ms = Date.now();
    book.connected = true;
    book.updates += 1;
    this.lastMaxTs.set(pid, msgMaxTs);

    this.fanOut(this.toSnapshot(symbol, book));
  }

  private fanOut(snap: BookSnapshot): void {
    const agg = this.env.AGGREGATOR_DO.get(
      this.env.AGGREGATOR_DO.idFromName("aggregator"),
    );
    agg.onBookUpdate(snap).catch(() => {
      /* drop */
    });
  }

  private onRelayClose(event: CloseEvent): void {
    console.warn("NADO relay closed", {
      code: event.code,
      reason: event.reason,
    });
    this.relayWs = null;
    this.relayState = "disconnected";
    this.upstreamConnected = false;
    for (const b of this.books.values()) b.connected = false;
  }

  private onRelayError(event: Event): void {
    console.error("NADO relay error", event.type);
  }

  private toSnapshot(market: string, book: SideBook): BookSnapshot {
    const bids: Array<[number, number]> = Array.from(
      book.bidLevels.values(),
    ).sort((a, b) => b[0] - a[0]);
    const asks: Array<[number, number]> = Array.from(
      book.askLevels.values(),
    ).sort((a, b) => a[0] - b[0]);
    return {
      exchange: "nado",
      symbol: market,
      bids: bids.slice(0, 10),
      asks: asks.slice(0, 10),
      timestamp_ms: book.ts_ms,
      connected:
        book.connected &&
        this.relayState === "connected" &&
        this.upstreamConnected,
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

function applyNadoLevels(
  sideMap: Map<string, [number, number]>,
  deltas: unknown,
): void {
  if (!Array.isArray(deltas)) return;
  for (const entry of deltas) {
    if (!Array.isArray(entry) || entry.length < 2) continue;
    const priceRaw = entry[0];
    const sizeRaw = entry[1];
    // x18 strings / numbers — divide by 1e18.
    const price = Number(priceRaw) / X18_DIVISOR;
    const size = Number(sizeRaw) / X18_DIVISOR;
    if (Number.isNaN(price) || Number.isNaN(size)) continue;
    // Use the raw string (stable) as map key for removal/upsert.
    const key = String(priceRaw);
    if (size <= 0) {
      sideMap.delete(key);
    } else {
      sideMap.set(key, [price, size]);
    }
  }
}
