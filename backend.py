"""Everything G2 Switcher does that isn't UI: settings, API keys, key tests,
and starting/stopping the Even Terminal process."""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import relay

# Settings and logs live outside the program folder so the packaged .exe can write them.
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "G2Switcher"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_DIR = DATA_DIR / "logs"

# Keys sit next to Even Terminal's own config, never in the program or repo folder.
ET_DIR = Path.home() / ".even-terminal"
KEY_FILES = {"claude": ET_DIR / "anthropic.key", "claude_sub": ET_DIR / "claude-subscription.token",
             "openrouter": ET_DIR / "openrouter.key"}
ET_CLI = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@evenrealities" / "even-terminal" / "bin" / "cli.js"
RELAY_PORT = int(os.environ.get("G2_SWITCHER_PORT", 3457))
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

PROVIDERS = ("claude", "claude_sub", "openrouter", "ollama")
MIN_CONTEXT = 64_000  # Claude Code's system prompt + tools are ~40k tokens before you say anything

DEFAULTS = {
    "provider": "claude",
    "autostart": False,  # start Even Terminal when G2 Switcher opens
    "model_filter": "free",  # OpenRouter list: all / free / paid / starred
    "favorites": [],         # starred OpenRouter model ids
    "auto_compact": True,    # compact before Ollama/OpenRouter context limits (Claude is never affected)
    "models": {
        "openrouter": {"main": "nvidia/nemotron-3-ultra-550b-a55b:free",
                       "fast": "nvidia/nemotron-3.5-lightning:free"},
        "ollama": {"main": "qwen3.8:27b", "fast": "qwen3.8:27b"},
    },
}


# ---------- settings ----------
def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if saved.get("provider") in PROVIDERS:
            s["provider"] = saved["provider"]
        s["autostart"] = bool(saved.get("autostart", False))
        if saved.get("model_filter") in ("all", "free", "paid", "starred"):
            s["model_filter"] = saved["model_filter"]
        s["favorites"] = [f for f in saved.get("favorites", []) if isinstance(f, str)]
        s["auto_compact"] = bool(saved.get("auto_compact", True))
        for p, m in saved.get("models", {}).items():
            s["models"].setdefault(p, {}).update(m)
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


# ---------- keys ----------
def read_key(provider):
    try:
        return KEY_FILES[provider].read_text().strip()
    except OSError:
        return ""


def write_key(provider, key):
    ET_DIR.mkdir(exist_ok=True)
    if key:
        KEY_FILES[provider].write_text(key, encoding="utf-8")
    else:
        KEY_FILES[provider].unlink(missing_ok=True)


def key_problem(provider, key):
    """Catch common paste mistakes before saving. Returns a short message or None."""
    if not key:
        return None
    if provider == "claude":
        if key.startswith("apikey_"):
            return "That's the key's ID. The real key starts with sk-ant- (shown once when created)."
        if key.startswith("sk-ant-oat"):
            return "That's a subscription token - use Claude (subscription) instead."
        if not key.startswith("sk-ant-"):
            return "Anthropic API keys start with sk-ant-"
    if provider == "claude_sub" and not key.startswith("sk-ant-oat"):
        return "Subscription tokens start with sk-ant-oat - run  claude setup-token  to get one."
    if provider == "openrouter" and not key.startswith("sk-or-"):
        return "OpenRouter keys start with sk-or-"
    return None


def fetch_json(url, timeout=10, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def test_key(provider, key):
    """Returns (ok, message). Makes a tiny real request so billing problems show up too."""
    try:
        if provider in relay.CLAUDE_PROVIDERS:
            body = {"model": "claude-haiku-4-5", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
            headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
            if provider == "claude":
                headers["x-api-key"] = key
            else:
                body["system"] = "You are Claude Code, Anthropic's official CLI for Claude."
                headers.update({"Authorization": f"Bearer {key}", "anthropic-beta": relay.OAUTH_BETA})
            fetch_json("https://api.anthropic.com/v1/messages", timeout=30,
                       data=json.dumps(body).encode(), headers=headers)
            return True, "key works and the account has credit" if provider == "claude" else "token works"
        info = fetch_json("https://openrouter.ai/api/v1/key", timeout=20,
                          headers={"Authorization": f"Bearer {key}"})["data"]
        tier = "free tier" if info.get("is_free_tier") else "paid credits"
        return True, f"key works ({tier}, ${info.get('usage', 0):.2f} used)"
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        return False, f"HTTP {e.code}: {msg or e.reason}"
    except OSError as e:
        return False, f"couldn't connect: {e}"


def _per_million(price):
    try:
        return float(price) * 1_000_000
    except (TypeError, ValueError):
        return -1


def _money(x):
    return f"${x:.2f}" if x < 10 else f"${x:.0f}"


def list_models(provider):
    """Models usable from Claude Code, as dicts: id, free, price (label), cost (sort key), ctx."""
    if provider == "ollama":
        return [{"id": m["name"], "free": True, "price": "local", "cost": 0, "ctx": None}
                for m in sorted(fetch_json("http://127.0.0.1:11434/api/tags")["models"], key=lambda m: m["name"])]
    models = []
    for m in fetch_json("https://openrouter.ai/api/v1/models", timeout=20)["data"]:
        if "tools" not in (m.get("supported_parameters") or []):
            continue  # Claude Code can't work without tool calling
        if m["id"].endswith(":batch") or (m.get("context_length") or 0) < MIN_CONTEXT:
            continue  # batch-only, or too small for Claude Code's prompt
        pricing = m.get("pricing") or {}
        p_in, p_out = _per_million(pricing.get("prompt")), _per_million(pricing.get("completion"))
        if p_in < 0 or p_out < 0:
            continue  # routers with variable pricing
        free = m["id"].endswith(":free") or (p_in == 0 and p_out == 0)
        models.append({"id": m["id"], "free": free, "cost": p_in + p_out, "ctx": m.get("context_length"),
                       "price": "free" if free else f"{_money(p_in)} / {_money(p_out)}"})
    return sorted(models, key=lambda m: m["id"])


# ---------- Even Terminal process ----------
def et_port():
    try:
        return int(json.loads((ET_DIR / "config.json").read_text()).get("port", 3456))
    except (OSError, ValueError):
        return 3456


def pid_listening_on(port):
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            return int(parts[4])
    return None


def kill_tree(pid):
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)


class EvenTerminal:
    """Runs `even-terminal start` with Claude Code pointed at the relay."""

    def __init__(self, on_line):
        self.proc = None
        self.on_line = on_line

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.running:
            return
        node = shutil.which("node")
        if not node or not ET_CLI.exists():
            raise FileNotFoundError("Couldn't find node or the even-terminal package "
                                    "(npm install -g @evenrealities/even-terminal).")
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith(("CLAUDE_CODE_", "ANTHROPIC_DEFAULT_")) or
                       k in ("CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"))}
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{RELAY_PORT}"
        # Claude Code gets a dummy token so it never needs (or refreshes) a Claude login;
        # the relay swaps in the real key for whichever provider is selected.
        env["ANTHROPIC_AUTH_TOKEN"] = relay.PLACEHOLDER_TOKEN
        env["FORCE_COLOR"] = "0"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logfile = LOG_DIR / time.strftime("even-terminal-%Y%m%d-%H%M%S.log")
        self.proc = subprocess.Popen(
            [node, str(ET_CLI), "start", "--log-file", str(logfile), "--log-level", "info"],
            cwd=str(Path.home()), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._read_output, args=(self.proc,), daemon=True).start()

    def _read_output(self, proc):
        ansi = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
        for line in proc.stdout:
            line = ansi.sub("", line).rstrip()
            if line and not line.startswith("[claude-sdk]"):
                self.on_line(line)
        self.on_line(f"even-terminal exited (code {proc.wait()})")

    def stop(self):
        if self.running:
            kill_tree(self.proc.pid)
        self.proc = None
