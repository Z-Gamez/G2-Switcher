"""Local Anthropic-API relay.

Even Terminal's Claude Code is pointed at this relay (ANTHROPIC_BASE_URL). Each
request is forwarded to whichever provider is currently selected, so switching
providers takes effect on the next request with no restart.

  claude      -> api.anthropic.com, auth swapped for the Anthropic API key
  claude_sub  -> api.anthropic.com, auth swapped for a `claude setup-token` subscription token
  openrouter  -> openrouter.ai/api, auth swapped for the OpenRouter key
  ollama      -> local Ollama's Anthropic-compatible /v1/messages

Claude Code itself only holds a placeholder token (PLACEHOLDER_TOKEN), so it
never needs a Claude login; the real keys live here and are entered in the GUI.
"""

import http.client
import http.server
import json
import re
import socket
import ssl
import threading
import time
import urllib.parse

import activity

PROVIDERS = {
    "claude": "https://api.anthropic.com",
    "claude_sub": "https://api.anthropic.com",
    "openrouter": "https://openrouter.ai/api",
    "ollama": "http://127.0.0.1:11434",
}

# Claude Code's system prompt + tools alone is ~25-40k tokens, so local models need a
# bigger window than Ollama's default. The user picks one of these in the GUI.
OLLAMA_CTX_PRESETS = (65536, 98304, 131072, 196608, 262144)
OLLAMA_DEFAULT_CTX = 65536

# Given to Claude Code as ANTHROPIC_AUTH_TOKEN; replaced by the relay before forwarding.
PLACEHOLDER_TOKEN = "g2-switcher-relay"
AUTH_HEADERS = ("authorization", "x-api-key")
OAUTH_BETA = "oauth-2025-04-20"
SHOW_PATH = "/g2-switcher/show"

# Claude Code's own compaction request (it summarises the conversation so far).
COMPACT_MARKER = "create a detailed summary of the conversation so far"


def is_compaction(body):
    msgs = body.get("messages") or []
    return bool(msgs) and COMPACT_MARKER in json.dumps(msgs[-1].get("content"))


def compact_trigger(ctx):
    """Token count at which to ask Claude Code to compact: leave room for the
    compaction request itself (instructions + summary output) to still fit."""
    return int(ctx - max(12000, ctx * 0.15))


def estimate(body):
    return int(len(json.dumps(body)) / 3.7)  # measured ~4.1 chars/token; 3.7 errs on the safe side


def _cut(text, keep_head=800, keep_tail=400):
    if len(text) <= keep_head + keep_tail + 200:
        return text
    return (f"{text[:keep_head]}\n[... {len(text) - keep_head - keep_tail} characters cut by G2 Switcher "
            f"so the summary fits the model's context ...]\n{text[-keep_tail:]}")


def fit_compaction(body, budget):
    """Shrink Claude Code's compaction request until it fits `budget` tokens, cutting
    the bulkiest old content first: tool outputs, then long text, then whole messages.
    Returns how many items were cut (0 if it already fit)."""
    msgs = body.get("messages") or []
    if estimate(body) <= budget or len(msgs) < 2:
        return 0
    cut = 0
    for kinds in (("tool_result",), ("text",)):
        for msg in msgs:
            if not isinstance(msg.get("content"), list):
                if ("text" in kinds and isinstance(msg.get("content"), str) and len(msg["content"]) > 1400
                        and COMPACT_MARKER not in msg["content"]):
                    msg["content"], cut = _cut(msg["content"]), cut + 1
                continue
            for block in msg["content"]:
                # Never cut the summarisation instructions themselves (the relay may have merged
                # them into the same message as a big tool output).
                if block.get("type") not in kinds or COMPACT_MARKER in block.get("text", ""):
                    continue
                if block["type"] == "tool_result":
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content)
                    if len(text) > 1400:
                        block["content"], cut = _cut(text), cut + 1
                elif len(block.get("text", "")) > 1400:
                    block["text"], cut = _cut(block["text"]), cut + 1
                if estimate(body) <= budget:
                    return cut
    # Still too big: drop the oldest messages, keeping the first user turn for context.
    while estimate(body) > budget and len(msgs) > 3:
        del msgs[1]
        cut += 1
    # Dropping messages can orphan tool calls/results; turn those into plain text.
    ids_used = {b.get("id") for m in msgs if isinstance(m.get("content"), list) for b in m["content"]
                if b.get("type") == "tool_use"}
    ids_answered = {b.get("tool_use_id") for m in msgs if isinstance(m.get("content"), list)
                    for b in m["content"] if b.get("type") == "tool_result"}
    for m in msgs:
        if isinstance(m.get("content"), list):
            m["content"] = [
                {"type": "text", "text": f"[tool result] {_cut(json.dumps(b.get('content')))}"}
                if b.get("type") == "tool_result" and b.get("tool_use_id") not in ids_used else
                {"type": "text", "text": f"[called {b.get('name')}]"}
                if b.get("type") == "tool_use" and b.get("id") not in ids_answered else b
                for b in m["content"]]
    if msgs and msgs[0].get("role") != "user":
        msgs.insert(0, {"role": "user", "content": "[earlier conversation omitted to fit the context window]"})
    return cut
CLAUDE_PROVIDERS = ("claude", "claude_sub")
KEY_LABELS = {"claude": "Anthropic API key", "claude_sub": "Claude subscription token",
              "openrouter": "OpenRouter API key"}

HOP_HEADERS = {"host", "connection", "keep-alive", "transfer-encoding",
               "content-length", "accept-encoding", "proxy-connection", "upgrade", "te"}


class State:
    """Shared, thread-safe routing state. The GUI writes it, the relay reads it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.provider = "claude"
        self.models = {}            # provider -> {"main": str, "fast": str}
        self.keys = {p: "" for p in KEY_LABELS}
        self.log = lambda msg: None
        self.on_show = lambda: None  # a second G2 Switcher launch asks this one to show itself
        self.on_ai = lambda event: None  # activity.Call events: what the model is doing
        # Claude Code assumes Claude's huge context window, so for providers with a smaller
        # one the relay asks it to compact before the real limit (see _maybe_compact).
        self.auto_compact = True
        self.contexts = {}  # OpenRouter model id -> context length
        self.just_compacted = False  # let the first request after a compaction through (no loops)
        self.ollama_ctx = OLLAMA_DEFAULT_CTX

    def snapshot(self):
        with self.lock:
            return self.provider, dict(self.models.get(self.provider, {})), self.keys.get(self.provider, "")


def _strip_unsigned_thinking(body, strip_all=False):
    """Anthropic rejects thinking blocks it didn't sign (e.g. produced by another
    provider before a switch). Drop them from history."""
    changed = False
    for msg in body.get("messages", []):
        content = msg.get("content")
        if msg.get("role") != "assistant" or not isinstance(content, list):
            continue
        kept = [b for b in content
                if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
                        and (strip_all or not b.get("signature") and b.get("type") == "thinking"))]
        if len(kept) != len(content):
            msg["content"] = kept or [{"type": "text", "text": "(no content)"}]
            changed = True
    return changed


def _to_generic(body):
    """Claude Code believes it is talking to Claude, so it sends Claude-only
    features. Reduce the request to the plain Messages API other providers speak."""
    for key in ("context_management", "container", "mcp_servers", "output_config", "thinking"):
        body.pop(key, None)
    body["max_tokens"] = min(int(body.get("max_tokens") or 8192), 32000)
    if isinstance(body.get("tools"), list):
        # Server tools (web search, advisor, ...) have a "type" and no schema.
        body["tools"] = [t for t in body["tools"] if "input_schema" in t]
        if not body["tools"]:
            del body["tools"]
            body.pop("tool_choice", None)

    merged = []
    for msg in body.get("messages", []):
        role, content = msg.get("role"), msg.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        if role == "system":
            # Mid-conversation system messages: keep text as a reminder, drop control blocks.
            text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
            if not text.strip():
                continue
            role, blocks = "user", [{"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}]
        blocks = [b for b in blocks if not (isinstance(b, dict) and
                  b.get("type") in ("thinking", "redacted_thinking", "configuration_update"))]
        if not blocks:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"].extend(blocks)
        else:
            merged.append({"role": role, "content": blocks})
    body["messages"] = merged


_ctx_lock = threading.Lock()
_ctx_variants = {}  # ollama model -> model with a big enough num_ctx


def _ollama_call(path, payload, timeout=600):
    conn = http.client.HTTPConnection("127.0.0.1:11434", timeout=timeout)
    try:
        conn.request("POST", path, body=json.dumps(payload), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 400:
            raise OSError(f"{path} {resp.status}: {data[:200].decode('utf-8', 'replace')}")
        return json.loads(data or b"{}")
    finally:
        conn.close()


VARIANT_SUFFIX = re.compile(r"[-:]g2-\d+k$")


def base_model(model):
    """`qwen3:4b-g2-64k` -> `qwen3:4b` (our context variants are an implementation detail)."""
    return VARIANT_SUFFIX.sub("", model)


def _ollama_ctx_variant(model, want, log):
    """Return (name, ctx) for a variant of `model` with a `want`-token context window,
    creating it the first time (it shares the model's weights, so no extra disk)."""
    model = base_model(model)
    with _ctx_lock:
        if (model, want) in _ctx_variants:
            return _ctx_variants[(model, want)]
        try:
            info = _ollama_call("/api/show", {"model": model}, timeout=30)
            params = info.get("parameters") or ""
            found = [int(line.split()[1]) for line in params.splitlines()
                     if line.split()[:1] == ["num_ctx"] and len(line.split()) > 1]
            if found and found[0] == want:
                result = (model, want)
            else:
                k = want // 1024
                name = f"{model}-g2-{k}k" if ":" in model else f"{model}:g2-{k}k"
                _ollama_call("/api/create", {"model": name, "from": model, "stream": False,
                                             "parameters": {"num_ctx": want}})
                log(f"ollama     using {name} ({k}k context)")
                result = (name, want)
        except (OSError, ValueError) as e:
            log(f"ollama     couldn't set up a {want // 1024}k context for {model}: {e}")
            return model, want  # don't cache; retry next request
        _ctx_variants[(model, want)] = result
        return result


def _too_long(tokens, limit):
    # Exact wording matters: Claude Code matches "prompt is too long: N tokens > M"
    # and responds by compacting the conversation and retrying.
    return {"type": "error", "error": {"type": "invalid_request_error",
            "message": f"prompt is too long: {tokens} tokens > {limit} maximum"}}


def _estimate_tokens(body):
    return {"input_tokens": max(1, len(json.dumps(body)) // 4)}


class RelayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: State = None  # set by start()

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass  # client hung up (Claude Code closes idle keep-alive sockets)

    def do_GET(self):
        if self.path == SHOW_PATH:
            self.state.on_show()
            return self._send_json(200, {"ok": True})
        self._handle(b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._handle(self.rfile.read(length) if length else b"")

    def _handle(self, raw):
        provider, models, key = self.state.snapshot()
        path = self.path
        body = None
        if raw and "json" in (self.headers.get("Content-Type") or ""):
            try:
                body = json.loads(raw)
            except ValueError:
                body = None

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        asked_model = body.get("model") if isinstance(body, dict) else None

        is_claude = provider in CLAUDE_PROVIDERS
        if not is_claude and isinstance(body, dict):
            if asked_model:
                slot = "fast" if "haiku" in asked_model.lower() else "main"
                body["model"] = models.get(slot) or models.get("main") or asked_model
            if "messages" in body:
                _to_generic(body)
        if not is_claude:
            path = path.replace("?beta=true", "").replace("&beta=true", "")

        call = None
        if isinstance(body, dict) and "messages" in body and "count_tokens" not in path:
            # Claude Code uses its small/fast model for side jobs (titles, summaries, checks).
            background = "haiku" in (asked_model or "").lower()
            call = activity.Call(self.state.on_ai, provider, body, background)

        for k in list(headers):
            if k.lower() in AUTH_HEADERS:
                del headers[k]
        if provider in KEY_LABELS and not key:
            message = f"G2 Switcher: no {KEY_LABELS[provider]} - enter one in the G2 Switcher window"
            if call:
                call.fail(message)
            return self._send_json(401, {"type": "error", "error": {"type": "authentication_error",
                                                                     "message": message}})

        if is_claude and isinstance(body, dict) and "/messages" in path:
            _strip_unsigned_thinking(body)
        if provider == "claude":
            headers["x-api-key"] = key
        elif provider == "claude_sub":
            # Subscription (OAuth) tokens are only accepted with the oauth beta flag.
            betas = [b.strip() for k, v in headers.items() if k.lower() == "anthropic-beta"
                     for b in v.split(",") if b.strip()]
            for k in [k for k in headers if k.lower() == "anthropic-beta"]:
                del headers[k]
            headers["anthropic-beta"] = ",".join(dict.fromkeys(betas + [OAUTH_BETA]))
            headers["Authorization"] = f"Bearer {key}"
        elif provider == "openrouter":
            for k in list(headers):
                if k.lower() in ("anthropic-workspace-id", "anthropic-beta"):
                    del headers[k]
            headers["Authorization"] = f"Bearer {key}"
        elif provider == "ollama":
            for k in list(headers):
                if k.lower() == "anthropic-beta":
                    del headers[k]
            if path.split("?")[0].endswith("/count_tokens"):
                return self._send_json(200, _estimate_tokens(body or {}))
            if isinstance(body, dict) and body.get("model"):
                body["model"], ctx = _ollama_ctx_variant(body["model"], self.state.ollama_ctx, self.state.log)
                self._fit_if_compaction(provider, body, ctx, call)
                # Ollama's own engine silently truncates oversized prompts (cutting off
                # Claude Code's instructions), so check size here. Measured ~4.1 JSON chars
                # per token; 3.7 leaves a margin, plus room for the reply.
                est = int(len(json.dumps(body)) / 3.7) + 4096
                if est > ctx:
                    self.state.log(f"ollama     ~{est} tokens > {ctx} context - asked Claude Code to compact")
                    if call:
                        call.compacting(est, ctx)
                    return self._send_json(400, _too_long(est, ctx))
                if self._maybe_compact(provider, body, ctx, call):
                    return
                if call:
                    call.context(estimate(body), ctx)
        if provider == "openrouter" and isinstance(body, dict):
            ctx = self.state.contexts.get(body.get("model"))
            if ctx:
                self._fit_if_compaction(provider, body, ctx, call)
                if self._maybe_compact(provider, body, ctx, call):
                    return
            if call:
                call.context(estimate(body), ctx)
        if is_claude and call:
            call.context(estimate(body), None)  # Claude Code manages Claude's own window

        sent_model = body.get("model") if isinstance(body, dict) else None
        t0 = time.time()
        status = self._forward(provider, path, headers, body, raw, call)
        if status == "retry-no-thinking":
            _strip_unsigned_thinking(body, strip_all=True)
            status = self._forward(provider, path, headers, body, raw, call, allow_retry=False)
        if "count_tokens" not in path:
            self.state.log(f"{provider:<10} {sent_model or '-'}  {status[:200]}  {time.time() - t0:.1f}s")

    def _maybe_compact(self, provider, body, ctx, call):
        """Ask Claude Code to compact before the provider's real context limit.

        Claude Code thinks it's talking to Claude (up to 1M tokens), so on its own it
        would never compact for a 64k local model. Answering with its "prompt is too
        long" error makes it summarise the conversation and carry on - even mid-task.
        Returns True if the request was answered here."""
        if not (self.state.auto_compact and body.get("tools") and "messages" in body) or is_compaction(body):
            return False  # side requests (titles etc.) have no tools and are small
        est = estimate(body)
        if est <= compact_trigger(ctx):
            self.state.just_compacted = False
            return False
        if self.state.just_compacted:
            # Right after a compaction: let it through rather than loop (the hard limit still applies).
            self.state.just_compacted = False
            return False
        trigger = compact_trigger(ctx)
        self.state.log(f"{provider:<10} ~{est} of {ctx} tokens used - asked Claude Code to compact")
        if call:
            call.compacting(est, ctx)
        self._send_json(400, _too_long(est, trigger))
        return True

    def _fit_if_compaction(self, provider, body, ctx, call):
        """A compaction request can itself be too big for a small context (one huge tool
        output pushed the conversation past the limit). Trim it so the summary can happen."""
        if not (self.state.auto_compact and is_compaction(body)):
            return
        self.state.just_compacted = True
        budget = ctx - 4096 - 6000  # room for the summary Claude Code asks for
        before = estimate(body)
        cut = fit_compaction(body, budget)
        if cut:
            self.state.log(f"{provider:<10} compaction request ~{before} -> ~{estimate(body)} tokens "
                           f"({cut} old outputs trimmed to fit {ctx})")
            if call:
                call.emit("trimmed", items=cut, before=before, after=estimate(body))

    def _forward(self, provider, path, headers, body, raw, call=None, allow_retry=True):
        status = self._forward_inner(provider, path, headers, body, raw, call, allow_retry)
        if call and status != "retry-no-thinking":
            if status.isdigit():
                call.end()
            else:
                call.fail(status if not status[:3].isdigit() else activity.error_message(status[4:].encode()))
        return status

    def _forward_inner(self, provider, path, headers, body, raw, call, allow_retry):
        base = urllib.parse.urlsplit(PROVIDERS[provider])
        data = json.dumps(body).encode() if body is not None else raw
        headers = dict(headers, **{"Content-Length": str(len(data))})
        if base.scheme == "https":
            conn = http.client.HTTPSConnection(base.netloc, timeout=900, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(base.netloc, timeout=900)
        try:
            conn.request(self.command, base.path + path, body=data if data else None, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            conn.close()
            self._send_json(502, {"type": "error", "error": {
                "type": "api_error", "message": f"G2 Switcher: {provider} unreachable ({e})"}})
            return f"unreachable: {e}"

        try:
            if resp.status >= 400:
                err = resp.read()
                if (allow_retry and provider in CLAUDE_PROVIDERS and resp.status == 400
                        and b"signature" in err.lower() and isinstance(body, dict)):
                    return "retry-no-thinking"
                if provider == "ollama" and b"exceed_context_size" in err:
                    # Reword into Anthropic's format so Claude Code auto-compacts and retries.
                    try:
                        e = json.loads(err).get("error", {})
                        n, limit = e.get("n_prompt_tokens", 0), e.get("n_ctx", self.state.ollama_ctx)
                    except ValueError:
                        n, limit = 0, self.state.ollama_ctx
                    err = json.dumps(_too_long(n, limit)).encode()
                    self._send_raw(400, [("Content-Type", "application/json")], err)
                    return f"400 context full ({n} > {limit}) - asked Claude Code to compact"
                if (provider == "ollama" and resp.status == 404 and b"not found" in err.lower()
                        and (body or {}).get("model")):
                    err = json.dumps({"type": "error", "error": {"type": "not_found_error", "message":
                        f"G2 Switcher: Ollama model '{(body or {}).get('model')}' not found"}}).encode()
                self._send_raw(resp.status, resp.getheaders(), err)
                return f"{resp.status} {err[:2000].decode('utf-8', 'replace')}"

            if call:
                call.headers(resp.getheader("Content-Type"))
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_HEADERS:
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                if call:
                    try:
                        call.feed(chunk)
                    except Exception:  # the activity feed must never break the actual stream
                        call = None
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return str(resp.status)
        except OSError as e:
            self.close_connection = True
            return f"stream broken: {e}"
        finally:
            conn.close()

    def _send_raw(self, status, headers, data):
        self.send_response(status)
        for k, v in headers:
            if k.lower() not in HOP_HEADERS:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status, obj):
        self._send_raw(status, [("Content-Type", "application/json")], json.dumps(obj).encode())


class _ExclusiveServer(http.server.ThreadingHTTPServer):
    # HTTPServer's default SO_REUSEADDR lets a second copy bind the same port on
    # Windows and silently share traffic; insist on owning it instead.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def start(state, port):
    handler = type("BoundRelayHandler", (RelayHandler,), {"state": state})
    server = _ExclusiveServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="relay").start()
    return server
