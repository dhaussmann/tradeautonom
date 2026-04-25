/**
 * better-auth instance for Cloudflare Worker + D1.
 *
 * Handles user registration, login, sessions via /api/auth/* routes.
 * Uses the shared D1 database (same as history tables).
 */

import { betterAuth } from "better-auth";
import { D1Dialect } from "kysely-d1";

export function createAuth(db: D1Database, secret: string, baseURL: string) {
  return betterAuth({
    database: {
      dialect: new D1Dialect({ database: db }),
      type: "sqlite",
    },
    secret,
    baseURL,
    basePath: "/api/auth",
    emailAndPassword: {
      enabled: true,
      minPasswordLength: 8,
    },
    session: {
      expiresIn: 60 * 60 * 24 * 30, // 30 days
      updateAge: 60 * 60 * 24, // refresh every 24h
      cookieCache: {
        enabled: true,
        maxAge: 60 * 5, // 5 min cache
      },
    },
    trustedOrigins: ["*"],
    /**
     * Phase F.4 M8 — Auto-V2 for new signups.
     *
     * After a user row is inserted, flip `backend` from the schema default
     * 'photon' (V1 / Photon Docker) to 'cf' (V2 / Cloudflare Container) so
     * the very first request from the new user is routed to USER_V2.
     *
     * Existing users are unaffected — the hook only fires on `create`,
     * never on `update`. To migrate an existing 'photon' user to 'cf',
     * use the admin endpoint `POST /api/admin/migrate-to-cf/:id`.
     *
     * Failures are swallowed: if the UPDATE fails, the user keeps the
     * default 'photon' backend and an admin can migrate them manually.
     * We never want a hook bug to break signup.
     *
     * Reference: docs/v2-cf-containers-architecture.md (Phase F.4)
     */
    databaseHooks: {
      user: {
        create: {
          async after(user: { id: string }) {
            try {
              await db
                .prepare(
                  "UPDATE user SET backend = ?, updatedAt = ? WHERE id = ?",
                )
                .bind("cf", new Date().toISOString(), user.id)
                .run();
              console.log(
                `[m8-auto-v2] new user ${user.id} routed to backend='cf'`,
              );
            } catch (err) {
              console.error(
                `[m8-auto-v2] failed to set backend='cf' for user ${user.id}:`,
                err,
              );
            }
          },
        },
      },
    },
  });
}

export type Auth = ReturnType<typeof createAuth>;
