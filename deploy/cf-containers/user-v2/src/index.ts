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
 * binding. Direct calls from the public internet to
 * user-v2.defitool.de/u/... will return 403 unless the caller also knows
 * the secret — useful for ad-hoc diagnostics from an admin machine.
 */

import { UserContainer } from "./user-container";
import type { Env } from "./types";

export { UserContainer };

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // Root: friendly banner, always reachable. Useful for quick "is
    // the Worker live?" checks without needing the shared token.
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

    // Admin recycle endpoint: destroys the user's Container DO instance so
    // the next request cold-starts with fresh envVars. Protected by the
    // same shared-secret as normal calls.
    //
    // Need a recycle mechanism because `wrangler deploy` does NOT auto-
    // restart warm container instances when only envVars (static Worker
    // config) changes — it just marks the new config for the next cold-
    // start. This endpoint explicitly stops + restarts the container.
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
        // Proxy a special /__recycle path into the DO itself. Handled
        // inside UserContainer.fetch() below.
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

    // Shared-secret gate. The main Worker adds this header when forwarding
    // a request via the USER_V2 service binding. Anything else must present
    // the same token to proceed (covers ad-hoc diagnostics).
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

    // Rebuild the request URL stripping the /u/<id> prefix so the Python
    // app sees its native paths. Also pass X-User-Id so the container can
    // scope persistence / logging per user.
    const forwardUrl = new URL(request.url);
    forwardUrl.pathname = remainder;

    const forwardHeaders = new Headers(request.headers);
    forwardHeaders.set("X-User-Id", userId);
    // Don't leak the shared token to the Python app.
    forwardHeaders.delete("X-Internal-Token");

    const stub = env.USER_CONTAINER.get(env.USER_CONTAINER.idFromName(userId));
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
