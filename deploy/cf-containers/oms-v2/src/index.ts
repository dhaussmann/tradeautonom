/**
 * V2 OMS Worker entrypoint.
 *
 * Routes:
 *   GET /                             hint page
 *   GET /health                       aggregate health (DO + exchanges)
 *   GET /tracked                      cross-exchange base-token mapping
 *   GET /status                       per-feed age/updates/connected
 *   GET /book/{exch}/{sym}            current top-10 orderbook
 *   GET /markets                      all Extended markets (direct from ExtendedOms)
 *   GET /ws                           V1-compatible bot subscriber WebSocket
 *   GET /ext/*                        direct passthrough to ExtendedOms (debugging)
 *
 * All stateful routes forward to the relevant Durable Object.
 */

import { ExtendedOms } from "./exchanges/extended";
import { AggregatorDO } from "./aggregator";
import type { Env } from "./types";

export { ExtendedOms, AggregatorDO };

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    // Aggregator-handled routes
    if (
      path === "/ws" ||
      path === "/health" ||
      path === "/tracked" ||
      path === "/status" ||
      path.startsWith("/book/")
    ) {
      const stub = env.AGGREGATOR_DO.get(env.AGGREGATOR_DO.idFromName("aggregator"));
      return stub.fetch(request);
    }

    // Direct passthrough to ExtendedOms (debugging / legacy PoC behavior)
    if (path === "/markets" || path === "/ext/health" || path.startsWith("/ext/")) {
      const stub = env.EXTENDED_OMS.get(env.EXTENDED_OMS.idFromName("singleton"));
      // Strip /ext prefix so ExtendedOms.fetch sees its own paths
      const forwardPath = path.startsWith("/ext/")
        ? path.replace("/ext", "")
        : path;
      const forwardUrl = new URL(request.url);
      forwardUrl.pathname = forwardPath;
      const forwarded = new Request(forwardUrl.toString(), request);
      return stub.fetch(forwarded);
    }

    if (path === "/" || path === "") {
      return new Response(
        [
          "TradeAutonom V2 OMS",
          "",
          "Bot-client endpoints (V1-compatible):",
          "  GET /ws                    WebSocket subscribe/unsubscribe + book push",
          "  GET /book/{exch}/{sym}     current orderbook snapshot",
          "  GET /tracked               cross-exchange token mapping",
          "  GET /status                per-feed health and freshness",
          "  GET /health                aggregate health",
          "",
          "Direct-DO debugging:",
          "  GET /markets               all tracked Extended markets",
          "  GET /ext/health            ExtendedOms health",
          "  GET /ext/book/{market}     ExtendedOms direct book",
          "",
          "Docs: docs/v2-oms-cloudflare-native.md",
        ].join("\n"),
        { headers: { "content-type": "text/plain" } },
      );
    }

    return new Response("not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;
