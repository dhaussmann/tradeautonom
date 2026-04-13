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
  });
}

export type Auth = ReturnType<typeof createAuth>;
