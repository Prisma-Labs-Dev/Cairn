// Common benchmark instrumentation. No prompt, header or request-payload changes.
import { appendFileSync } from "node:fs";
import { spawnSync } from "node:child_process";

let nativeFetch = globalThis.fetch;
const log = (record: unknown) => appendFileSync(process.env.WAYLINE_BENCH_METADATA!, JSON.stringify(record) + "\n");
const observedFetch: typeof fetch = async (input, init) => {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const parsedUrl = new URL(url);
  const endpoint = parsedUrl.origin + parsedUrl.pathname;
  const method = String(init?.method ?? (typeof input === "object" && "method" in input ? input.method : "GET")).toUpperCase();
  const watched = endpoint === "https://chatgpt.com/backend-api/codex/responses" && method === "POST";
  const oauth = endpoint === "https://auth.openai.com/oauth/token" && method === "POST";
  const allowedOrigins: string[] = JSON.parse(process.env.WAYLINE_BENCH_ALLOWED_ORIGINS ?? "[]");
  const fixture = method === "GET" && parsedUrl.protocol === "http:" && parsedUrl.hostname === "127.0.0.1" && allowedOrigins.includes(parsedUrl.origin);
  if ((!watched && !oauth && !fixture) || parsedUrl.search || parsedUrl.username || parsedUrl.password) {
    log({ type: "endpoint_refused", endpoint, method });
    throw new Error("benchmark refuses unsupported provider endpoint");
  }
  if (watched) {
    const body = init?.body ?? (typeof input === "object" && "clone" in input ? await input.clone().text() : undefined);
    const payload = JSON.parse(String(body));
    if (payload.model !== "gpt-6-luna" || payload.reasoning?.effort !== "low") {
      log({ type: "routing_refused", model: payload.model, reasoning: payload.reasoning });
      throw new Error("benchmark refuses non-Luna or non-low request");
    }
    const gate = spawnSync(process.env.WAYLINE_BENCH_PYTHON!, [process.env.WAYLINE_BENCH_TRANSPORT!, "reserve"], { encoding: "utf8", timeout: 10000 });
    if (gate.status !== 0) {
      log({ type: "budget_refused", status: gate.status });
      throw new Error("benchmark provider-request budget is stopped");
    }
    log({ type: "request_metadata", request_id: gate.stdout.trim(), model: payload.model, reasoning: payload.reasoning, endpoint: new URL(url).origin + new URL(url).pathname });
  }
  const response = await nativeFetch(input, init);
  if (watched) {
    log({ type: "http_metadata", status: response.status });
    const clone = response.clone();
    void (async () => {
      if (!clone.body) return;
      const reader = clone.body.getReader();
      const decoder = new TextDecoder();
      let buffered = "", bytes = 0;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        bytes += value.byteLength;
        if (bytes > 4194304) { await reader.cancel(); log({ type: "observer_limit" }); break; }
        buffered += decoder.decode(value, { stream: true });
        let newline: number;
        while ((newline = buffered.indexOf("\n")) >= 0) {
          const line = buffered.slice(0, newline).trim();
          buffered = buffered.slice(newline + 1);
          if (!line.startsWith("data:")) continue;
          try {
            const event = JSON.parse(line.slice(5));
            if (event.response && ["response.created", "response.completed", "response.done", "response.incomplete"].includes(event.type)) {
              const r = event.response;
              log({ type: "server_response_metadata", event: event.type, id: r.id, model: r.model, status: r.status, usage: r.usage, service_tier: r.service_tier });
            }
          } catch { /* Other SSE lines have no accounting metadata. */ }
        }
      }
    })().catch(error => log({ type: "observer_error", message: String(error) }));
  }
  return response;
};

// Pi calls undici.install() during startup. Keep observing after assignment.
Object.defineProperty(globalThis, "fetch", {
  configurable: false,
  enumerable: true,
  get: () => observedFetch,
  set: (replacement: typeof fetch) => {
    if (replacement !== observedFetch) nativeFetch = replacement;
  },
});

// A misconfigured automatic/WebSocket transport must stop before any network call.
class RefusedWebSocket {
  constructor() {
    log({ type: "websocket_transport_refused" });
    process.exit(125);
  }
}
Object.defineProperty(globalThis, "WebSocket", {
  configurable: false,
  enumerable: true,
  get: () => RefusedWebSocket,
  set: () => { /* undici.install cannot replace the refusal guard. */ },
});
