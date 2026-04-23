/**
 * OMS-v2 PoC — Worker entrypoint.
 *
 * Routes all /book/* and /health requests to the ExtendedOms singleton DO.
 * Nothing else is wired up. This is intentionally minimal.
 */

import { ExtendedOms } from "./exchanges/extended";

export { ExtendedOms };

interface Env {
  EXTENDED_OMS: DurableObjectNamespace<ExtendedOms>;
  EXTENDED_API_KEY: string;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // Any route handled by the ExtendedOms DO goes through the singleton instance.
    if (url.pathname === "/health" || url.pathname.startsWith("/book/")) {
      const id = env.EXTENDED_OMS.idFromName("singleton");
      const stub = env.EXTENDED_OMS.get(id);
      return stub.fetch(request);
    }

    // Root: give a tiny hint.
    if (url.pathname === "/" || url.pathname === "") {
      return new Response(
        [
          "OMS-v2 Proof of Concept",
          "",
          "Endpoints:",
          "  GET /health         — DO health snapshot",
          "  GET /book/BTC-USD   — top-20 orderbook for BTC-USD",
          "",
          "See deploy/cf-containers/proof-of-concept/README.md",
        ].join("\n"),
        { headers: { "content-type": "text/plain" } },
      );
    }

    return new Response("not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;
