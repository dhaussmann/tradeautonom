# Nado Relay Container
#
# Purpose: bridge Cloudflare Worker → Nado upstream WebSocket.
#
# The Nado subscription gateway (wss://gateway.prod.nado.xyz/v1/subscribe)
# requires `Sec-WebSocket-Extensions: permessage-deflate`. Cloudflare Workers'
# outbound WebSocket client does NOT negotiate extensions and is rejected
# with HTTP 403 "Invalid compression headers".
#
# This container runs Node.js with the `ws` library which handles deflate
# transparently. NadoOms DO holds ONE persistent WebSocket to this container;
# the container holds ONE persistent WebSocket to Nado. Events are forwarded
# as parsed JSON (no book state in the container — that stays in NadoOms DO).
#
# See docs/v2-oms-cloudflare-native.md for the full rationale.

# ── Build stage ─────────────────────────────────────────────────────
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
# Full install (including devDeps) so we can run tsc.
RUN npm ci --ignore-scripts || npm install --ignore-scripts
COPY tsconfig.json ./
COPY src ./src
RUN npx tsc --project tsconfig.json

# ── Runtime stage ───────────────────────────────────────────────────
FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production

# Only production deps in final image.
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev --ignore-scripts || npm install --omit=dev --ignore-scripts
COPY --from=build /app/dist ./dist

# Relay listens on 8080 for the Worker-side WebSocket.
EXPOSE 8080

# Simple healthcheck: HTTP GET /health on localhost.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -q -O - http://localhost:8080/health || exit 1

CMD ["node", "dist/index.js"]
