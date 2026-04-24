import type { UserContainer } from "./user-container";

export interface Env {
  /**
   * One Container DO instance per user, addressed by `idFromName(userId)`.
   * The user_id is the same primary key used in D1 `user.id` (better-auth).
   */
  USER_CONTAINER: DurableObjectNamespace<UserContainer>;
  /**
   * Shared secret that must be present as the `X-Internal-Token` request
   * header for any /u/<user_id>/... call to succeed. Populated via
   * `wrangler secret put V2_SHARED_TOKEN` on this Worker; the main
   * `tradeautonom` Worker (bot.defitool.de) uses the same value to
   * authenticate service-binding calls. Unset in dev → only `/` works.
   */
  V2_SHARED_TOKEN: string;
}
