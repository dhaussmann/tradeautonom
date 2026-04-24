/**
 * Phase F.0 feasibility PoC Worker — transparently forwards all requests to
 * the singleton UserV2PocContainer. If F.0 passes, F.1 will add per-user DO
 * sharding via `idFromName(user_id)`.
 */

import { UserV2PocContainer } from "./user-v2-poc-container";
import type { Env } from "./types";

export { UserV2PocContainer };

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const stub = env.USER_V2_POC.get(env.USER_V2_POC.idFromName("singleton"));
    return stub.fetch(request);
  },
} satisfies ExportedHandler<Env>;
