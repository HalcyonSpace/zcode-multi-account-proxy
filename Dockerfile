# syntax=docker/dockerfile:1

FROM oven/bun:1-alpine AS deps
WORKDIR /app
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

FROM oven/bun:1-alpine
WORKDIR /app

RUN apk add --no-cache curl

COPY --from=deps /app/node_modules ./node_modules
COPY package.json tsconfig.json config.example.yaml ./
COPY src ./src
COPY scripts ./scripts
COPY entrypoint.sh ./entrypoint.sh

RUN bun run scripts/build-fork-worker.ts && rm -rf scripts && chmod +x entrypoint.sh
RUN mkdir -p /data /store && chown -R bun:bun /app /data /store

ENV ZCODE_PROXY_PORT=8080
ENV ZCODE_PROXY_CONFIG=/data/config.yaml
ENV ZCODE_PROXY_STORE_DIR=/store
ENV BUN_JSC_useJIT=0
ENV CAPTCHA_SOLVER_MODE=child
ENV CAPTCHA_CHILD_BATCH=2
ENV CAPTCHA_CHILD_CONCURRENCY=1

EXPOSE 8080
USER bun

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]
