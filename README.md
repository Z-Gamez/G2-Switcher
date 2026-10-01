# G2 Switcher

A small Windows tray app that lets the **Even Realities G2** glasses' Terminal mode
talk to whichever AI you pick — and switch between them mid-conversation, no restart.

| Provider | What it uses |
|---|---|
| **Claude · API key** | Pay-as-you-go [Anthropic API](https://console.anthropic.com) credits |
| **Claude · subscription** | Your Claude Pro / Max plan, via a `claude setup-token` token |
| **OpenRouter · free models** | Any `:free` model on [OpenRouter](https://openrouter.ai) |
| **Ollama · local** | A model running on your own PC with [Ollama](https://ollama.com) |

## How it works

```
G2 glasses ─▶ Even Terminal ─▶ Claude Code ─▶ G2 Switcher relay (127.0.0.1:3457) ─▶ selected provider
```

G2 Switcher starts Even Terminal with Claude Code pointed at a local relay. Claude Code
only ever sees a placeholder token; the relay adds the real key for the provider you
selected. For non-Claude providers it also strips the Claude-only request fields
(extended thinking, context management, server tools, …) those APIs reject, and for
Ollama it creates a large-context copy of your model, because Claude Code's prompt
alone is ~40k tokens.

## Install

**Requirements:** Windows 10/11, [Node.js](https://nodejs.org), and Even Terminal
(`npm install -g @evenrealities/even-terminal`). Ollama only if you want local models.

**Easiest:** download `G2-Switcher.exe` from the
[latest release](https://github.com/Z-Gamez/G2-Switcher/releases/latest) and run it.
It's unsigned, so Windows SmartScreen may warn you the first time — choose
*More info → Run anyway*, or build it yourself below.

**Build it yourself** (needs Python 3.10+):

```bat
pip install -r requirements.txt pyinstaller
build.bat
powershell -ExecutionPolicy Bypass -File install.ps1
```

`install.ps1` copies `G2 Switcher.exe` to `%LOCALAPPDATA%\Programs\G2 Switcher` and adds
Start menu and desktop shortcuts. Open it, then right-click its taskbar icon → **Pin to taskbar**.

Or run from source: `pip install -r requirements.txt` then `pythonw app.py`.

## Use

1. Pick a provider (click it or press **1–4**).
2. Paste the key for it under **settings** and press **test**.
   - Claude subscription: run `claude setup-token` in a terminal and paste the `sk-ant-oat…` token.
3. Press **start** to launch Even Terminal, then open Terminal mode on your glasses.

Closing the window keeps G2 Switcher in the system tray — right-click the tray icon to
switch providers, start/stop Even Terminal, or quit. Launching it again just brings the
window back.

## Where things are stored

| What | Where |
|---|---|
| API keys / tokens | `%USERPROFILE%\.even-terminal\anthropic.key`, `claude-subscription.token`, `openrouter.key` |
| Settings | `%LOCALAPPDATA%\G2Switcher\settings.json` |
| Even Terminal logs | `%LOCALAPPDATA%\G2Switcher\logs\` |

Keys are plain text files readable by your Windows user account — the same way
Even Terminal and Claude Code store their own credentials. Nothing is sent anywhere
except to the provider you select.

## Troubleshooting

- **"Your credit balance is too low"** with credits on your account — the key belongs to a
  different organization than the credits. Check each organization's *Billing* page in the
  Anthropic Console and create the key in the one with the balance. (claude.ai "extra usage"
  is for subscriptions and doesn't fund API keys.)
- **OpenRouter errors like "overloaded" or "Upstream idle timeout"** — free models are shared
  and get busy. Pick a different `:free` model.
- **Ollama's first reply is slow** — the first turn has to process Claude Code's whole prompt;
  later turns are fast.
- **"another copy is running"** — an Even Terminal not started by G2 Switcher is using the port.
  Press start and G2 Switcher will offer to replace it.

## Development

`G2_SWITCHER_PORT=3499 python app.py` runs a second copy on another relay port.

| File | Purpose |
|---|---|
| `app.py` | Tkinter UI and tray icon |
| `backend.py` | Settings, keys, key tests, Even Terminal process |
| `relay.py` | The local Anthropic-API relay |
| `theme.py` | Colors, fonts, app icon (`python theme.py` writes `g2switcher.ico`) |

Not affiliated with Even Realities or Anthropic.

## License

MIT
