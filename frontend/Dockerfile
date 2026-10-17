# SecRAG frontend (Next.js, standalone). Build context: ./frontend
# Base pinned by digest (node:22-slim = Node 22.23.3; multi-arch index digest resolved
# 2026-09-27); bump deliberately: docker buildx imagetools inspect node:22-slim.
ARG NODE_IMAGE=node:22-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c

FROM ${NODE_IMAGE} AS builder
WORKDIR /app
# Same npm as WSL and the gate (package.json engines: npm >= 11).
RUN npm install -g --no-fund --no-audit npm@11.20.0
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# No build-time backend URL any more (T12.1.7): the UI calls relative /api/... paths only,
# proxied by the BFF route handlers using the server-side BACKEND_URL (set at run time).
RUN npm run build

FROM ${NODE_IMAGE} AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
EXPOSE 3000
CMD ["node", "server.js"]
