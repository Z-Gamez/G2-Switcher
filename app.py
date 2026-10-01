"""G2 Switcher - run Even Terminal and switch its AI provider on the fly.

Even Terminal's Claude Code talks to a local relay (relay.py). Picking a
provider here changes where the *next* request goes; nothing restarts.
Closing the window keeps it running in the system tray.
"""

import ctypes
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

VERSION = "1.1.0"

PROVIDERS = {
    #  key          name          variant          one-line description
    "claude":     ("Claude",     "API key",       "pay-as-you-go API credits"),
    "claude_sub": ("Claude",     "subscription",  "your Pro / Max plan"),
    "openrouter": ("OpenRouter", "any model",     "free or paid cloud models"),
    "ollama":     ("Ollama",     "local",         "runs on this PC"),
}
KEY_FIELD = {"claude": "api key", "claude_sub": "token", "openrouter": "api key"}
HINTS = {
    "claude": "Create a key at console.anthropic.com → API Keys. Usage is billed to the "
              "credits of the organization the key belongs to.",
    "claude_sub": "Run  claude setup-token  in a terminal, sign in, and paste the sk-ant-oat… "
                  "token it prints. Counts toward your plan's usage limits.",
    "openrouter": "Prices are per million tokens (in / out); one Claude Code turn sends ~40k+ tokens. "
                  "Free models get busy - if replies stall, pick another. Click ☆ to star a model.",
    "ollama": "Needs Ollama running. The first reply can take a few minutes while Claude Code's "
              "~40k-token prompt loads; later replies are quick.",
}
FILTERS = ("all", "free", "paid", "starred")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def label(provider):
    name, variant, _ = PROVIDERS[provider]
    return f"{name} · {variant}"


def short_ctx(n):
    if not n:
        return ""
    return f"{n / 1_000_000:g}M" if n >= 1_000_000 else f"{n // 1000}k"


class Button(tk.Label):
    """A `[ text ]` terminal-style button that inverts on hover."""

    def __init__(self, parent, text, command, font, fg=T.GREEN, brackets=True):
        super().__init__(parent, bg=parent["bg"], fg=fg, font=font, cursor="hand2", padx=2)
        self.command, self.fg, self.enabled, self.brackets = command, fg, True, brackets
        self.set_text(text)
        self.bind("<Enter>", lambda e: self.enabled and self.configure(bg=self.fg, fg=T.BLACK))
        self.bind("<Leave>", lambda e: self._paint())
        self.bind("<Button-1>", lambda e: self.enabled and self.command())

    def set_text(self, text):
        self.configure(text=f"[ {text} ]" if self.brackets else text)

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
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._fill_model_list())
        self.tray = None
        self.tray_hint_shown = False
        self.last_tray = None
        self.log_tab = "ai"
        self.ai = {"phase": "idle", "since": time.time(), "provider": "", "detail": "", "rid": None}
        self.spin = 0
        self.model_list = self.model_combo = None

        state.log = lambda msg: self.events.put(("relay", msg))
        state.on_show = lambda: self.events.put(("call", self.show))
        state.on_ai = lambda ev: self.events.put(("ai", ev))

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
        root.after(250, self._tick_status)
        self.write(f"g2 switcher {VERSION} · relay on 127.0.0.1:{backend.RELAY_PORT}", "dim", log="system")
        self.write("waiting for you to talk to Claude Code on your glasses…", "dim", log="ai")
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
        self.char_w = tkfont.Font(family=mono, size=9).measure("0")

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
        head.pack(fill="x", pady=(14, 6))
        tk.Label(head, text=f"// {title}", bg=T.BG, fg=T.GREEN, font=self.f_head).pack(side="left")
        if note:
            tk.Label(head, text=note, bg=T.BG, fg=T.DIM, font=self.f_small).pack(side="left", padx=(8, 0))
        tk.Frame(head, bg=T.LINE, height=1).pack(side="left", fill="x", expand=True, padx=(10, 0))
        return head

    def _entry(self, parent, var, **kw):
        return tk.Entry(parent, textvariable=var, bg=T.FIELD, fg=T.FG, insertbackground=T.GREEN,
                        relief="flat", font=self.f, highlightthickness=1, highlightbackground=T.LINE,
                        highlightcolor=T.GREEN, selectbackground=T.GREEN, selectforeground=T.BLACK, **kw)

    def _text(self, parent, height):
        t = tk.Text(parent, bg=T.FIELD, fg=T.FG, font=self.f_small, relief="flat", wrap="word",
                    height=height, padx=10, pady=8, state="disabled", cursor="arrow",
                    selectbackground=T.SEL, inactiveselectbackground=T.SEL, spacing1=1)
        for tag, color in (("ts", T.LINE), ("dim", T.DIM), ("ok", T.GREEN), ("warn", T.AMBER),
                           ("err", T.RED), ("info", T.FG), ("tool", T.CYAN)):
            t.tag_configure(tag, foreground=color)
        t.tag_configure("you", foreground=T.FG, font=self.f_head)
        t.tag_configure("indent", lmargin2=self.char_w * 13)
        return t

    # ---------- layout ----------
    def _build(self):
        outer = tk.Frame(self.root, bg=T.BG)
        outer.pack(fill="both", expand=True, padx=20, pady=(8, 10))

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
        inner.pack(fill="x", padx=12, pady=9)
        self.et_dot = tk.Label(inner, text="●", bg=T.PANEL, fg=T.DIM, font=self.f_bold)
        self.et_dot.pack(side="left")
        self.et_text = tk.Label(inner, text="stopped", bg=T.PANEL, fg=T.FG, font=self.f, anchor="w")
        self.et_text.pack(side="left", padx=(8, 0))
        self.et_btn = Button(inner, "start", self.toggle_et, self.f_bold)
        self.et_btn.pack(side="right")
        self.auto_btn = Button(inner, "", self.toggle_autostart, self.f_small, fg=T.DIM, brackets=False)
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

        # activity: live status + two logs (what the AI is doing / app & Even Terminal output)
        head = self._section(outer, "activity")
        self.tab_btns = {}
        for tab, text in (("system", "system"), ("ai", "ai")):
            b = Button(head, text, lambda t=tab: self.show_log(t), self.f_small, fg=T.DIM)
            b.pack(side="right", padx=(6, 0))
            self.tab_btns[tab] = b
        logbox = tk.Frame(outer, bg=T.FIELD, highlightthickness=1, highlightbackground=T.LINE)
        logbox.pack(fill="both", expand=True)
        self.status = tk.Label(logbox, bg=T.SEL, fg=T.DIM, font=self.f_small, anchor="w", padx=10, pady=5)
        self.status.pack(fill="x")
        body = tk.Frame(logbox, bg=T.FIELD)
        body.pack(fill="both", expand=True)
        self.logs = {"ai": self._text(body, 9), "system": self._text(body, 9)}
        self.log_sb = ttk.Scrollbar(body, orient="vertical", style="Term.Vertical.TScrollbar")
        self.log_sb.pack(side="right", fill="y")
        self.show_log("ai")

        # status bar
        bar = tk.Frame(outer, bg=T.BG)
        bar.pack(fill="x", pady=(8, 0))
        tk.Label(bar, text="✕ hides to tray", bg=T.BG, fg=T.DIM, font=self.f_small).pack(side="right")
        self.route = tk.Label(bar, bg=T.BG, fg=T.GREEN, font=self.f_small, anchor="w")
        self.route.pack(side="left", fill="x", expand=True)

    def show_log(self, tab):
        self.log_tab = tab
        for name, t in self.logs.items():
            t.pack_forget()
            self.tab_btns[name].fg = T.GREEN if name == tab else T.DIM
            self.tab_btns[name]._paint()
        t = self.logs[tab]
        t.configure(yscrollcommand=self.log_sb.set)
        self.log_sb.configure(command=t.yview)
        t.pack(side="left", fill="both", expand=True)
        t.see("end")

    def _provider_card(self, parent, num, p):
        name, variant, desc = PROVIDERS[p]
        card = self._card(parent)
        card.pack(fill="x", pady=(0, 5))
        row = tk.Frame(card, bg=T.PANEL)
        row.pack(fill="x", padx=(0, 12), pady=7)
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
        for widget in [card, row, *w.values()]:
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
        for widget in c["w"].values():
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
        self.model_list = self.model_combo = None
        box = tk.Frame(self.config_card, bg=T.PANEL)
        box.pack(fill="x", padx=14, pady=10)
        box.columnconfigure(1, weight=1)
        r = 0
        tk.Label(box, text=label(p), bg=T.PANEL, fg=T.GREEN, font=self.f_bold).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(0, 6))
        r += 1

        if p in KEY_FIELD:
            r = self._key_row(box, r, p)
        if p == "openrouter":
            r = self._openrouter_picker(box, r)
        elif p == "ollama":
            r = self._ollama_picker(box, r)

        self.hint = tk.Label(box, text=HINTS[p], bg=T.PANEL, fg=T.DIM, font=self.f_small, anchor="w",
                             justify="left", wraplength=560)
        self.hint.grid(row=r, column=0, columnspan=3, sticky="we", pady=(8, 0))
        box.bind("<Configure>", lambda e: self.hint.configure(wraplength=max(200, e.width - 10)))

    def _key_row(self, box, r, p):
        tk.Label(box, text=KEY_FIELD[p], bg=T.PANEL, fg=T.DIM, font=self.f_small, width=8,
                 anchor="w").grid(row=r, column=0, sticky="w")
        entry = self._entry(box, self.key_vars[p], show="•")
        entry.grid(row=r, column=1, sticky="we", ipady=4)
        entry.bind("<Return>", lambda e: self.save_key(p))
        btns = tk.Frame(box, bg=T.PANEL)
        btns.grid(row=r, column=2, sticky="e", padx=(8, 0))
        show = Button(btns, "show", None, self.f_small, fg=T.DIM)

        def toggle():
            hidden = bool(entry.cget("show"))
            entry.configure(show="" if hidden else "•")
            show.set_text("hide" if hidden else "show")
        show.command = toggle
        show.pack(side="left")
        Button(btns, "save", lambda: self.save_key(p), self.f_small).pack(side="left", padx=(4, 0))
        Button(btns, "test", lambda: self.test_key(p), self.f_small).pack(side="left", padx=(4, 0))
        text, color = self.key_msgs[p]
        self.key_msg = tk.Label(box, text=text, bg=T.PANEL, fg=color, font=self.f_small, anchor="w",
                                justify="left")
        self.key_msg.grid(row=r + 1, column=1, columnspan=2, sticky="we", pady=(3, 4))
        return r + 2

    def _openrouter_picker(self, box, r):
        # current model
        tk.Label(box, text="model", bg=T.PANEL, fg=T.DIM, font=self.f_small, width=8,
                 anchor="w").grid(row=r, column=0, sticky="w", pady=(4, 0))
        self.current_model = tk.Label(box, bg=T.PANEL, fg=T.GREEN, font=self.f, anchor="w")
        self.current_model.grid(row=r, column=1, sticky="we", pady=(4, 0))
        Button(box, "refresh", lambda: self.refresh_models("openrouter"), self.f_small).grid(
            row=r, column=2, sticky="e", padx=(8, 0), pady=(4, 0))
        r += 1

        # filters + search
        tk.Label(box, text="show", bg=T.PANEL, fg=T.DIM, font=self.f_small, width=8,
                 anchor="w").grid(row=r, column=0, sticky="w", pady=(8, 4))
        bar = tk.Frame(box, bg=T.PANEL)
        bar.grid(row=r, column=1, columnspan=2, sticky="we", pady=(8, 4))
        self.filter_btns = {}
        for f in FILTERS:
            b = Button(bar, f, lambda f=f: self.set_filter(f), self.f_small, fg=T.DIM)
            b.pack(side="left", padx=(0, 2))
            self.filter_btns[f] = b
        search = self._entry(bar, self.search_var, width=18)
        search.pack(side="right", ipady=2)
        tk.Label(bar, text="search", bg=T.PANEL, fg=T.DIM, font=self.f_small).pack(side="right", padx=(0, 6))
        r += 1

        # list
        frame = tk.Frame(box, bg=T.FIELD, highlightthickness=1, highlightbackground=T.LINE)
        frame.grid(row=r, column=0, columnspan=3, sticky="we")
        lb = tk.Listbox(frame, bg=T.FIELD, fg=T.FG, font=self.f_small, height=7, relief="flat",
                        highlightthickness=0, activestyle="none", selectbackground=T.SEL,
                        selectforeground=T.GREEN, exportselection=False, cursor="hand2")
        sb = ttk.Scrollbar(frame, orient="vertical", command=lb.yview, style="Term.Vertical.TScrollbar")
        lb.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        lb.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=2)
        lb.bind("<ButtonRelease-1>", self._model_click)
        lb.bind("<Button-3>", lambda e: self._toggle_star(lb.nearest(e.y)))
        lb.bind("<Return>", lambda e: self._model_click(None))
        lb.bind("<Configure>", lambda e: self._fill_model_list())
        self.model_list = lb
        self.paint_filters()
        return r + 1

    def _ollama_picker(self, box, r):
        tk.Label(box, text="model", bg=T.PANEL, fg=T.DIM, font=self.f_small, width=8,
                 anchor="w").grid(row=r, column=0, sticky="w", pady=(4, 0))
        var = tk.StringVar(value=self.settings["models"]["ollama"]["main"])
        cb = ttk.Combobox(box, textvariable=var, style="Term.TCombobox", font=self.f)
        cb.grid(row=r, column=1, sticky="we", pady=(4, 0))
        for ev in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
            cb.bind(ev, lambda e: self.set_model("ollama", var.get()))
        Button(box, "refresh", lambda: self.refresh_models("ollama"), self.f_small).grid(
            row=r, column=2, sticky="e", padx=(8, 0), pady=(4, 0))
        self.model_combo = cb
        self._fill_combo()
        return r + 1

    def _fill_combo(self):
        if not self.model_combo:
            return
        names, current = [m["id"] for m in self.model_lists["ollama"]], self.model_combo.get()
        self.model_combo["values"] = names if current in names or not current else [current] + names

    # --- openrouter list ---
    def set_filter(self, f):
        self.settings["model_filter"] = f
        backend.save_settings(self.settings)
        self.paint_filters()

    def paint_filters(self):
        for f, b in self.filter_btns.items():
            b.fg = T.GREEN if f == self.settings["model_filter"] else T.DIM
            b._paint()
        self._fill_model_list()

    def _visible_models(self):
        f, q = self.settings["model_filter"], self.search_var.get().strip().lower()
        favs = set(self.settings["favorites"])
        out = []
        for m in self.model_lists["openrouter"]:
            if (f == "free" and not m["free"]) or (f == "paid" and m["free"]) or (f == "starred" and m["id"] not in favs):
                continue
            if q and not all(word in m["id"].lower() for word in q.split()):
                continue
            out.append(m)
        out.sort(key=lambda m: (m["id"] not in favs, m["id"]))
        return out

    def _fill_model_list(self):
        lb = self.model_list
        if not lb or not lb.winfo_exists():
            return
        self._update_current_model()
        self.visible = self._visible_models()
        favs, current = set(self.settings["favorites"]), self.settings["models"]["openrouter"]["main"]
        cols = max(40, lb.winfo_width() // self.char_w - 2)
        name_w = cols - 2 - 16 - 6
        lb.delete(0, "end")
        for i, m in enumerate(self.visible):
            name = m["id"] if len(m["id"]) <= name_w else m["id"][: name_w - 1] + "…"
            star = "★" if m["id"] in favs else "☆"
            lb.insert("end", f"{star} {name:<{name_w}}{m['price']:>16}{short_ctx(m['ctx']):>6}")
            if m["id"] in favs:
                lb.itemconfigure(i, fg=T.AMBER if m["id"] != current else T.GREEN)
            if m["id"] == current:
                lb.selection_set(i)
                lb.see(i)
        if not self.visible:
            msg = {"starred": "  no starred models yet - click ☆ next to a model (or right-click it)",
                   "all": "  loading models…" if not self.model_lists["openrouter"] else "  no matches"}
            lb.insert("end", msg.get(self.settings["model_filter"], "  no matches"))
            lb.itemconfigure(0, fg=T.DIM)

    def _update_current_model(self):
        if getattr(self, "current_model", None) and self.current_model.winfo_exists():
            current = self.settings["models"]["openrouter"]["main"]
            info = next((m for m in self.model_lists["openrouter"] if m["id"] == current), None)
            self.current_model.configure(text=current + (f"   {info['price']}" if info else ""))

    def _model_click(self, event):
        lb = self.model_list
        idx = lb.nearest(event.y) if event else (lb.curselection() or [None])[0]
        if idx is None or idx >= len(getattr(self, "visible", [])):
            return
        if event and event.x < self.char_w * 3:  # the ☆ column
            return self._toggle_star(idx)
        self.set_model("openrouter", self.visible[idx]["id"])
        self._fill_model_list()

    def _toggle_star(self, idx):
        if idx is None or idx >= len(getattr(self, "visible", [])):
            return
        mid = self.visible[idx]["id"]
        favs = self.settings["favorites"]
        favs.remove(mid) if mid in favs else favs.append(mid)
        backend.save_settings(self.settings)
        self._fill_model_list()

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
            self.write(f"switched to {label(p)}", "ok", log="system")
            self.write(f"── switched to {label(p)} ──", "ok", log="ai")
            if p in KEY_FIELD and not self.state.keys[p]:
                self.write(f"{label(p)} has no {KEY_FIELD[p]} yet - add one under settings", "warn", log="system")
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
        self.write(f"{PROVIDERS[p][0]} model → {model}", "ok", log="system")
        self._update_route()
        self._update_current_model()

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
        self.write(f"{label(p)} {KEY_FIELD[p]} {'saved' if key else 'removed'}", "ok" if key else "dim",
                   log="system")
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
        self.auto_btn.set_text(f"[{'x' if self.settings.get('autostart') else ' '}] auto-start")

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
            self.write(str(e), "err", log="system")
            return
        self.write(f"even terminal starting (pid {self.et.proc.pid})", "ok", log="system")
        self._watch(reschedule=False)

    def stop_et(self):
        if self.et.running:
            self.et.stop()
            self.write("even terminal stopped", "warn", log="system")
        self._watch(reschedule=False)

    # ---------- AI activity ----------
    def _on_ai(self, ev):
        kind, bg = ev["kind"], None
        if kind == "request":
            self.calls = getattr(self, "calls", {})
            self.calls[ev["rid"]] = ev
        call = getattr(self, "calls", {}).get(ev["rid"], {})
        bg = call.get("background", False)
        if bg:
            # Side jobs (titles, summaries, safety checks): only worth showing when they fail.
            if kind == "error":
                self.write(f"✗ background request failed: {ev['message']}", "err", log="ai")
            if kind in ("done", "error"):
                self.calls.pop(ev["rid"], None)
            return

        where = label(call.get("provider", "claude")) if call else ""
        if kind == "request":
            if ev["input"].startswith("you: "):
                self.write(ev["input"][5:], "you", log="ai", prefix="▸ ")
            elif ev["input"]:
                self.write(ev["input"], "dim", log="ai", prefix="↩ ")
            self.write(f"asking {where} · {ev['model']} (~{ev['tokens'] / 1000:.0f}k tokens)", "dim",
                       log="ai", prefix="⇢ ")
            self._ai_phase("waiting", ev["rid"], where)
        elif ev["rid"] != self.ai.get("rid"):
            if kind in ("done", "error"):  # an older call finishing; the status line follows the newest
                self.calls.pop(ev["rid"], None)
            return
        elif kind == "first":
            if ev["wait"] >= 8:
                self.write(f"first response after {ev['wait']:.0f}s", "dim", log="ai", prefix="· ")
            self._ai_phase("responding", ev["rid"], where)
        elif kind == "thinking":
            self._ai_phase("thinking", ev["rid"], where)
        elif kind == "thought":
            if ev["secs"] >= 1:
                self.write(f"thought for {ev['secs']:.0f}s", "dim", log="ai", prefix="◆ ")
            self._ai_phase("responding", ev["rid"], where)
        elif kind == "writing":
            self._ai_phase("writing", ev["rid"], where, f"{ev['chars']} chars")
        elif kind == "text":
            self.write(ev["text"], "info", log="ai", prefix="✎ ")
        elif kind == "tool":
            self.write(f"{ev['name']}  {ev['detail']}".rstrip(), "tool", log="ai", prefix="» ")
            self._ai_phase("tool", ev["rid"], where, ev["name"])
        elif kind == "done":
            out = f" · {ev['out_tokens']:,} tokens" if ev.get("out_tokens") else ""
            if ev.get("stop") == "tool_use":
                self.write(f"step done in {ev['secs']:.0f}s{out} → Claude Code is running the tool", "ok",
                           log="ai", prefix="✓ ")
                self._ai_phase("tool", ev["rid"], where, self.ai.get("detail") or "tool")
            else:
                why = {"max_tokens": " (hit the length limit)", "refusal": " (model refused)"}.get(ev.get("stop"), "")
                self.write(f"reply finished in {ev['secs']:.0f}s{out}{why}", "ok", log="ai", prefix="✓ ")
                self._ai_phase("idle", ev["rid"], where)
        elif kind == "error":
            self.write(ev["message"], "err", log="ai", prefix="✗ ")
            self._ai_phase("error", ev["rid"], where, ev["message"])
        if kind in ("done", "error"):
            self.calls.pop(ev["rid"], None)

    def _ai_phase(self, phase, rid, provider, detail=""):
        if phase != self.ai["phase"] or rid != self.ai["rid"]:
            self.ai["since"] = time.time()
        self.ai.update(phase=phase, rid=rid, provider=provider, detail=detail)
        self._tick_status(reschedule=False)

    def _tick_status(self, reschedule=True):
        a, now = self.ai, time.time()
        secs = int(now - a["since"])
        self.spin = (self.spin + 1) % len(SPINNER)
        s = SPINNER[self.spin]
        color = T.GREEN
        phase = a["phase"]
        if phase == "idle":
            text, color = ("○  idle - say something to Claude Code on your glasses"
                           if self.et.running else "○  idle - Even Terminal isn't running (press start)"), T.DIM
        elif phase == "waiting":
            text = f"{s}  waiting for {a['provider']} to start answering · {secs}s"
            if secs >= 45:
                color = T.AMBER
                text += {"ollama": " - loading the prompt into the local model, the first reply can take minutes",
                         "openrouter": " - free models can stall; if this keeps going, pick another model"}.get(
                    self.state.provider, " - still waiting on the provider")
        elif phase == "responding":
            text = f"{s}  {a['provider']} is responding · {secs}s"
        elif phase == "thinking":
            text = f"{s}  thinking · {secs}s"
        elif phase == "writing":
            text = f"{s}  writing a reply · {a['detail']}"
        elif phase == "tool":
            text, color = f"{s}  Claude Code is running {a['detail']} · {secs}s", T.CYAN
        else:
            text, color = f"✗  {a['detail'][:150]}", T.RED
        self.status.configure(text=text, fg=color)
        if reschedule:
            self.root.after(250, self._tick_status)

    # ---------- log / status ----------
    def write(self, text, tag="info", log="system", prefix=""):
        t = self.logs[log]
        t.configure(state="normal")
        t.insert("end", time.strftime("%H:%M:%S  "), "ts")
        if prefix:
            t.insert("end", prefix, (tag if tag != "you" else "ok", "indent"))
        t.insert("end", text + "\n", (tag, "indent"))
        lines = int(t.index("end-1c").split(".")[0])
        if lines > 3000:
            t.delete("1.0", f"{lines - 3000}.0")
        t.see("end")
        t.configure(state="disabled")

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
        if isinstance(event.widget, (tk.Entry, ttk.Combobox, tk.Listbox)):
            return
        if event.char and event.char in "1234":
            self.select(list(PROVIDERS)[int(event.char) - 1])

    def _pump(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "call":
                payload()
            elif kind == "ai":
                self._on_ai(payload)
            elif kind == "models":
                p, models = payload
                self.model_lists[p] = models
                if p == "ollama":
                    self.ollama_online = True
                    self._paint_cards()
                    self._fill_combo()
                else:
                    self._fill_model_list()
            elif kind == "models_failed":
                p, err = payload
                if p == "ollama":
                    self.ollama_online = False
                    self._paint_cards()
                    self.write("ollama isn't reachable on 127.0.0.1:11434 - is it running?", "warn", log="system")
                else:
                    self.write(f"couldn't list {p} models: {err}", "warn", log="system")
            elif kind == "keytest":
                p, ok, msg = payload
                self._set_key_msg(p, ("✓ " if ok else "✗ ") + msg, T.GREEN if ok else T.RED)
                self.write(f"{label(p)} test: {msg}", "ok" if ok else "err", log="system")
            elif kind == "relay":
                self.write("→ " + payload, self._relay_tag(payload), log="system")
            else:
                self.write(payload, "dim", log="system")
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


def blend_title_bar(root):
    """Paint the native Windows title bar in the app's colors (Windows 11; dark mode on 10).
    Keeps the real caption buttons, snapping and dragging."""
    def colorref(hex_color):
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        return ctypes.c_int(r | g << 8 | b << 16)
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        set_attr = ctypes.windll.dwmapi.DwmSetWindowAttribute
        for attr, value in ((20, ctypes.c_int(1)),         # DWMWA_USE_IMMERSIVE_DARK_MODE
                            (35, colorref(T.BG)),           # DWMWA_CAPTION_COLOR
                            (36, colorref(T.DIM)),          # DWMWA_TEXT_COLOR
                            (34, colorref(T.LINE))):        # DWMWA_BORDER_COLOR
            set_attr(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
    except (AttributeError, OSError):
        pass


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        if not getattr(sys, "frozen", False):
            # Running from source: give the window its own taskbar icon instead of Python's.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("G2Switcher.App")
    except (AttributeError, OSError):
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
    height = min(int(980 * scale), root.winfo_screenheight() - int(80 * scale))
    root.geometry(f"{int(760 * scale)}x{height}")
    root.minsize(int(640 * scale), min(int(720 * scale), height))
    icon = ImageTk.PhotoImage(T.make_icon(64))
    root.iconphoto(True, icon)
    blend_title_bar(root)
    App(root, state)
    root.mainloop()


if __name__ == "__main__":
    main()
