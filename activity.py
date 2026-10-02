"""Turns the Messages API traffic passing through the relay into a readable
"what is the AI doing" feed: what you asked, thinking, text, tool calls, errors.

Everything here only *reads* copies of the data; the relay forwards bytes unchanged.
Events are plain dicts handed to an `emit` callback:

  request   new model call            {provider, model, background, tokens, input}
  first     first byte of the reply   {wait}
  thinking  model started thinking
  thought   thinking finished         {secs}
  writing   text is streaming         {chars}           (progress only, throttled)
  text      a text block finished     {text}
  tool      a tool call finished      {name, detail}
  done      reply finished            {secs, stop, out_tokens}
  error     the call failed           {message}
  compact   relay asked Claude Code to compact first   {used, limit}
  trimmed   relay shrank an oversized compaction request {items, before, after}
"""

import itertools
import json
import time

_ids = itertools.count(1)

COMPACT_MARKER = "create a detailed summary of the conversation so far"  # Claude Code's compaction prompt

# Most informative argument to show for common Claude Code tools.
_TOOL_ARGS = ("file_path", "command", "pattern", "url", "query", "path", "description", "prompt", "skill")


def _clip(text, n):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def describe_input(body):
    """One line about what this call is answering: the user's words or tool results."""
    msgs = body.get("messages") or []
    if not msgs:
        return ""
    last = msgs[-1]
    blocks = _blocks(last.get("content"))
    if any(COMPACT_MARKER in (b.get("text") or "") for b in blocks):
        return "compacting: summarizing the conversation so far"
    results = [b for b in blocks if b.get("type") == "tool_result"]
    if results:
        failed = sum(1 for b in results if b.get("is_error"))
        word = "result" if len(results) == 1 else "results"
        return f"tool {word} sent back ({len(results)}{f', {failed} failed' if failed else ''})"
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    # Claude Code wraps context in <system-reminder> blocks; the user's words are what's left.
    texts = [t for t in texts if t.strip() and not t.lstrip().startswith("<")]
    return f"you: {_clip(texts[-1], 160)}" if texts else ""


def tool_detail(name, args):
    if not isinstance(args, dict):
        return ""
    for key in _TOOL_ARGS:
        if args.get(key):
            return _clip(args[key], 90)
    return _clip(json.dumps(args), 90) if args else ""


class Call:
    """Tracks one model call. Feed it response bytes; it emits events."""

    def __init__(self, emit, provider, body, background):
        self.emit = lambda kind, **kw: emit(dict(kind=kind, rid=self.rid, **kw))
        self.rid = next(_ids)
        self.t0 = time.time()
        self.buf = b""
        self.json_body = b""
        self.is_sse = None
        self.blocks = {}
        self.got_first = False
        self.last_progress = 0
        self.stop = None
        self.out_tokens = None
        self.ended = False
        self.emit("request", provider=provider, model=body.get("model", "?"), background=background,
                  tokens=max(1, len(json.dumps(body)) // 4), input=describe_input(body))

    # ---- response side ----
    def headers(self, content_type):
        self.is_sse = "event-stream" in (content_type or "")

    def feed(self, chunk):
        if not self.got_first:
            self.got_first = True
            self.emit("first", wait=time.time() - self.t0)
        if not self.is_sse:
            if len(self.json_body) < 4_000_000:
                self.json_body += chunk
            return
        self.buf += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n\n", 1)
            data = b"\n".join(line[5:].strip() for line in raw.split(b"\n") if line.startswith(b"data:"))
            if data:
                try:
                    self._event(json.loads(data))
                except ValueError:
                    pass

    def _event(self, ev):
        t = ev.get("type")
        if t == "content_block_start":
            block = dict(ev.get("content_block") or {})
            block.update(_text="", _json="", _t=time.time())
            self.blocks[ev.get("index")] = block
            if block.get("type") in ("thinking", "redacted_thinking"):
                self.emit("thinking")
        elif t == "content_block_delta":
            block = self.blocks.get(ev.get("index"))
            delta = ev.get("delta") or {}
            if block is None:
                return
            if delta.get("type") == "text_delta":
                block["_text"] += delta.get("text", "")
                if time.time() - self.last_progress > 0.4:
                    self.last_progress = time.time()
                    self.emit("writing", chars=len(block["_text"]))
            elif delta.get("type") == "input_json_delta":
                block["_json"] += delta.get("partial_json", "")
        elif t == "content_block_stop":
            self._finish_block(self.blocks.pop(ev.get("index"), None))
        elif t == "message_delta":
            self.stop = (ev.get("delta") or {}).get("stop_reason") or self.stop
            self.out_tokens = (ev.get("usage") or {}).get("output_tokens", self.out_tokens)
        elif t == "error":
            self.fail((ev.get("error") or {}).get("message") or "stream error")

    def _finish_block(self, block):
        if not block:
            return
        kind = block.get("type")
        if kind in ("thinking", "redacted_thinking"):
            self.emit("thought", secs=time.time() - block["_t"])
        elif kind == "text" and (block["_text"] or block.get("text", "")).strip():
            self.emit("text", text=_clip(block["_text"] or block.get("text", ""), 220))
        elif kind in ("tool_use", "server_tool_use"):
            try:
                args = json.loads(block["_json"]) if block["_json"] else block.get("input")
            except ValueError:
                args = None
            name = block.get("name", "tool")
            self.emit("tool", name=name, detail=tool_detail(name, args))

    def end(self):
        if self.ended:
            return
        if not self.is_sse and self.json_body:
            try:
                msg = json.loads(self.json_body)
                if msg.get("type") == "error":  # some providers send errors with HTTP 200
                    return self.fail((msg.get("error") or {}).get("message") or "provider error")
                for block in msg.get("content") or []:
                    if isinstance(block, dict):
                        block = dict(block, _text=block.get("text", ""), _t=time.time(),
                                     _json=json.dumps(block.get("input")) if "input" in block else "")
                        self._finish_block(block)
                self.stop = msg.get("stop_reason")
                self.out_tokens = (msg.get("usage") or {}).get("output_tokens")
            except ValueError:
                pass
        self.ended = True
        self.emit("done", secs=time.time() - self.t0, stop=self.stop, out_tokens=self.out_tokens)

    def compacting(self, used, limit):
        if not self.ended:
            self.ended = True
            self.emit("compact", used=used, limit=limit)

    def fail(self, message):
        if not self.ended:
            self.ended = True
            self.emit("error", message=_clip(message, 300), secs=time.time() - self.t0)


def error_message(raw):
    try:
        err = json.loads(raw).get("error") or {}
        return err.get("message") or str(err)
    except (ValueError, AttributeError):
        return raw[:300].decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
