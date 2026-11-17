# Build from the repository root: docker build -f host/Dockerfile .
FROM node:22-alpine

WORKDIR /app
COPY host/index.html host/host.js host/server.mjs ./
ENV HOST_BIND=0.0.0.0 AGENT_UPSTREAM=http://agent:8003
EXPOSE 5173

HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
  CMD wget -q --spider http://127.0.0.1:5173/healthz || exit 1

CMD ["node", "server.mjs"]
