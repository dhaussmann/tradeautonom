-- Migration 0006 — 2026-04-24 — per-user backend selector for V2 rollout.
--
-- Phase F.3 of the V2 Cloudflare migration. Adds a column to the `user`
-- table so the Worker can decide at request time whether to route a user's
-- traffic to V1 (Photon Docker containers via the orchestrator) or V2
-- (per-user Cloudflare Container via USER_V2 service binding).
--
-- Values:
--   'photon'  V1 (default for existing users and all new signups).
--             Routed through NAS_BACKEND → orchestrator → ta-user-<id>.
--   'cf'      V2. Routed through USER_V2 service binding → UserContainer DO.
--
-- The flip is one-way per request: changing the column changes routing
-- starting with the next request. Admin-gated in /api/admin/user/:id/backend.
--
-- References:
--   docs/v2-cf-containers-architecture.md (Phase F.3)
--   deploy/cloudflare/src/index.ts::handleUserApiProxy (routing branch)

ALTER TABLE user ADD COLUMN backend TEXT NOT NULL DEFAULT 'photon';
