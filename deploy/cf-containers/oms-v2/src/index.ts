/**
 * V2 OMS Worker entrypoint.
 *
 * Routes:
 *   GET /                             hint page
 *   GET /health                       aggregate health
 *   GET /tracked                      cross-exchange base-token mapping
 *   GET /status                       per-feed age/updates/connected
 *   GET /book/{exch}/{sym}            current top-10 orderbook
 *   GET /discovery                    auto-discovery stats
 *   GET /discovery/run                force a discovery run (same as cron)
 *   GET /markets                      all Extended markets (debug)
 *   GET /{ext,grvt,nado,variational}/health   per-exchange DO health
 *   GET /ws                           V1-compatible bot subscriber WebSocket
 *
 * Scheduled:
 *   Every 15 min → AggregatorDO.runDiscoveryAndPropagate()
 *     Rebuilds base-token mapping, pushes fresh symbol lists to each
 *     ExchangeOms so GRVT/Nado/Variational track the right markets.
 */

import { ExtendedOms } from "./exchanges/extended";
import { GrvtOms } from "./exchanges/grvt";
import { NadoOms } from "./exchanges/nado";
import { VariationalOms } from "./exchanges/variational";
import { AggregatorDO } from "./aggregator";
import type { Env } from "./types";

export { ExtendedOms, GrvtOms, NadoOms, VariationalOms, AggregatorDO };

type ExchangeBindingKey = "extended" | "grvt" | "nado" | "variational";
function exchangeStub(env: Env, key: ExchangeBindingKey): DurableObjectStub {
  const ns =
    key === "extended" ? env.EXTENDED_OMS
    : key === "grvt" ? env.GRVT_OMS
    : key === "nado" ? env.NADO_OMS
    : env.VARIATIONAL_OMS;
  return ns.get(ns.idFromName("singleton"));
}

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
      path === "/discovery" ||
      path === "/discovery/run" ||
      path.startsWith("/book/")
    ) {
      const stub = env.AGGREGATOR_DO.get(env.AGGREGATOR_DO.idFromName("aggregator"));
      return stub.fetch(request);
    }

    // Per-exchange DO passthroughs for debugging
    const exMatch = path.match(/^\/(ext|grvt|nado|variational)(\/.*)?$/);
    if (exMatch) {
      const prefix = exMatch[1]!;
      const remain = exMatch[2] || "/health";
      const exKey: ExchangeBindingKey =
        prefix === "ext" ? "extended" : (prefix as ExchangeBindingKey);
      const forwardUrl = new URL(request.url);
      forwardUrl.pathname = remain;
      const forwarded = new Request(forwardUrl.toString(), request);
      return exchangeStub(env, exKey).fetch(forwarded);
    }

    // Extended-markets passthrough (legacy path from Phase A)
    if (path === "/markets") {
      const forwardUrl = new URL(request.url);
      forwardUrl.pathname = "/markets";
      const forwarded = new Request(forwardUrl.toString(), request);
      return exchangeStub(env, "extended").fetch(forwarded);
    }

    if (path === "/" || path === "") {
      return new Response(
        [
          "TradeAutonom V2 OMS",
          "",
          "Bot-client endpoints (V1-compatible):",
          "  GET /ws                        WebSocket: {action:'subscribe',exchange,symbol}",
          "  GET /book/{exch}/{sym}         current orderbook snapshot",
          "  GET /tracked                   cross-exchange token mapping",
          "  GET /status                    per-feed health and freshness",
          "  GET /health                    aggregate health",
          "",
          "Auto-discovery:",
          "  GET /discovery                 last-run stats",
          "  GET /discovery/run             force a fresh run",
          "",
          "Per-exchange debugging:",
          "  GET /ext/health                ExtendedOms health",
          "  GET /grvt/health               GrvtOms health",
          "  GET /nado/health               NadoOms health",
          "  GET /variational/health        VariationalOms health",
          "",
          "Supported exchanges: extended, grvt, nado, variational",
          "",
          "Docs: docs/v2-oms-cloudflare-native.md",
        ].join("\n"),
        { headers: { "content-type": "text/plain" } },
      );
    }

    return new Response("not found", { status: 404 });
  },

  async scheduled(_controller: ScheduledController, env: Env): Promise<void> {
    const stub = env.AGGREGATOR_DO.get(env.AGGREGATOR_DO.idFromName("aggregator"));
    // Trigger discovery; AggregatorDO will push symbol lists to each ExchangeOms.
    await (stub as any).runDiscoveryAndPropagate();
  },
} satisfies ExportedHandler<Env>;
