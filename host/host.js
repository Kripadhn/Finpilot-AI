/* Thin host logic: /chat loop + MCP Apps host bridge (spec 2026-01-26).
   Views run in sandboxed iframes (allow-scripts only). The host answers ui/initialize,
   pushes ui/notifications/tool-result, resizes on ui/notifications/size-changed, and
   forwards tools/call to the agent's /ui/tool endpoint — the only declared network path. */
"use strict";

// Empty means same origin: server.mjs proxies /chat and /ui/tool to the agent.
const AGENT_URL = window.AGENT_URL || "";
const chat = document.getElementById("chat");
const form = document.getElementById("form");
const input = document.getElementById("input");
const send = document.getElementById("send");
const tokenSelect = document.getElementById("token");

let conversationId = null;
// iframe contentWindow -> { entry, iframe, initialized }
const frames = new Map();

document.getElementById("reset").addEventListener("click", () => {
  conversationId = null;
  chat.innerHTML = "";
  frames.clear();
});
tokenSelect.addEventListener("change", () => {
  conversationId = null; // a conversation never crosses identities
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  addMessage("user", message);
  send.disabled = true;
  try {
    const response = await fetch(`${AGENT_URL}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_token: tokenSelect.value,
        conversation_id: conversationId,
        message,
      }),
    });
    if (!response.ok) throw new Error(`agent returned ${response.status}`);
    const data = await response.json();
    conversationId = data.conversation_id || conversationId;
    const meta = [];
    if (data.usage && data.usage.model) meta.push(data.usage.model);
    if (data.usage && data.usage.latency_ms != null) meta.push(`${data.usage.latency_ms} ms`);
    if (data.trace_id) meta.push(`trace ${data.trace_id.slice(0, 12)}…`);
    addMessage("bot", data.text || "(no text)", meta.join(" · "));
    for (const entry of data.ui_resources || []) {
      if (entry.html) mountView(entry);
    }
  } catch (error) {
    addMessage("bot error", `Could not reach the agent: ${error.message}`);
  } finally {
    send.disabled = false;
    input.focus();
  }
});

function addMessage(cls, text, meta) {
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  div.textContent = text;
  if (meta) {
    const small = document.createElement("div");
    small.className = "meta";
    small.textContent = meta;
    div.appendChild(small);
  }
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function mountView(entry) {
  const iframe = document.createElement("iframe");
  iframe.className = "view-frame";
  iframe.setAttribute("sandbox", "allow-scripts");
  iframe.setAttribute("title", entry.uri);
  iframe.srcdoc = entry.html;
  chat.appendChild(iframe);
  chat.scrollTop = chat.scrollHeight;
  iframe.addEventListener("load", () => {
    frames.set(iframe.contentWindow, { entry, iframe, initialized: false });
    // Push the result even if the view skipped/raced the initialize handshake.
    setTimeout(() => pushToolResult(iframe.contentWindow), 300);
  });
}

function pushToolResult(view) {
  const state = frames.get(view);
  if (!state || state.pushed) return;
  state.pushed = true;
  view.postMessage(
    {
      jsonrpc: "2.0",
      method: "ui/notifications/tool-result",
      params: state.entry.result,
    },
    "*"
  );
}

window.addEventListener("message", async (event) => {
  const state = frames.get(event.source);
  const msg = event.data;
  if (!state || !msg || msg.jsonrpc !== "2.0") return;

  if (msg.method === "ui/initialize" && msg.id !== undefined) {
    event.source.postMessage(
      {
        jsonrpc: "2.0",
        id: msg.id,
        result: {
          protocolVersion: "2026-01-26",
          hostInfo: { name: "dvt-thin-host", version: "1.0.0" },
          hostCapabilities: { tools: {} },
        },
      },
      "*"
    );
    return;
  }
  if (msg.method === "ui/notifications/initialized") {
    state.initialized = true;
    pushToolResult(event.source);
    return;
  }
  if (msg.method === "ui/notifications/size-changed") {
    const height = Number(msg.params && msg.params.height);
    if (height > 0) state.iframe.style.height = `${Math.min(height, 900)}px`;
    return;
  }
  if (msg.method === "tools/call" && msg.id !== undefined) {
    try {
      const response = await fetch(`${AGENT_URL}/ui/tool`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_token: tokenSelect.value,
          name: msg.params && msg.params.name,
          arguments: (msg.params && msg.params.arguments) || {},
        }),
      });
      const result = await response.json();
      event.source.postMessage({ jsonrpc: "2.0", id: msg.id, result }, "*");
    } catch (error) {
      event.source.postMessage(
        { jsonrpc: "2.0", id: msg.id, error: { code: -32000, message: String(error) } },
        "*"
      );
    }
  }
});
