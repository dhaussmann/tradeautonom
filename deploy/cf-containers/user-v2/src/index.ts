/**
 * UserContainer-v2 Worker entrypoint.
 *
 * For Phase F.1 smoke-testing this Worker is standalone: it accepts a
 * `user-v2.defitool.de` request, extracts the user id from a URL path prefix
 * (`/u/<user_id>/...`), and forwards the rest of the request to that user's
 * Container DO. Example:
 *   https://user-v2.defitool.de/u/testuser-1/health
 *     → UserContainer DO with idFromName("testuser-1"), path "/health"
 *
 * This is deliberately a separate Worker zone (not bot.defitool.de) so we
 * can smoke-test without touching V1 routing. Phase F.3 integrates this
 * into `deploy/cloudflare/src/index.ts` so the real session-cookie path
 * (`bot.defitool.de/api/...`) routes to UserContainer for users with
 * `backend='cf'`.
 *
 * Security note: this standalone Worker has NO authentication. Anyone who
 * knows a user_id can hit their container. That's acceptable for F.1/F.2
 * smoke-testing with a throwaway test user; before F.3 or any real user
 * data, either delete this Worker or add token gating.
 */

import { UserContainer } from "./user-container";
import type { Env } from "./types";

export { UserContainer };

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\/u\/([A-Za-z0-9._-]+)(\/.*)?$/);
    if (!match) {
      return new Response(
        [
          "TradeAutonom UserContainer-v2 (Phase F.1 smoke-test Worker)",
          "",
          "Route requests as: GET /u/<user_id>/<container_path>",
          "Example:          GET /u/testuser-1/health",
          "",
          "Each <user_id> gets its own Container DO via idFromName(user_id).",
        ].join("\n"),
        { headers: { "content-type": "text/plain" } },
      );
    }
    const userId = match[1]!;
    const remainder = match[2] ?? "/";

    // Forward to the user's container. Rebuild the URL without the /u/<id>
    // prefix so the Python app sees its native paths.
    const forwardUrl = new URL(request.url);
    forwardUrl.pathname = remainder;

    const stub = env.USER_CONTAINER.get(env.USER_CONTAINER.idFromName(userId));
    const forwardReq = new Request(forwardUrl.toString(), request);
    return stub.fetch(forwardReq);
  },
} satisfies ExportedHandler<Env>;
