/**
 * Upstream WebSocket client to Nado.
 *
 * Handles:
 *   - permessage-deflate (why this container exists at all)
 *   - 30-second ping frames per docs
 *   - Automatic reconnect with exponential backoff (capped)
 *   - Re-subscribing all tracked product_ids on reconnect
 *   - Forwarding parsed JSON events to a callback
 *
 * Nado protocol reference:
 *   https://docs.nado.xyz/developer-resources/api/subscriptions
 *   https://docs.nado.xyz/developer-resources/api/subscriptions/streams
 */

import WebSocket from "ws";

const UPSTREAM_URL = "wss://gateway.prod.nado.xyz/v1/subscribe";
const PING_INTERVAL_MS = 30_000;
const RECONNECT_MIN_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

export interface NadoUpstreamCallbacks {
  onEvent: (evt: unknown) => void;
  onConnected: () => void;
  onDisconnected: (reason: string) => void;
}

export class NadoUpstream {
  private ws: WebSocket | null = null;
  private pingTimer: NodeJS.Timeout | null = null;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private reconnectDelayMs = RECONNECT_MIN_MS;
  private subscribed: Set<number> = new Set();
  private stopped = false;

  constructor(private readonly cb: NadoUpstreamCallbacks) {}

  start(): void {
    this.stopped = false;
    this.openSocket();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    if (this.ws) {
      try {
        this.ws.close(1000, "relay shutting down");
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
  }

  /** Replace the full set of tracked product_ids. */
  resubscribeAll(productIds: number[]): void {
    this.subscribed.clear();
    for (const pid of productIds) this.subscribed.add(pid);
    this.sendAllSubscribes();
  }

  subscribe(productId: number): void {
    if (this.subscribed.has(productId)) return;
    this.subscribed.add(productId);
    this.sendSubscribe(productId);
  }

  unsubscribe(productId: number): void {
    if (!this.subscribed.has(productId)) return;
    this.subscribed.delete(productId);
    this.sendUnsubscribe(productId);
  }

  state(): {
    connected: boolean;
    subscribed_count: number;
    reconnect_delay_ms: number;
  } {
    return {
      connected: this.ws?.readyState === WebSocket.OPEN,
      subscribed_count: this.subscribed.size,
      reconnect_delay_ms: this.reconnectDelayMs,
    };
  }

  // ── internals ───────────────────────────────────────────────────

  private openSocket(): void {
    if (this.stopped) return;
    if (this.ws) {
      try {
        this.ws.removeAllListeners();
        this.ws.terminate();
      } catch {
        /* ignore */
      }
      this.ws = null;
    }

    console.log(
      JSON.stringify({
        evt: "nado_upstream_opening",
        url: UPSTREAM_URL,
        subscribed: this.subscribed.size,
      }),
    );

    const ws = new WebSocket(UPSTREAM_URL, {
      // ws defaults include permessage-deflate offer; be explicit anyway.
      perMessageDeflate: {
        clientNoContextTakeover: false,
        serverNoContextTakeover: false,
        clientMaxWindowBits: 15,
        serverMaxWindowBits: 15,
      },
      handshakeTimeout: 15_000,
      headers: {
        "User-Agent": "tradeautonom-nado-relay/0.1",
      },
    });
    this.ws = ws;

    ws.on("open", () => {
      this.reconnectDelayMs = RECONNECT_MIN_MS;
      console.log(JSON.stringify({ evt: "nado_upstream_open" }));
      this.cb.onConnected();
      // Start 30s ping cadence required by Nado docs.
      this.startPings();
      // Re-subscribe any known product_ids.
      this.sendAllSubscribes();
    });

    ws.on("message", (data: WebSocket.RawData, isBinary: boolean) => {
      try {
        const text = isBinary
          ? Buffer.from(data as Buffer).toString("utf-8")
          : data.toString("utf-8");
        const parsed = JSON.parse(text);
        this.cb.onEvent(parsed);
      } catch (err) {
        console.warn(
          JSON.stringify({
            evt: "nado_upstream_parse_error",
            err: err instanceof Error ? err.message : String(err),
          }),
        );
      }
    });

    ws.on("pong", () => {
      // Received pong — connection is alive.
    });

    ws.on("close", (code: number, reason: Buffer) => {
      this.clearTimers();
      const reasonStr = reason?.toString("utf-8") || `code=${code}`;
      console.warn(
        JSON.stringify({
          evt: "nado_upstream_close",
          code,
          reason: reasonStr,
        }),
      );
      this.cb.onDisconnected(reasonStr);
      this.scheduleReconnect();
    });

    ws.on("error", (err) => {
      console.warn(
        JSON.stringify({
          evt: "nado_upstream_error",
          err: err instanceof Error ? err.message : String(err),
        }),
      );
      // A close event follows; don't schedule reconnect here.
    });

    ws.on("unexpected-response", (_req, res) => {
      let body = "";
      res.on("data", (chunk: Buffer) => {
        body += chunk.toString("utf-8");
        if (body.length > 500) body = body.slice(0, 500);
      });
      res.on("end", () => {
        console.warn(
          JSON.stringify({
            evt: "nado_upstream_unexpected_response",
            status: res.statusCode,
            body,
          }),
        );
      });
    });
  }

  private startPings(): void {
    this.clearPingTimer();
    this.pingTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        try {
          this.ws.ping();
        } catch {
          /* ignore */
        }
      }
    }, PING_INTERVAL_MS);
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    if (this.reconnectTimer) return;
    const delay = this.reconnectDelayMs;
    console.log(
      JSON.stringify({ evt: "nado_upstream_reconnect_scheduled", delay_ms: delay }),
    );
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.openSocket();
    }, delay);
    this.reconnectDelayMs = Math.min(this.reconnectDelayMs * 2, RECONNECT_MAX_MS);
  }

  private sendSubscribe(productId: number): void {
    this.sendRaw({
      method: "subscribe",
      stream: { type: "book_depth", product_id: productId },
      id: productId,
    });
  }

  private sendUnsubscribe(productId: number): void {
    this.sendRaw({
      method: "unsubscribe",
      stream: { type: "book_depth", product_id: productId },
      id: productId,
    });
  }

  private sendAllSubscribes(): void {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    for (const pid of this.subscribed) {
      this.sendSubscribe(pid);
    }
  }

  private sendRaw(payload: unknown): void {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    try {
      this.ws.send(JSON.stringify(payload));
    } catch (err) {
      console.warn(
        JSON.stringify({
          evt: "nado_upstream_send_error",
          err: err instanceof Error ? err.message : String(err),
        }),
      );
    }
  }

  private clearTimers(): void {
    this.clearPingTimer();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private clearPingTimer(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }
}
