/**
 * Nado Relay Container — entrypoint.
 *
 * Architecture:
 *   Cloudflare Worker (NadoOms DO) -- WS --> [THIS CONTAINER] -- WS (+deflate) --> Nado Gateway
 *
 * Responsibilities:
 *   - Accept exactly one WebSocket from the NadoOms DO at path /ws
 *   - Maintain exactly one upstream WebSocket to Nado with permessage-deflate
 *   - Forward control messages (subscribe/unsubscribe/resubscribe_all) upstream
 *   - Forward every upstream JSON event downstream to the DO
 *
 * The container is "dumb": it keeps NO book state. All normalization,
 * delta-application, and fan-out stays in the NadoOms DO so the code path
 * matches Extended/GRVT/Variational.
 *
 * Health:
 *   GET /health   → { ok: true, upstream: {...}, do_connected: bool }
 */

import http from "node:http";
import { WebSocketServer, type WebSocket as WsSocket } from "ws";
import { NadoUpstream } from "./nado-upstream.js";
import type {
  ContainerToDoMessage,
  DoToContainerMessage,
} from "./types.js";

const PORT = Number(process.env.PORT ?? 8080);
const RELAY_VERSION = "0.1.0";
const STARTED_AT_MS = Date.now();

let doSocket: WsSocket | null = null;

function sendToDo(msg: ContainerToDoMessage): void {
  if (!doSocket || doSocket.readyState !== 1 /* OPEN */) return;
  try {
    doSocket.send(JSON.stringify(msg));
  } catch (err) {
    console.warn(
      JSON.stringify({
        evt: "send_to_do_error",
        err: err instanceof Error ? err.message : String(err),
      }),
    );
  }
}

const upstream = new NadoUpstream({
  onEvent: (event) => {
    sendToDo({ type: "event", at_ms: Date.now(), event });
  },
  onConnected: () => {
    sendToDo({ type: "upstream_connected", at_ms: Date.now() });
  },
  onDisconnected: (reason) => {
    sendToDo({
      type: "upstream_disconnected",
      at_ms: Date.now(),
      reason,
    });
  },
});

// Start upstream immediately; we reconnect forever in the background.
// Even before the DO connects, we can verify connectivity to Nado.
upstream.start();

// Root HTTP server (handles both /health and /ws upgrade).
const server = http.createServer((req, res) => {
  if (req.url === "/health") {
    const body = JSON.stringify({
      ok: true,
      relay_version: RELAY_VERSION,
      started_at_ms: STARTED_AT_MS,
      uptime_ms: Date.now() - STARTED_AT_MS,
      do_connected: !!(doSocket && doSocket.readyState === 1),
      upstream: upstream.state(),
    });
    res.writeHead(200, { "content-type": "application/json" });
    res.end(body);
    return;
  }
  if (req.url === "/") {
    res.writeHead(200, { "content-type": "text/plain" });
    res.end(
      [
        "Nado Relay Container",
        `version=${RELAY_VERSION}`,
        "",
        "Endpoints:",
        "  GET /health    — JSON status",
        "  WS  /ws        — single NadoOms DO connection (control + event stream)",
      ].join("\n"),
    );
    return;
  }
  res.writeHead(404, { "content-type": "text/plain" });
  res.end("not found");
});

const wss = new WebSocketServer({ server, path: "/ws" });

wss.on("connection", (socket, request) => {
  console.log(
    JSON.stringify({
      evt: "do_connected",
      remote: request.socket.remoteAddress,
    }),
  );

  // Only one DO is expected; if a second shows up, close the older one.
  if (doSocket && doSocket !== socket) {
    try {
      doSocket.close(1001, "replaced by newer connection");
    } catch {
      /* ignore */
    }
  }
  doSocket = socket;

  sendToDo({
    type: "hello",
    relay_version: RELAY_VERSION,
    started_at_ms: STARTED_AT_MS,
  });

  // Surface current upstream state immediately.
  const upstate = upstream.state();
  if (upstate.connected) {
    sendToDo({ type: "upstream_connected", at_ms: Date.now() });
  } else {
    sendToDo({
      type: "upstream_disconnected",
      at_ms: Date.now(),
      reason: "upstream not yet connected",
    });
  }

  socket.on("message", (data, isBinary) => {
    let msg: DoToContainerMessage;
    try {
      const text = isBinary
        ? Buffer.from(data as Buffer).toString("utf-8")
        : (data as Buffer).toString("utf-8");
      msg = JSON.parse(text) as DoToContainerMessage;
    } catch (err) {
      console.warn(
        JSON.stringify({
          evt: "do_msg_parse_error",
          err: err instanceof Error ? err.message : String(err),
        }),
      );
      return;
    }

    if (msg.op === "subscribe") {
      upstream.subscribe(msg.product_id);
    } else if (msg.op === "unsubscribe") {
      upstream.unsubscribe(msg.product_id);
    } else if (msg.op === "resubscribe_all") {
      upstream.resubscribeAll(msg.product_ids);
    } else {
      console.warn(
        JSON.stringify({
          evt: "do_unknown_op",
          msg: msg as unknown,
        }),
      );
    }
  });

  socket.on("close", (code, reason) => {
    if (doSocket === socket) doSocket = null;
    console.log(
      JSON.stringify({
        evt: "do_disconnected",
        code,
        reason: reason?.toString("utf-8") ?? "",
      }),
    );
  });

  socket.on("error", (err) => {
    console.warn(
      JSON.stringify({
        evt: "do_socket_error",
        err: err instanceof Error ? err.message : String(err),
      }),
    );
  });
});

server.listen(PORT, () => {
  console.log(
    JSON.stringify({
      evt: "relay_listening",
      port: PORT,
      version: RELAY_VERSION,
    }),
  );
});

function shutdown(signal: string): void {
  console.log(JSON.stringify({ evt: "shutdown", signal }));
  upstream.stop();
  wss.close();
  server.close(() => process.exit(0));
  // Fallback forced exit in case sockets hang.
  setTimeout(() => process.exit(0), 5_000).unref();
}

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
