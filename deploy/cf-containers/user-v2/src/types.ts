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
   * authenticate service-binding calls.
   */
  V2_SHARED_TOKEN: string;
  /**
   * Phase F.4: R2 bucket holding per-user state tarballs. Key layout:
   *   <user_id>.tar.gz
   * The tarball is a gzipped snapshot of /app/data/ from the container
   * (auth.json, secrets.enc, bots/<sym>/{config,position,timer}.json, etc.)
   * Restored on container cold-start by cloud_persistence.py via the
   * /__state/restore GET endpoint, uploaded every 30s + on SIGTERM via
   * /__state/flush POST.
   */
  STATE_BUCKET: R2Bucket;
}
