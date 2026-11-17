// Chat host server (port 5173). Node 18+, no dependencies, no build step.
//
// - Serves the static host files from this directory.
// - Proxies POST /chat and POST /ui/tool to the agent (AGENT_UPSTREAM), so the browser only ever
//   talks to this origin: no hard-coded agent URL, no CORS, works behind any hostname or HTTPS
//   reverse proxy. Nothing else is proxied (the agent /logs endpoint stays private).
// - GET /config.js tells the page where the agent is. Empty (same origin) unless PUBLIC_AGENT_URL
//   is set to talk to an agent directly.
// - Optional HTTP Basic auth for showcases (DEMO_USER and DEMO_PASSWORD); /healthz is always open.
import { createServer, request as httpRequest } from "node:http";
import { readFile } from "node:fs/promises";
import { timingSafeEqual } from "node:crypto";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));
const port = Number(process.env.HOST_PORT || 5173);
const bind = process.env.HOST_BIND || "127.0.0.1";
const upstream = new URL(process.env.AGENT_UPSTREAM || "http://127.0.0.1:8003");
const publicAgentUrl = process.env.PUBLIC_AGENT_URL || "";
const demoUser = process.env.DEMO_USER || "demo";
const demoPassword = process.env.DEMO_PASSWORD || "";
const proxied = new Set(["/chat", "/ui/tool"]);
const types = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
};

function safeEqual(a, b) {
  const left = Buffer.from(a);
  const right = Buffer.from(b);
  return left.length === right.length && timingSafeEqual(left, right);
}

function authorized(req) {
  if (!demoPassword) return true;
  const header = req.headers.authorization || "";
  if (!header.startsWith("Basic ")) return false;
  const decoded = Buffer.from(header.slice(6), "base64").toString("utf8");
  return safeEqual(decoded, `${demoUser}:${demoPassword}`);
}

function proxy(req, res) {
  const forward = httpRequest(
    {
      hostname: upstream.hostname,
      port: upstream.port || 80,
      path: req.url,
      method: req.method,
      headers: { "content-type": req.headers["content-type"] || "application/json" },
      timeout: 300000,
    },
    (answer) => {
      res.writeHead(answer.statusCode || 502, { "Content-Type": answer.headers["content-type"] || "application/json" });
      answer.pipe(res);
    }
  );
  forward.on("timeout", () => forward.destroy(new Error("agent timed out")));
  forward.on("error", () => {
    if (!res.headersSent) res.writeHead(502, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ detail: "The agent is not reachable." }));
  });
  req.pipe(forward);
}

createServer(async (req, res) => {
  const path = (req.url || "/").split("?")[0];
  if (path === "/healthz") {
    res.writeHead(200, { "Content-Type": "text/plain" }).end("ok");
    return;
  }
  if (!authorized(req)) {
    res.writeHead(401, { "WWW-Authenticate": 'Basic realm="Agentic AI Bank demo"' }).end("authentication required");
    return;
  }
  if (path === "/config.js") {
    res.writeHead(200, { "Content-Type": types[".js"], "Cache-Control": "no-store" });
    res.end(`window.AGENT_URL = ${JSON.stringify(publicAgentUrl)};\n`);
    return;
  }
  if (req.method === "POST" && proxied.has(path)) {
    proxy(req, res);
    return;
  }
  const file = normalize(join(root, path === "/" ? "/index.html" : path));
  if (!file.startsWith(root)) {
    res.writeHead(403).end("forbidden");
    return;
  }
  try {
    const body = await readFile(file);
    res.writeHead(200, { "Content-Type": types[extname(file)] || "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404).end("not found");
  }
}).listen(port, bind, () => {
  const auth = demoPassword ? "with password" : "no password";
  console.log(`chat host on http://localhost:${port} (${auth}); agent upstream ${upstream.origin}`);
});
