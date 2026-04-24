/**
 * NadoOms — outbound WebSocket to Nado gateway with:
 *   - REST /symbols discovery → product_id mapping
 *   - REST /query(market_liquidity) snapshot per tracked product BEFORE WS
 *   - WS /v1/subscribe with one subscribe frame per product_id
 *   - Delta-application with x18 divisor (1e18)
 *   - Sequence-gap detection via max_timestamp / last_max_timestamp; on gap, reconnect + re-snapshot
 *
 * Reference: deploy/monitor/monitor_service.py::_run_nado_ws_all
 *
 * Caveat: Photon OMS uses permessage-deflate extension. Cloudflare Workers
 * WebSocket client does not document deflate support. We try without; if Nado
 * sends compressed frames we'll fail to parse and have to switch to a gateway.
 * Most production WS feeds negotiate deflate as optional, so raw JSON is
 * typically served if the client doesn't advertise the extension.
 */

import { DurableObject } from "cloudflare:workers";
import type { Env, BookSnapshot } from "../types";

const X18_DIVISOR = 1e18;
const ALARM_INTERVAL_MS = 30_000;
const SUB_PACE_MS = 50;

// CF Workers fetch() requires https:// even for WebSocket upgrades.
const WS_URLS: Record<string, string> = {
  mainnet: "https://gateway.prod.nado.xyz/v1/subscribe",
  testnet: "https://gateway.sepolia.nado.xyz/v1/subscribe",
};
const GATEWAY_URLS: Record<string, string> = {
  mainnet: "https://gateway.prod.nado.xyz",
  testnet: "https://gateway.sepolia.nado.xyz",
};

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

  private ws: WebSocket | null = null;
  private wsState: "connected" | "disconnected" | "connecting" = "disconnected";
  private reconnectAttempts = 0;
  private lastMessageMs: number | null = null;
  private startedAt: number = Date.now();
  private pendingReconnect: boolean = false;

  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    this.nadoEnv = "mainnet"; // could be made configurable via env vars later
    state.blockConcurrencyWhile(async () => {
      const stored = (await state.storage.get<string[]>("tracked")) ?? [];
      for (const s of stored) this.trackedSymbols.add(s);
      const storedPids = (await state.storage.get<Record<string, number>>("product_ids")) ?? {};
      for (const [sym, pid] of Object.entries(storedPids)) {
        this.productIds.set(sym, pid);
        this.productToSymbol.set(pid, sym);
      }
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

  /**
   * Add symbols to track. We need product_ids; these are passed from the
   * auto-discovery Worker which already calls /symbols. This keeps the DO
   * from having to re-fetch /symbols every time a market is added.
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
      this.pendingReconnect = true;
      await this.ensureWs();
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
        ws_state: this.wsState,
        reconnect_attempts: this.reconnectAttempts,
        last_message_ms: this.lastMessageMs,
        tracked_symbols: this.trackedSymbols.size,
        markets_with_data: this.books.size,
        product_id_cache: this.productIds.size,
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
        console.warn("NADO WS stale, closing");
        try { this.ws.close(); } catch { /* ignore */ }
        this.ws = null;
        this.wsState = "disconnected";
      } else {
        return;
      }
    }

    if (this.pendingReconnect && this.ws) {
      try { this.ws.close(); } catch { /* ignore */ }
      this.ws = null;
      this.wsState = "disconnected";
      this.pendingReconnect = false;
    }

    this.wsState = "connecting";
    this.reconnectAttempts += 1;

    const gateway = GATEWAY_URLS[this.nadoEnv] ?? GATEWAY_URLS.mainnet;
    const wsUrl = WS_URLS[this.nadoEnv] ?? WS_URLS.mainnet;
    console.log("NADO opening WS", {
      attempt: this.reconnectAttempts,
      symbols: this.trackedSymbols.size,
      url: wsUrl,
    });

    // 1) Snapshot every tracked symbol via REST before opening WS.
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
          console.warn("NADO snapshot failed", { symbol, pid, status: resp.status });
          this.snapshotTs.set(pid, "0");
          this.lastMaxTs.set(pid, "0");
          continue;
        }
        const body = (await resp.json()) as any;
        const data = body?.data ?? {};
        const snapTs: string = data.timestamp ?? "0";
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
        console.warn("NADO snapshot threw", { symbol, err: String(err) });
        this.snapshotTs.set(pid, "0");
        this.lastMaxTs.set(pid, "0");
      }
    }

    // 2) Open WS
    try {
      const resp = await fetch(wsUrl, {
        headers: { Upgrade: "websocket", "User-Agent": "tradeautonom-oms-v2/0.1" },
      });
      if (resp.status !== 101 || !resp.webSocket) {
        const bodyPreview = await resp.text().catch(() => "");
        console.error("NADO WS upgrade failed", {
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

      // 3) Subscribe per product_id with pacing
      for (const symbol of this.trackedSymbols) {
        const pid = this.productIds.get(symbol);
        if (pid === undefined) continue;
        ws.send(
          JSON.stringify({
            method: "subscribe",
            stream: { type: "book_depth", product_id: pid },
            id: pid,
          }),
        );
        await sleep(SUB_PACE_MS);
      }
      console.log("NADO subscribed", { products: this.trackedSymbols.size });

      ws.addEventListener("message", (e) => this.onMessage(e));
      ws.addEventListener("close", (e) => this.onClose(e));
      ws.addEventListener("error", (e) => this.onError(e));
    } catch (err) {
      console.error("NADO WS open threw", err instanceof Error ? err.message : err);
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

    // Normalize envelope: either top-level {bids, asks, product_id, ...} or {data: {...}}
    const envelope = msg.data && typeof msg.data === "object" ? msg.data : msg;
    if (!envelope || (envelope.bids === undefined && envelope.asks === undefined)) return;

    const pid: number | undefined = envelope.product_id;
    if (pid === undefined) return;
    const symbol = this.productToSymbol.get(pid);
    if (!symbol || !this.trackedSymbols.has(symbol)) return;

    const msgMaxTs: string = String(envelope.max_timestamp ?? "0");
    const msgLastMaxTs: string = String(envelope.last_max_timestamp ?? "0");
    const snapTs = this.snapshotTs.get(pid) ?? "0";
    const last = this.lastMaxTs.get(pid) ?? "0";

    // Drop events at or before our snapshot timestamp.
    if (msgMaxTs !== "0" && snapTs !== "0" && msgMaxTs <= snapTs) return;

    // Sequence continuity check.
    if (last !== "0" && msgLastMaxTs !== last) {
      console.warn("NADO seq gap", {
        symbol,
        expected_last_max: last,
        got_last_max: msgLastMaxTs,
      });
      // Trigger full reconnect + re-snapshot
      this.pendingReconnect = true;
      try { this.ws?.close(); } catch { /* ignore */ }
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
    agg.onBookUpdate(snap).catch(() => { /* drop */ });
  }

  private onClose(event: CloseEvent): void {
    console.warn("NADO WS closed", { code: event.code, reason: event.reason });
    this.ws = null;
    this.wsState = "disconnected";
    for (const b of this.books.values()) b.connected = false;
  }

  private onError(event: Event): void {
    console.error("NADO WS error", event.type);
  }

  private toSnapshot(market: string, book: SideBook): BookSnapshot {
    const bids: Array<[number, number]> = Array.from(book.bidLevels.values()).sort(
      (a, b) => b[0] - a[0],
    );
    const asks: Array<[number, number]> = Array.from(book.askLevels.values()).sort(
      (a, b) => a[0] - b[0],
    );
    return {
      exchange: "nado",
      symbol: market,
      bids: bids.slice(0, 10),
      asks: asks.slice(0, 10),
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

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
