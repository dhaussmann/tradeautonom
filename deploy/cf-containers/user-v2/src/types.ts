import type { UserContainer } from "./user-container";

export interface Env {
  /**
   * One Container DO instance per user, addressed by `idFromName(userId)`.
   * The user_id is the same primary key used in D1 `user.id` (better-auth).
   */
  USER_CONTAINER: DurableObjectNamespace<UserContainer>;
}
