/**
 * OMS-v2 PoC — Worker entrypoint.
 *
 * Routes all ExtendedOms endpoints through the singleton DO.
 * Nothing else is wired up. This is intentionally minimal.
 */

import { ExtendedOms } from "./exchanges/extended";

export { ExtendedOms };

interface Env {
  EXTENDED_OMS: DurableObjectNamespace<ExtendedOms>;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    // All state lives inside the ExtendedOms singleton DO.
    if (
      path === "/health" ||
      path === "/markets" ||
      path.startsWith("/book/")
    ) {
      const id = env.EXTENDED_OMS.idFromName("singleton");
      const stub = env.EXTENDED_OMS.get(id);
      return stub.fetch(request);
    }

    if (path === "/" || path === "") {
      return new Response(
        [
          "OMS-v2 Proof of Concept — Extended all-markets shared stream",
          "",
          "Endpoints:",
          "  GET /health          — DO health snapshot",
          "  GET /markets         — list all tracked markets (top-of-book + stats)",
          "  GET /book/{market}   — top-10 orderbook for a specific market",
          "",
          "Data is held in memory only (no persistence). See",
          "  deploy/cf-containers/proof-of-concept/README.md",
        ].join("\n"),
        { headers: { "content-type": "text/plain" } },
      );
    }

    return new Response("not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;
