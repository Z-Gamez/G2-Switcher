"""G2 Switcher - run Even Terminal and switch its AI provider on the fly.

Even Terminal's Claude Code talks to a local relay (relay.py). Picking a
provider here changes where the *next* request goes; nothing restarts.
Closing the window keeps it running in the system tray.
"""

import json
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
from tkinter import messagebox, ttk

import pystray
from PIL import ImageTk

import backend
import relay
import theme as T

VERSION = "1.0.0"

PROVIDERS = {
    #  key          name          variant          one-line description
    "claude":     ("Claude",     "API key",       "pay-as-you-go API credits"),
    "claude_sub": ("Claude",     "subscription",  "your Pro / Max plan"),
    "openrouter": ("OpenRouter", "free models",   "free cloud models"),
    "ollama":     ("Ollama",     "local",         "runs on this PC"),
}
KEY_FIELD = {"claude": "api key", "claude_sub": "token", "openrouter": "api key"}
HINTS = {
    "claude": "Create a key at console.anthropic.com → API Keys. Usage is billed to the "
              "credits of the organization the key belongs to.",
    "claude_sub": "Run  claude setup-token  in a terminal, sign in, and paste the sk-ant-oat… "
                  "token it prints. Counts toward your plan's usage limits.",
    "openrouter": "Free models get busy. If replies stall or error, pick a different :free model.",
    "ollama": "Needs Ollama running. The first reply can take a few minutes while Claude Code's "
              "~40k-token prompt loads; later replies are quick.",
}


def label(provider):
    name, variant, _ = PROVIDERS[provider]
    return f"{name} · {variant}"


class Button(tk.Label):
    """A `[ text ]` terminal-style button that inverts on hover."""

    def __init__(self, parent, text, command, font, fg=T.GREEN):
        super().__init__(parent, bg=parent["bg"], fg=fg, font=font, cursor="hand2", padx=2)
        self.command, self.fg, self.enabled = command, fg, True
        self.set_text(text)
        self.bind("<Enter>", lambda e: self.enabled and self.configure(bg=self.fg, fg=T.BLACK))
        self.bind("<Leave>", lambda e: self._paint())
        self.bind("<Button-1>", lambda e: self.enabled and self.command())

    def set_text(self, text):
        self.configure(text=f"[ {text} ]")

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._paint()

    def _paint(self):
        self.configure(bg=self.master["bg"], fg=self.fg if self.enabled else T.LINE)


class App:
    def __init__(self, root, state):
        self.root, self.state = root, state
        self.settings = backend.load_settings()
        self.events = queue.Queue()
        self.et = backend.EvenTerminal(on_line=lambda line: self.events.put(("et", line)))
        self.model_lists = {"openrouter": [], "ollama": []}
        self.ollama_online = None
        self.key_vars = {p: tk.StringVar(value=state.keys[p]) for p in KEY_FIELD}
        self.key_msgs = {p: ("saved" if state.keys[p] else "no key yet", T.DIM) for p in KEY_FIELD}
        self.tray = None
        self.tray_hint_shown = False
        self.last_tray = None

        state.log = lambda msg: self.events.put(("relay", msg))
        state.on_show = lambda: self.events.put(("call", self.show))

        self._fonts()
        self._style()
        self._build()
        self.select(self.settings["provider"], announce=False)
        for p in self.model_lists:
            self.refresh_models(p)
        self._start_tray()

        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.bind("<Key>", self._hotkey)
        root.after(100, self._pump)
        root.after(300, self._watch)
        root.after(500, self._blink)
        self.write(f"g2 switcher {VERSION} · relay on 127.0.0.1:{backend.RELAY_PORT}", "dim")
        if self.settings.get("autostart"):
            root.after(400, self.start_et)

    # ---------- look ----------
    def _fonts(self):
        mono = T.pick_mono(set(tkfont.families(self.root)))
        self.f_title = (mono, 17, "bold")
        self.f = (mono, 10)
        self.f_bold = (mono, 10, "bold")
        self.f_small = (mono, 9)
        self.f_head = (mono, 9, "bold")

    def _style(self):
        r = self.root
        r.configure(bg=T.BG)
        s = ttk.Style(r)
        s.theme_use("clam")
        s.configure("Term.TCombobox", fieldbackground=T.FIELD, background=T.PANEL, foreground=T.FG,
                    arrowcolor=T.GREEN, bordercolor=T.LINE, lightcolor=T.LINE, darkcolor=T.LINE,
                    selectbackground=T.FIELD, selectforeground=T.GREEN, insertcolor=T.GREEN, padding=4)
        s.map("Term.TCombobox", fieldbackground=[("readonly", T.FIELD)],
              bordercolor=[("focus", T.GREEN)], arrowcolor=[("active", T.FG)])
        s.configure("Term.Vertical.TScrollbar", troughcolor=T.FIELD, background=T.LINE,
                    bordercolor=T.FIELD, arrowcolor=T.DIM, lightcolor=T.LINE, darkcolor=T.LINE)
        s.map("Term.Vertical.TScrollbar", background=[("active", T.DIM)])
        for opt, val in (("background", T.FIELD), ("foreground", T.FG), ("selectBackground", T.GREEN),
                         ("selectForeground", T.BLACK), ("font", self.f)):
            r.option_add(f"*TCombobox*Listbox.{opt}", val)

    def _card(self, parent):
        return tk.Frame(parent, bg=T.PANEL, highlightthickness=1, highlightbackground=T.LINE)

    def _section(self, parent, title, note=""):
        head = tk.Frame(parent, bg=T.BG)
        head.pack(fill="x", pady=(16, 6))
        tk.Label(head, text=f"// {title}", bg=T.BG, fg=T.GREEN, font=self.f_head).pack(side="left")
        if note:
            tk.Label(head, text=note, bg=T.BG, fg=T.DIM, font=self.f_small).pack(side="left", padx=(8, 0))
        tk.Frame(head, bg=T.LINE, height=1).pack(side="left", fill="x", expand=True, padx=(10, 0))

    # ---------- layout ----------
    def _build(self):
        outer = tk.Frame(self.root, bg=T.BG)
        outer.pack(fill="both", expand=True, padx=20, pady=(14, 10))

        # header
        head = tk.Frame(outer, bg=T.BG)
        head.pack(fill="x")
        tk.Label(head, text="> g2_switcher", bg=T.BG, fg=T.GREEN, font=self.f_title).pack(side="left")
        self.cursor = tk.Label(head, text="█", bg=T.BG, fg=T.GREEN, font=self.f_title)
        self.cursor.pack(side="left")
        tk.Label(head, text=f"v{VERSION}", bg=T.BG, fg=T.DIM, font=self.f_small).pack(side="right", anchor="s")
        tk.Label(outer, text="pick which AI answers your G2 glasses' Even Terminal - switch any time",
                 bg=T.BG, fg=T.DIM, font=self.f_small, anchor="w").pack(fill="x")

        # even terminal
        self._section(outer, "even terminal")
        card = self._card(outer)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=T.PANEL)
        inner.pack(fill="x", padx=12, pady=10)
        self.et_dot = tk.Label(inner, text="●", bg=T.PANEL, fg=T.DIM, font=self.f_bold)
        self.et_dot.pack(side="left")
        self.et_text = tk.Label(inner, text="stopped", bg=T.PANEL, fg=T.FG, font=self.f, anchor="w")
        self.et_text.pack(side="left", padx=(8, 0))
        self.et_btn = Button(inner, "start", self.toggle_et, self.f_bold)
        self.et_btn.pack(side="right")
        self.auto_btn = Button(inner, "", self.toggle_autostart, self.f_small, fg=T.DIM)
        self.auto_btn.pack(side="right", padx=(0, 10))
        self._paint_autostart()

        # providers
        self._section(outer, "provider", "applies to your next message · keys 1-4")
        self.cards = {}
        for i, p in enumerate(PROVIDERS, 1):
            self.cards[p] = self._provider_card(outer, i, p)

        # settings for the selected provider
        self._section(outer, "settings")
        self.config_card = self._card(outer)
        self.config_card.pack(fill="x")

        # activity
        self._section(outer, "activity")
        logbox = tk.Frame(outer, bg=T.FIELD, highlightthickness=1, highlightbackground=T.LINE)
        logbox.pack(fill="both", expand=True)
        self.log = tk.Text(logbox, bg=T.FIELD, fg=T.FG, font=self.f_small, relief="flat", wrap="word",
                           height=8, padx=10, pady=8, state="disabled", cursor="arrow",
                           selectbackground=T.SEL, inactiveselectbackground=T.SEL)
        sb = ttk.Scrollbar(logbox, orient="vertical", command=self.log.yview, style="Term.Vertical.TScrollbar")
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, color in (("ts", T.LINE), ("dim", T.DIM), ("ok", T.GREEN), ("warn", T.AMBER),
                           ("err", T.RED), ("info", T.FG)):
            self.log.tag_configure(tag, foreground=color)

        # status bar
        bar = tk.Frame(outer, bg=T.BG)
        bar.pack(fill="x", pady=(8, 0))
        tk.Label(bar, text="✕ hides to tray", bg=T.BG, fg=T.DIM, font=self.f_small).pack(side="right")
        self.route = tk.Label(bar, bg=T.BG, fg=T.GREEN, font=self.f_small, anchor="w")
        self.route.pack(side="left", fill="x", expand=True)

    def _provider_card(self, parent, num, p):
        name, variant, desc = PROVIDERS[p]
        card = self._card(parent)
        card.pack(fill="x", pady=(0, 6))
        row = tk.Frame(card, bg=T.PANEL)
        row.pack(fill="x", padx=(0, 12), pady=8)
        w = {
            "bar": tk.Label(row, text="▌", bg=T.PANEL, fg=T.PANEL, font=self.f_bold),
            "num": tk.Label(row, text=str(num), bg=T.PANEL, fg=T.DIM, font=self.f_small, width=2),
            "name": tk.Label(row, text=name, bg=T.PANEL, fg=T.FG, font=self.f_bold),
            "variant": tk.Label(row, text=f"· {variant}", bg=T.PANEL, fg=T.FG, font=self.f, padx=0),
            "status": tk.Label(row, bg=T.PANEL, fg=T.DIM, font=self.f_small, width=12, anchor="e"),
            "desc": tk.Label(row, text=desc, bg=T.PANEL, fg=T.DIM, font=self.f_small),
        }
        for k in ("bar", "num", "name", "variant"):
            w[k].pack(side="left")
        w["status"].pack(side="right")
        w["desc"].pack(side="right", padx=(0, 14))
        widgets = [card, row, *w.values()]
        for widget in widgets:
            widget.bind("<Button-1>", lambda e, p=p: self.select(p))
            widget.bind("<Enter>", lambda e, p=p: self._hover(p, True))
            widget.bind("<Leave>", lambda e, p=p: self._hover(p, False))
            widget.configure(cursor="hand2")
        return {"card": card, "row": row, "w": w}

    def _hover(self, p, on):
        if p != self.state.provider:
            self._paint_card(p, T.HOVER if on else T.PANEL)

    def _paint_card(self, p, bg):
        c = self.cards[p]
        selected = p == self.state.provider
        c["card"].configure(bg=bg, highlightbackground=T.GREEN if selected else T.LINE)
        c["row"].configure(bg=bg)
        for k, widget in c["w"].items():
            widget.configure(bg=bg)
        c["w"]["bar"].configure(fg=T.GREEN if selected else bg)
        c["w"]["name"].configure(fg=T.GREEN if selected else T.FG)

    def _paint_cards(self):
        for p in PROVIDERS:
            self._paint_card(p, T.SEL if p == self.state.provider else T.PANEL)
            status = self.cards[p]["w"]["status"]
            if p in KEY_FIELD:
                ok = bool(self.state.keys[p])
                status.configure(text="● ready" if ok else "○ needs key", fg=T.GREEN if ok else T.AMBER)
            else:
                text, color = {True: ("● online", T.GREEN), False: ("○ offline", T.RED),
                               None: ("…", T.DIM)}[self.ollama_online]
                status.configure(text=text, fg=color)

    # ---------- settings panel ----------
    def _build_config(self):
        p = self.state.provider
        for child in self.config_card.winfo_children():
            child.destroy()
        box = tk.Frame(self.config_card, bg=T.PANEL)
        box.pack(fill="x", padx=14, pady=10)
        box.columnconfigure(1, weight=1)
        r = 0
        tk.Label(box, text=label(p), bg=T.PANEL, fg=T.GREEN, font=self.f_bold).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(0, 6))
        r += 1

        if p in KEY_FIELD:
            tk.Label(box, text=KEY_FIELD[p], bg=T.PANEL, fg=T.DIM, font=self.f_small, width=8,
                     anchor="w").grid(row=r, column=0, sticky="w")
            entry = tk.Entry(box, textvariable=self.key_vars[p], show="•", bg=T.FIELD, fg=T.FG,
                             insertbackground=T.GREEN, relief="flat", font=self.f, highlightthickness=1,
                             highlightbackground=T.LINE, highlightcolor=T.GREEN,
                             selectbackground=T.GREEN, selectforeground=T.BLACK)
            entry.grid(row=r, column=1, sticky="we", ipady=4)
            entry.bind("<Return>", lambda e: self.save_key(p))
            btns = tk.Frame(box, bg=T.PANEL)
            btns.grid(row=r, column=2, sticky="e", padx=(8, 0))
            show = Button(btns, "show", None, self.f_small, fg=T.DIM)

            def toggle(entry=entry, show=show):
                hidden = bool(entry.cget("show"))
                entry.configure(show="" if hidden else "•")
                show.set_text("hide" if hidden else "show")
            show.command = toggle
            show.pack(side="left")
            Button(btns, "save", lambda: self.save_key(p), self.f_small).pack(side="left", padx=(4, 0))
            Button(btns, "test", lambda: self.test_key(p), self.f_small).pack(side="left", padx=(4, 0))
            r += 1
            text, color = self.key_msgs[p]
            self.key_msg = tk.Label(box, text=text, bg=T.PANEL, fg=color, font=self.f_small, anchor="w",
                                    justify="left")
            self.key_msg.grid(row=r, column=1, columnspan=2, sticky="we", pady=(3, 4))
            r += 1

        self.model_combo = None
        if p in self.model_lists:
            tk.Label(box, text="model", bg=T.PANEL, fg=T.DIM, font=self.f_small, width=8,
                     anchor="w").grid(row=r, column=0, sticky="w", pady=(4, 0))
            var = tk.StringVar(value=self.settings["models"][p]["main"])
            cb = ttk.Combobox(box, textvariable=var, style="Term.TCombobox", font=self.f)
            cb.grid(row=r, column=1, sticky="we", pady=(4, 0))
            self._fill_combo(cb, p)
            for ev in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
                cb.bind(ev, lambda e, var=var: self.set_model(p, var.get()))
            Button(box, "refresh", lambda: self.refresh_models(p), self.f_small).grid(
                row=r, column=2, sticky="e", padx=(8, 0), pady=(4, 0))
            self.model_combo = cb
            r += 1

        self.hint = tk.Label(box, text=HINTS[p], bg=T.PANEL, fg=T.DIM, font=self.f_small, anchor="w",
                             justify="left", wraplength=560)
        self.hint.grid(row=r, column=0, columnspan=3, sticky="we", pady=(8, 0))
        box.bind("<Configure>", lambda e: self.hint.configure(wraplength=max(200, e.width - 10)))

    def _fill_combo(self, cb, p):
        names, current = self.model_lists[p], cb.get()
        cb["values"] = names if current in names or not current else [current] + names

    # ---------- actions ----------
    def select(self, p, announce=True):
        changed = p != self.state.provider
        with self.state.lock:
            self.state.provider = p
            self.state.models = json.loads(json.dumps(self.settings["models"]))
        self.settings["provider"] = p
        backend.save_settings(self.settings)
        self._paint_cards()
        self._build_config()
        self._update_route()
        if announce and changed:
            self.write(f"switched to {label(p)}", "ok")
            if p in KEY_FIELD and not self.state.keys[p]:
                self.write(f"{label(p)} has no {KEY_FIELD[p]} yet - add one under settings", "warn")
        self._update_tray()

    def set_model(self, p, model):
        model = model.strip()
        if not model or model == self.settings["models"][p]["main"]:
            return
        self.settings["models"][p]["main"] = model
        if p == "ollama":
            self.settings["models"][p]["fast"] = model  # one local model avoids VRAM swapping
        with self.state.lock:
            self.state.models = json.loads(json.dumps(self.settings["models"]))
        backend.save_settings(self.settings)
        self.write(f"{PROVIDERS[p][0]} model → {model}", "ok")
        self._update_route()

    def _set_key_msg(self, p, text, color):
        self.key_msgs[p] = (text, color)
        if self.state.provider == p and getattr(self, "key_msg", None) and self.key_msg.winfo_exists():
            self.key_msg.configure(text=text, fg=color)

    def save_key(self, p):
        key = self.key_vars[p].get().strip()
        problem = backend.key_problem(p, key)
        if problem:
            self._set_key_msg(p, "✗ " + problem, T.RED)
            return False
        backend.write_key(p, key)
        with self.state.lock:
            self.state.keys[p] = key
        self.key_vars[p].set(key)
        self._set_key_msg(p, "✓ saved" if key else "key removed", T.GREEN if key else T.DIM)
        self.write(f"{label(p)} {KEY_FIELD[p]} {'saved' if key else 'removed'}", "ok" if key else "dim")
        self._paint_cards()
        return True

    def test_key(self, p):
        if not self.save_key(p):
            return
        key = self.state.keys[p]
        if not key:
            self._set_key_msg(p, f"✗ enter a {KEY_FIELD[p]} first", T.RED)
            return
        self._set_key_msg(p, "testing…", T.AMBER)

        def work():
            ok, msg = backend.test_key(p, key)
            self.events.put(("keytest", (p, ok, msg)))
        threading.Thread(target=work, daemon=True).start()

    def refresh_models(self, p):
        def work():
            try:
                self.events.put(("models", (p, backend.list_models(p))))
            except Exception as e:
                self.events.put(("models_failed", (p, str(e))))
        threading.Thread(target=work, daemon=True).start()

    def toggle_autostart(self):
        self.settings["autostart"] = not self.settings.get("autostart")
        backend.save_settings(self.settings)
        self._paint_autostart()

    def _paint_autostart(self):
        self.auto_btn.configure(text=f"[{'x' if self.settings.get('autostart') else ' '}] auto-start")

    # ---------- Even Terminal ----------
    def toggle_et(self):
        self.stop_et() if self.et.running else self.start_et()

    def start_et(self):
        if self.et.running:
            return
        port = backend.et_port()
        other = backend.pid_listening_on(port)
        if other:
            self.show()
            if not messagebox.askyesno("G2 Switcher", f"Another Even Terminal is already running on port "
                                       f"{port} (PID {other}).\n\nStop it and start one managed by G2 Switcher?"):
                return
            backend.kill_tree(other)
            time.sleep(1)
        try:
            self.et.start()
        except FileNotFoundError as e:
            self.write(str(e), "err")
            return
        self.write(f"even terminal starting (pid {self.et.proc.pid})", "ok")
        self._watch(reschedule=False)

    def stop_et(self):
        if self.et.running:
            self.et.stop()
            self.write("even terminal stopped", "warn")
        self._watch(reschedule=False)

    # ---------- log / status ----------
    def write(self, text, tag="info"):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S  "), "ts")
        self.log.insert("end", text + "\n", tag)
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 2000:
            self.log.delete("1.0", f"{lines - 2000}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _relay_tag(self, msg):
        m = re.search(r"\s(\d{3})\s", msg)
        if "unreachable" in msg or "stream broken" in msg or (m and int(m.group(1)) >= 400):
            return "err"
        return "ok" if m else "info"

    def _update_route(self):
        p = self.state.provider
        model = f" · {self.settings['models'][p]['main']}" if p in self.model_lists else ""
        self.route.configure(text=f"▶ routing to {label(p)}{model}")

    def _watch(self, reschedule=True):
        port = backend.et_port()
        if self.et.running:
            self.et_dot.configure(fg=T.GREEN)
            self.et_text.configure(text=f"running · pid {self.et.proc.pid} · port {port}", fg=T.FG)
            self.et_btn.set_text("stop")
            self.et_btn.fg = T.AMBER
        else:
            other = backend.pid_listening_on(port)
            if other:
                self.et_dot.configure(fg=T.AMBER)
                self.et_text.configure(text=f"another copy is running (pid {other}) - not routed here",
                                       fg=T.AMBER)
            else:
                self.et_dot.configure(fg=T.DIM)
                self.et_text.configure(text="stopped - press start, then open Terminal on your G2", fg=T.DIM)
            self.et_btn.set_text("start")
            self.et_btn.fg = T.GREEN
        self.et_btn._paint()
        self._update_tray()
        if reschedule:
            self.root.after(2000, self._watch)

    def _blink(self):
        self.cursor.configure(fg=T.BG if self.cursor.cget("fg") == T.GREEN else T.GREEN)
        self.root.after(530, self._blink)

    def _hotkey(self, event):
        if isinstance(event.widget, (tk.Entry, ttk.Combobox)):
            return
        if event.char in "1234" and event.char:
            self.select(list(PROVIDERS)[int(event.char) - 1])

    def _pump(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "call":
                payload()
            elif kind == "models":
                p, names = payload
                self.model_lists[p] = names
                if p == "ollama":
                    self.ollama_online = True
                    self._paint_cards()
                if self.model_combo and self.state.provider == p:
                    self._fill_combo(self.model_combo, p)
            elif kind == "models_failed":
                p, err = payload
                if p == "ollama":
                    self.ollama_online = False
                    self._paint_cards()
                    self.write("ollama isn't reachable on 127.0.0.1:11434 - is it running?", "warn")
                else:
                    self.write(f"couldn't list {p} models: {err}", "warn")
            elif kind == "keytest":
                p, ok, msg = payload
                self._set_key_msg(p, ("✓ " if ok else "✗ ") + msg, T.GREEN if ok else T.RED)
                self.write(f"{label(p)} test: {msg}", "ok" if ok else "err")
            elif kind == "relay":
                self.write("→ " + payload, self._relay_tag(payload))
            else:
                self.write(payload, "dim")
        self.root.after(100, self._pump)

    # ---------- tray ----------
    def _start_tray(self):
        def post(fn):
            return lambda: self.events.put(("call", fn))

        def choose(p):
            return lambda: self.events.put(("call", lambda: self.select(p)))

        def is_on(p):
            return lambda item: self.state.provider == p

        items = [pystray.MenuItem("Open G2 Switcher", post(self.show), default=True), pystray.Menu.SEPARATOR]
        items += [pystray.MenuItem(label(p), choose(p), checked=is_on(p), radio=True) for p in PROVIDERS]
        items += [pystray.Menu.SEPARATOR,
                  pystray.MenuItem(lambda item: "Stop Even Terminal" if self.et.running else "Start Even Terminal",
                                   post(self.toggle_et)),
                  pystray.MenuItem("Quit", post(self.quit))]
        self.tray = pystray.Icon("g2switcher", T.make_icon(64, active=False), "G2 Switcher", pystray.Menu(*items))
        threading.Thread(target=self.tray.run, daemon=True, name="tray").start()

    def _update_tray(self):
        if not self.tray:
            return
        running = self.et.running
        sig = (running, self.state.provider)
        if sig == self.last_tray:
            return
        self.last_tray = sig
        self.tray.icon = T.make_icon(64, active=running)
        self.tray.title = f"G2 Switcher · {label(self.state.provider)} · {'running' if running else 'stopped'}"
        self.tray.update_menu()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def hide(self):
        self.root.withdraw()
        if not self.tray_hint_shown:
            self.tray_hint_shown = True
            try:
                self.tray.notify("Still running in the tray. Right-click the icon to switch or quit.",
                                 "G2 Switcher")
            except Exception:
                pass

    def quit(self):
        self.et.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        if not getattr(sys, "frozen", False):
            # Running from source: give the window its own taskbar icon instead of Python's.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("G2Switcher.App")
    except Exception:
        pass

    state = relay.State()
    state.keys = {p: backend.read_key(p) for p in KEY_FIELD}
    try:
        relay.start(state, backend.RELAY_PORT)
    except OSError:
        # Already running (e.g. launched again from the taskbar): bring that window up instead.
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{backend.RELAY_PORT}{relay.SHOW_PATH}", timeout=3).read()
            return
        except OSError:
            tk.Tk().withdraw()
            messagebox.showerror("G2 Switcher", f"Port {backend.RELAY_PORT} is in use by another program.")
            return

    root = tk.Tk()
    root.title("G2 Switcher")
    scale = root.winfo_fpixels("1i") / 96
    root.geometry(f"{int(720 * scale)}x{int(860 * scale)}")
    root.minsize(int(620 * scale), int(700 * scale))
    icon = ImageTk.PhotoImage(T.make_icon(64))
    root.iconphoto(True, icon)
    App(root, state)
    root.mainloop()


if __name__ == "__main__":
    main()
