/**
 * UserContainer-v2 Worker entrypoint.
 *
 * Routes /u/<user_id>/<path> to that user's Container DO. Addressing is
 * `idFromName(user_id)` — the same user_id used as the primary key in
 * D1's `user` table.
 *
 * Phase F.3 adds shared-secret gating. Every request (except the root
 * friendly banner) must present the header `X-Internal-Token` equal to
 * the `V2_SHARED_TOKEN` Worker secret. The main `tradeautonom` Worker
 * (bot.defitool.de) adds this header when it proxies via the service
 * binding.
 *
 * Phase F.4 adds R2-backed state persistence. The container calls back
 * to /__state/restore and /__state/flush on this Worker, which owns the
 * R2 binding (STATE_BUCKET). No S3 API tokens needed.
 */

import { UserContainer } from "./user-container";
import type { Env } from "./types";

export { UserContainer };

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // Root: friendly banner, always reachable.
    if (url.pathname === "/" || url.pathname === "") {
      return new Response(
        [
          "TradeAutonom UserContainer-v2 Worker",
          "",
          "Requests to /u/<user_id>/<path> require a valid X-Internal-Token",
          "header matching the V2_SHARED_TOKEN Worker secret.",
          "",
          "Typical usage is indirect: bot.defitool.de proxies here via",
          "the USER_V2 service binding after authenticating the user session.",
        ].join("\n"),
        { headers: { "content-type": "text/plain" } },
      );
    }

    // Phase F.4: R2-backed state endpoints. Called by cloud_persistence.py
    // inside each user's Container via HTTPS to user-v2.defitool.de. Bucket
    // binding lives here so containers don't need S3 credentials.
    //
    //   GET  /__state/restore?user_id=<id>  → returns the user's tar.gz (or 404)
    //   POST /__state/flush?user_id=<id>    → stores the request body as tar.gz
    //
    // Both require X-Internal-Token.
    if (url.pathname === "/__state/restore" && request.method === "GET") {
      return handleStateRestore(request, url, env);
    }
    if (url.pathname === "/__state/flush" && request.method === "POST") {
      return handleStateFlush(request, url, env);
    }

    // Admin recycle endpoint: stops the user's Container DO so the next
    // request cold-starts with fresh envVars.
    const recycleMatch = url.pathname.match(/^\/admin\/recycle\/([A-Za-z0-9._-]+)\/?$/);
    if (recycleMatch) {
      const presented = request.headers.get("X-Internal-Token") ?? "";
      const expected = env.V2_SHARED_TOKEN ?? "";
      if (!expected || presented !== expected) {
        return new Response(
          JSON.stringify({ error: "Forbidden" }),
          { status: 403, headers: { "content-type": "application/json" } },
        );
      }
      const targetUserId = recycleMatch[1]!;
      const ns = env.USER_CONTAINER;
      const stub = ns.get(ns.idFromName(targetUserId));
      try {
        const recycleReq = new Request("http://user-v2.internal/__recycle", {
          method: "POST",
          headers: { "X-Internal-Token": expected },
        });
        const resp = await stub.fetch(recycleReq);
        const body = await resp.text();
        return new Response(body, {
          status: resp.status,
          headers: { "content-type": "application/json" },
        });
      } catch (err) {
        return new Response(
          JSON.stringify({
            error: "recycle_failed",
            detail: err instanceof Error ? err.message : String(err),
          }),
          { status: 500, headers: { "content-type": "application/json" } },
        );
      }
    }

    const match = url.pathname.match(/^\/u\/([A-Za-z0-9._-]+)(\/.*)?$/);
    if (!match) {
      return new Response("Not found", { status: 404 });
    }

    // Shared-secret gate.
    const presented = request.headers.get("X-Internal-Token") ?? "";
    const expected = env.V2_SHARED_TOKEN ?? "";
    if (!expected || presented !== expected) {
      return new Response(
        JSON.stringify({ error: "Forbidden", detail: "Missing or invalid X-Internal-Token" }),
        { status: 403, headers: { "content-type": "application/json" } },
      );
    }

    const userId = match[1]!;
    const remainder = match[2] ?? "/";

    const forwardUrl = new URL(request.url);
    forwardUrl.pathname = remainder;

    const forwardHeaders = new Headers(request.headers);
    forwardHeaders.set("X-User-Id", userId);
    // Phase F.4: Python container needs X-Internal-Token so it can call
    // back to /__state/restore and /__state/flush on this same Worker.
    // The header stays; cloud_persistence.py strips it from its own
    // outbound callbacks as needed. Only internal traffic sees it since
    // the Python /app/server.py never echoes it in responses.
    forwardHeaders.set("X-Internal-Token", expected);

    const stub = env.USER_CONTAINER.get(env.USER_CONTAINER.idFromName(userId));

    // Phase F.4: ensure the container is started with per-instance envVars
    // carrying USER_ID + V2_SHARED_TOKEN so cloud_persistence.py can call
    // back for /__state/restore + /__state/flush. The ensureStarted RPC is
    // idempotent; CF library handles "already running" gracefully.
    try {
      await stub.ensureStarted(userId, expected);
    } catch (err) {
      console.warn(
        JSON.stringify({
          evt: "ensureStarted_failed",
          err: err instanceof Error ? err.message : String(err),
        }),
      );
    }

    const forwardReq = new Request(forwardUrl.toString(), {
      method: request.method,
      headers: forwardHeaders,
      body: request.body,
      // @ts-expect-error — duplex needed for streaming request bodies
      duplex: "half",
    });
    return stub.fetch(forwardReq);
  },
} satisfies ExportedHandler<Env>;

// ── State bucket handlers ──────────────────────────────────────────

// M6 helper — write a single Analytics Engine data point. Wrapped in
// try/catch because AE writes are best-effort: a binding hiccup must
// never break the actual flush/restore.
function logPersistEvent(
  env: Env,
  event: "flush" | "restore",
  userId: string,
  status: "ok" | "not_found" | "error" | "forbidden" | "bad_request",
  byteSize: number,
  httpStatus: number,
): void {
  try {
    if (!env.PERSIST_LOG) return;
    env.PERSIST_LOG.writeDataPoint({
      blobs: [event, userId, status],
      doubles: [byteSize, httpStatus],
      indexes: [userId.slice(0, 32)], // index by user for fast filtering
    });
  } catch {
    // Swallow — telemetry must not break the data path
  }
}

async function handleStateRestore(
  request: Request,
  url: URL,
  env: Env,
): Promise<Response> {
  const presented = request.headers.get("X-Internal-Token") ?? "";
  const expected = env.V2_SHARED_TOKEN ?? "";
  const userId = url.searchParams.get("user_id") ?? "";
  if (!expected || presented !== expected) {
    logPersistEvent(env, "restore", userId, "forbidden", 0, 403);
    return new Response(
      JSON.stringify({ error: "Forbidden" }),
      { status: 403, headers: { "content-type": "application/json" } },
    );
  }
  if (!userId) {
    logPersistEvent(env, "restore", "", "bad_request", 0, 400);
    return new Response(
      JSON.stringify({ error: "user_id required" }),
      { status: 400, headers: { "content-type": "application/json" } },
    );
  }
  const key = `${userId}.tar.gz`;
  const obj = await env.STATE_BUCKET.get(key);
  if (!obj) {
    logPersistEvent(env, "restore", userId, "not_found", 0, 404);
    return new Response(
      JSON.stringify({ error: "not found", user_id: userId }),
      { status: 404, headers: { "content-type": "application/json" } },
    );
  }
  logPersistEvent(env, "restore", userId, "ok", obj.size, 200);
  return new Response(obj.body, {
    status: 200,
    headers: {
      "content-type": "application/gzip",
      "x-r2-size": String(obj.size),
    },
  });
}

async function handleStateFlush(
  request: Request,
  url: URL,
  env: Env,
): Promise<Response> {
  const presented = request.headers.get("X-Internal-Token") ?? "";
  const expected = env.V2_SHARED_TOKEN ?? "";
  const userId = url.searchParams.get("user_id") ?? "";
  if (!expected || presented !== expected) {
    // Phase F.4 diagnostic: log masked tokens so we can diagnose mismatches
    // without leaking secrets.
    const mask = (t: string) => (t.length >= 10 ? `${t.slice(0, 4)}…${t.slice(-4)}(len=${t.length})` : t ? `***(len=${t.length})` : "");
    console.warn(JSON.stringify({
      evt: "state_flush_forbidden",
      presented: mask(presented),
      expected: mask(expected),
      user_id: userId,
    }));
    logPersistEvent(env, "flush", userId, "forbidden", 0, 403);
    return new Response(
      JSON.stringify({
        error: "Forbidden",
        presented_masked: mask(presented),
        expected_masked: mask(expected),
      }),
      { status: 403, headers: { "content-type": "application/json" } },
    );
  }
  if (!userId) {
    logPersistEvent(env, "flush", "", "bad_request", 0, 400);
    return new Response(
      JSON.stringify({ error: "user_id required" }),
      { status: 400, headers: { "content-type": "application/json" } },
    );
  }
  if (!request.body) {
    logPersistEvent(env, "flush", userId, "bad_request", 0, 400);
    return new Response(
      JSON.stringify({ error: "body required" }),
      { status: 400, headers: { "content-type": "application/json" } },
    );
  }
  const key = `${userId}.tar.gz`;
  try {
    const body = await request.arrayBuffer();
    await env.STATE_BUCKET.put(key, body, {
      httpMetadata: { contentType: "application/gzip" },
      customMetadata: {
        uploadedAt: new Date().toISOString(),
        userId,
      },
    });
    logPersistEvent(env, "flush", userId, "ok", body.byteLength, 200);
    return new Response(
      JSON.stringify({ status: "ok", size: body.byteLength, key }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  } catch (err) {
    logPersistEvent(env, "flush", userId, "error", 0, 500);
    return new Response(
      JSON.stringify({
        error: "put_failed",
        detail: err instanceof Error ? err.message : String(err),
      }),
      { status: 500, headers: { "content-type": "application/json" } },
    );
  }
}
