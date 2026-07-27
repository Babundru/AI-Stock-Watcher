import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox
import threading
import queue
import webbrowser
import datetime
import yfinance as yf
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from main import StockAppBackend
from portfolio_manager import PortfolioManager
from source_manager import SourceManager
from keyword_manager import KeywordManager

# --- FUTURISTIC THEME SETTINGS ---
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue") 
plt.style.use('dark_background')

# Colors
COLOR_BG = "#050505"       # Deep Void
COLOR_SIDEBAR = "#0F0F12"  # Dark Tech
COLOR_ACCENT = "#00E5FF"   # Neon Cyan
COLOR_ACCENT_HOVER = "#00B8D4"
COLOR_DANGER = "#FF2A6D"   # Neon Red/Pink
COLOR_SUCCESS = "#05FFA1"  # Neon Green
COLOR_TEXT = "#E0E0E0"
COLOR_TEXT_DIM = "#808080"
COLOR_PANEL = "#0B0B0E"   # Slightly lifted from the void, for cards
COLOR_LINE = "#242428"    # Hairline borders
COLOR_WARN = "#FFA500"
FONT_MONO = ("Consolas", 12)
FONT_HEAD = ("Segoe UI", 16, "bold")
FONT_DATA = ("Segoe UI", 13)

import sys

class ConsoleRedirector:
    def __init__(self, queue, tag=""):
        self.queue = queue
        self.tag = tag

    def write(self, message):
        if message.strip():
            self.queue.put(f"{self.tag}{message.strip()}")
    
    def flush(self):
        pass

class StockAppGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("STOCKS WATCHER // v2.1")
        self.geometry("1100x800") # Increased height for charts
        self.configure(fg_color=COLOR_BG) 

        self.portfolio_mgr = PortfolioManager()
        self.source_mgr = SourceManager()
        self.keyword_mgr = KeywordManager()
        self.backend = None
        self.log_queue = queue.Queue()
        # Backend callbacks fire on the worker thread; queue them and drain on
        # the UI thread rather than touching widgets from another thread.
        self.alert_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self.alerts = []
        self.show_ai_traffic = ctk.BooleanVar(value=False)
        self.autoscroll = ctk.BooleanVar(value=True)

        # Redirect Console
        sys.stdout = ConsoleRedirector(self.log_queue)
        sys.stderr = ConsoleRedirector(self.log_queue, "[ERROR] ")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- Sidebar (HUD Panel) ---
        self.sidebar_frame = ctk.CTkFrame(self, width=220, corner_radius=0, fg_color=COLOR_SIDEBAR)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_columnconfigure(0, weight=1)
        # Row 6 is an empty spacer: it pushes the live counters and the
        # diagnostic button to the bottom instead of leaving a dead gap.
        self.sidebar_frame.grid_rowconfigure(6, weight=1)

        # Logo
        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="STOCKS WATCHER\n[OWL_SYSTEM]",
                                      font=("Segoe UI", 20, "bold"), text_color=COLOR_ACCENT)
        self.logo_label.grid(row=0, column=0, padx=20, pady=(26, 14))

        # --- Status panel: state, active engine, and current activity ---
        status_box = ctk.CTkFrame(self.sidebar_frame, fg_color=COLOR_PANEL, corner_radius=0,
                                  border_width=1, border_color=COLOR_LINE)
        status_box.grid(row=1, column=0, padx=16, pady=(0, 18), sticky="ew")

        self.status_label = ctk.CTkLabel(status_box, text="● OFFLINE", text_color=COLOR_DANGER,
                                         font=("Consolas", 13, "bold"), anchor="w")
        self.status_label.pack(fill='x', padx=12, pady=(10, 2))

        self.engine_label = ctk.CTkLabel(status_box, text=self._engine_text(), text_color=COLOR_TEXT_DIM,
                                         font=("Consolas", 9), anchor="w", justify="left")
        self.engine_label.pack(fill='x', padx=12, pady=(0, 6))

        self.activity_label = ctk.CTkLabel(status_box, text="Idle", text_color=COLOR_ACCENT,
                                           font=("Consolas", 9), anchor="w", justify="left",
                                           wraplength=170)
        self.activity_label.pack(fill='x', padx=12, pady=(0, 10))

        # Controls
        self._create_sidebar_btn("INITIALIZE WATCHER", self.start_backend, COLOR_SUCCESS, 2)
        self._create_sidebar_btn("TERMINATE PROCESS", self.stop_backend, COLOR_DANGER, 3, state="disabled")
        self._create_sidebar_btn("RELOAD SETTINGS", self.reload_settings, COLOR_WARN, 4)
        self._create_sidebar_btn("SYSTEM OPTIONS", self.open_settings, COLOR_ACCENT, 5)

        # --- Live counters (bottom) ---
        stats_box = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        stats_box.grid(row=7, column=0, padx=16, pady=(0, 10), sticky="ew")
        stats_box.grid_columnconfigure((0, 1, 2), weight=1)
        self.stat_labels = {}
        for col, (key, caption, color) in enumerate([
            ('scanned', 'SCANNED', COLOR_TEXT),
            ('alerts', 'ALERTS', COLOR_SUCCESS),
            ('skipped', 'SKIPPED', COLOR_TEXT_DIM),
        ]):
            cell = ctk.CTkFrame(stats_box, fg_color="transparent")
            cell.grid(row=0, column=col, sticky="ew")
            value = ctk.CTkLabel(cell, text="0", text_color=color, font=("Consolas", 15, "bold"))
            value.pack()
            ctk.CTkLabel(cell, text=caption, text_color="#555", font=("Consolas", 7)).pack()
            self.stat_labels[key] = value

        self._create_sidebar_btn("SYSTEM_DIAGNOSTIC", self.send_test_alert, COLOR_TEXT, 8)

        # --- Main View ---
        self.main_area = ctk.CTkFrame(self, fg_color="transparent")
        self.main_area.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.main_area.grid_rowconfigure(1, weight=1)
        self.main_area.grid_columnconfigure(0, weight=1)

        # Styled Tabs
        self.tab_var =  ctk.StringVar(value="ALERTS")
        self.seg_button = ctk.CTkSegmentedButton(self.main_area, values=["ALERTS", "LOGS", "PORTFOLIO", "SOURCES", "KEYWORDS"],
                                                command=self.switch_tab,
                                                selected_color=COLOR_SIDEBAR,
                                                selected_hover_color=COLOR_SIDEBAR,
                                                unselected_color=COLOR_BG,
                                                unselected_hover_color="#1a1a1a",
                                                text_color=COLOR_ACCENT,
                                                corner_radius=0,
                                                border_width=1,
                                                font=("Consolas", 12, "bold"))
        self.seg_button.set("ALERTS")
        self.seg_button.grid(row=0, column=0, sticky="ew", pady=(0, 15))

        # -- Views --
        self.frame_alerts = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.frame_logs = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.frame_portfolio = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.frame_sources = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.frame_keywords = ctk.CTkFrame(self.main_area, fg_color="transparent")

        # Alerts is the default view - it is what the app exists to produce.
        self.frame_alerts.grid(row=1, column=0, sticky="nsew")

        # Tab: Terminal Logs (toolbar + colour-coded output)
        log_bar = ctk.CTkFrame(self.frame_logs, fg_color=COLOR_PANEL, corner_radius=0,
                               border_width=1, border_color=COLOR_LINE)
        log_bar.pack(fill='x', pady=(0, 8))

        ctk.CTkLabel(log_bar, text="SYSTEM LOG", font=("Segoe UI", 13, "bold"),
                     text_color=COLOR_ACCENT).pack(side='left', padx=16, pady=8)

        ctk.CTkButton(log_bar, text="CLEAR", command=self.clear_log, width=80,
                      fg_color="transparent", border_width=1, border_color="#444",
                      text_color=COLOR_TEXT_DIM, hover_color="#222", corner_radius=0,
                      font=("Consolas", 10, "bold")).pack(side='right', padx=10)

        ctk.CTkCheckBox(log_bar, text="AI TRAFFIC", variable=self.show_ai_traffic,
                        onvalue=True, offvalue=False, checkbox_width=16, checkbox_height=16,
                        fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                        text_color=COLOR_TEXT_DIM, font=("Consolas", 10),
                        corner_radius=0, border_color="#444").pack(side='right', padx=10)

        ctk.CTkCheckBox(log_bar, text="AUTOSCROLL", variable=self.autoscroll,
                        onvalue=True, offvalue=False, checkbox_width=16, checkbox_height=16,
                        fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                        text_color=COLOR_TEXT_DIM, font=("Consolas", 10),
                        corner_radius=0, border_color="#444").pack(side='right', padx=10)

        self.log_area = ctk.CTkTextbox(self.frame_logs, state='disabled',
                                      font=("Consolas", 11),
                                      fg_color="#000000",
                                      text_color=COLOR_TEXT_DIM,
                                      border_color=COLOR_SIDEBAR, border_width=2, corner_radius=0)
        self.log_area.pack(expand=True, fill='both')
        self._init_log_tags()

        # Tab 2: Portfolio Charts Init
        self.figure_pie = None
        self.figure_bar = None
        self.canvas_pie = None
        self.canvas_bar = None
        
        self._setup_alerts_tab()
        self._setup_portfolio_tab()
        self._setup_sources_tab()
        self._setup_keywords_tab()

        self.after(100, self.process_log_queue)
        self.refresh_portfolio_list()

    # --- Alerts -----------------------------------------------------------

    def _engine_text(self):
        """Describe which analysis engine is active."""
        try:
            import config
            if config.USE_LOCAL_LLM:
                return f"ENGINE  LLM\n        {config.LOCAL_MODEL_NAME}"
            return "ENGINE  KEYWORDS\n        offline scoring"
        except Exception:
            return "ENGINE  unknown"

    def _setup_alerts_tab(self):
        """Alert history - the app's actual output, which used to only ever
        scroll past in the log."""
        self.frame_alerts.grid_rowconfigure(1, weight=1)
        self.frame_alerts.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(self.frame_alerts, fg_color=COLOR_PANEL, corner_radius=0,
                           border_width=1, border_color=COLOR_LINE)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        self.alerts_title = ctk.CTkLabel(bar, text="TRIGGERED ALERTS", font=("Segoe UI", 14, "bold"),
                                         text_color=COLOR_ACCENT)
        self.alerts_title.pack(side='left', padx=20, pady=10)

        ctk.CTkButton(bar, text="CLEAR", command=self.clear_alerts, width=90,
                      fg_color="transparent", border_width=1, border_color=COLOR_DANGER,
                      text_color=COLOR_DANGER, hover_color="#220000", corner_radius=0,
                      font=("Consolas", 11, "bold")).pack(side='right', padx=10)

        self.alerts_list = ctk.CTkScrollableFrame(self.frame_alerts, fg_color="transparent",
                                                  corner_radius=0)
        self.alerts_list.grid(row=1, column=0, sticky="nsew")
        self.refresh_alerts_list()

    def add_alert(self, alert):
        """Called from the backend thread - queue for the UI thread."""
        self.alert_queue.put(alert)

    def clear_alerts(self):
        self.alerts = []
        self.refresh_alerts_list()

    def refresh_alerts_list(self):
        for widget in self.alerts_list.winfo_children():
            widget.destroy()

        self.alerts_title.configure(
            text=f"TRIGGERED ALERTS  ({len(self.alerts)})" if self.alerts else "TRIGGERED ALERTS"
        )

        if not self.alerts:
            empty = ctk.CTkFrame(self.alerts_list, fg_color="transparent")
            empty.pack(expand=True, fill='both', pady=70)
            ctk.CTkLabel(empty, text="NO ALERTS YET", font=("Consolas", 16, "bold"),
                         text_color="#333").pack()
            ctk.CTkLabel(empty,
                         text="Alerts appear here when the analyzer finds\n"
                              "HIGH or CRITICAL impact news.\n\n"
                              "Most articles correctly produce no alert -\n"
                              "check the LOGS tab to confirm it is scanning.",
                         font=("Consolas", 10), text_color="#444", justify="center").pack(pady=12)
            return

        # Newest first
        for alert in reversed(self.alerts):
            self._build_alert_card(alert)

    def _build_alert_card(self, alert):
        positive = alert.get('sentiment') == 'POSITIVE'
        accent = COLOR_SUCCESS if positive else COLOR_DANGER

        card = ctk.CTkFrame(self.alerts_list, fg_color=COLOR_PANEL, corner_radius=0,
                            border_width=1, border_color=COLOR_LINE)
        card.pack(fill='x', pady=4, padx=2)

        # Sentiment stripe down the left edge
        stripe = ctk.CTkFrame(card, fg_color=accent, width=4, corner_radius=0)
        stripe.pack(side='left', fill='y')

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(side='left', fill='both', expand=True, padx=14, pady=10)

        header = ctk.CTkFrame(body, fg_color="transparent")
        header.pack(fill='x')

        ticker = alert.get('ticker')
        name = alert.get('company') or 'Unknown'
        title = f"{name} ({ticker})" if ticker else name
        ctk.CTkLabel(header, text=title, font=("Segoe UI", 14, "bold"),
                     text_color="#FFF", anchor="w").pack(side='left')

        impact = alert.get('impact', 'HIGH')
        badge_color = COLOR_DANGER if impact == 'CRITICAL' else COLOR_WARN
        ctk.CTkLabel(header, text=f" {impact} ", font=("Consolas", 9, "bold"),
                     text_color="#000", fg_color=badge_color, corner_radius=0).pack(side='left', padx=8)

        ctk.CTkLabel(header, text=f"{'▲' if positive else '▼'} {alert.get('sentiment', '')}",
                     font=("Consolas", 10, "bold"), text_color=accent).pack(side='left', padx=4)

        if alert.get('is_owned'):
            ctk.CTkLabel(header, text=" OWNED ", font=("Consolas", 9, "bold"),
                         text_color="#000", fg_color=COLOR_ACCENT, corner_radius=0).pack(side='left', padx=6)

        stamp = alert.get('time')
        stamp_text = stamp.strftime('%H:%M:%S') if hasattr(stamp, 'strftime') else str(stamp or '')
        ctk.CTkLabel(header, text=stamp_text, font=("Consolas", 10),
                     text_color="#555").pack(side='right')

        headline = alert.get('headline') or ''
        if headline:
            ctk.CTkLabel(body, text=headline, font=("Segoe UI", 11), text_color=COLOR_TEXT_DIM,
                         anchor="w", justify="left", wraplength=740).pack(fill='x', pady=(6, 0))

        explanation = (alert.get('explanation') or '').strip()
        if explanation:
            ctk.CTkLabel(body, text=explanation, font=("Segoe UI", 11), text_color=COLOR_TEXT,
                         anchor="w", justify="left", wraplength=740).pack(fill='x', pady=(6, 0))

        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.pack(fill='x', pady=(8, 0))

        prediction = alert.get('prediction')
        if prediction:
            ctk.CTkLabel(footer, text=f"PREDICTION: {prediction}", font=("Consolas", 10, "bold"),
                         text_color=accent).pack(side='left')

        url = alert.get('url')
        if url:
            ctk.CTkButton(footer, text="OPEN ARTICLE ↗", width=130, height=26,
                          command=lambda u=url: webbrowser.open(u),
                          fg_color="transparent", border_width=1, border_color=COLOR_ACCENT,
                          text_color=COLOR_ACCENT, hover_color="#00232B", corner_radius=0,
                          font=("Consolas", 10, "bold")).pack(side='right')

    # --- Log rendering ----------------------------------------------------

    def _init_log_tags(self):
        """Colour-code log output. Everything used to render identical green,
        so a CRITICAL alert looked exactly like 'already processed'."""
        try:
            box = self.log_area._textbox
        except AttributeError:
            return
        box.tag_config('alert', foreground=COLOR_SUCCESS)
        box.tag_config('error', foreground=COLOR_DANGER)
        box.tag_config('warn', foreground=COLOR_WARN)
        box.tag_config('head', foreground="#FFFFFF")
        box.tag_config('ai', foreground="#4A4A6A")
        box.tag_config('muted', foreground="#4F4F4F")
        box.tag_config('info', foreground=COLOR_TEXT_DIM)
        box.tag_config('accent', foreground=COLOR_ACCENT)

    @staticmethod
    def _classify_log(msg):
        """Pick a tag for a log line, and whether it is AI traffic."""
        text = str(msg)
        stripped = text.lstrip()
        is_ai = ('SENT TO AI' in text or 'AI RESPONSE' in text)
        if is_ai:
            return 'ai', True
        if stripped.startswith('[ERROR]') or '!! ERROR' in text or 'Error' in text or 'error' in text:
            return 'error', False
        if 'ALERT' in text or stripped.startswith('🚀'):
            return 'alert', False
        if 'WARNING' in text or stripped.startswith('⚠') or 'Warning' in text:
            return 'warn', False
        if stripped.startswith('📰') or 'Processing article' in text:
            return 'head', False
        if stripped.startswith('⊘') or 'Already processed' in text or 'No notification' in text:
            return 'muted', False
        if 'Scanning for news' in text or 'Market Status' in text:
            return 'accent', False
        return 'info', False

    def clear_log(self):
        self.log_area.configure(state='normal')
        self.log_area.delete("1.0", tk.END)
        self.log_area.configure(state='disabled')

    def _create_sidebar_btn(self, text, cmd, glow_color, row, state="normal"):
        btn = ctk.CTkButton(self.sidebar_frame, text=text, command=cmd, state=state,
                           fg_color="transparent", border_width=1, border_color=glow_color,
                           text_color=glow_color, hover_color="#222", corner_radius=0,
                           font=("Consolas", 11, "bold"), height=35)
        btn.grid(row=row, column=0, padx=20, pady=10, sticky="ew")
        setattr(self, f"btn_{row}", btn) 
        return btn

    def open_settings(self):
        win = ctk.CTkToplevel(self)
        win.title("SYSTEM CONFIGURATION")
        win.geometry("500x520")
        win.configure(fg_color=COLOR_BG)
        win.transient(self)
        
        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(expand=True, fill='both', padx=10, pady=10)
        
        ctk.CTkLabel(scroll, text="NOTIFICATION SETTINGS", font=("Segoe UI", 16, "bold"), text_color=COLOR_ACCENT).pack(pady=(10, 20))
        
        def add_input(label, default_val):
            ctk.CTkLabel(scroll, text=label, text_color="#AAA", font=("Consolas", 11)).pack(anchor="w", padx=20, pady=(10,0))
            entry = ctk.CTkEntry(scroll, width=350, fg_color="#0F0F12", border_color="#333", text_color="#FFF", font=("Consolas", 12))
            entry.pack(pady=(5,0))
            if default_val:
                entry.insert(0, str(default_val))
            return entry
            
        try:
            import config
            current_topic = config.NTFY_TOPIC
            current_model = config.LOCAL_MODEL_NAME
            current_threads = config.OLLAMA_NUM_THREADS
        except:
            current_topic = "stocks_ai_secret"
            current_model = "phi3:mini"
            current_threads = 1

        e_topic = add_input("NTFY NOTIFICATION TOPIC:", current_topic)

        ctk.CTkLabel(scroll, text="ℹ️ Change this to a unique value for privacy",
                    text_color="#666", font=("Consolas", 9)).pack(pady=(5, 20))

        ctk.CTkLabel(scroll, text="LOCAL AI MODEL", font=("Segoe UI", 16, "bold"),
                    text_color=COLOR_ACCENT).pack(pady=(10, 10))

        e_model = add_input("OLLAMA MODEL NAME:", current_model)
        e_threads = add_input("OLLAMA THREADS:", current_threads)

        ctk.CTkLabel(scroll, text="ℹ️ Model must be pulled first: ollama pull <name>",
                    text_color="#666", font=("Consolas", 9)).pack(pady=(5, 20))

        def save():
            import json
            new_settings = {
                "NTFY_TOPIC": e_topic.get().strip(),
                "LOCAL_MODEL_NAME": e_model.get().strip(),
                "OLLAMA_NUM_THREADS": e_threads.get().strip()
            }
            try:
                with open("data/settings.json", "w") as f:
                    json.dump(new_settings, f, indent=4)
                messagebox.showinfo("System Update", "Saved. Please RESTART app/watcher.")
                win.destroy()
            except Exception as e:
                messagebox.showerror("Error", f"Save failed: {e}")

        ctk.CTkButton(scroll, text="SAVE CONFIGURATION", command=save, 
                     fg_color=COLOR_ACCENT, text_color="#000", hover_color=COLOR_ACCENT_HOVER,
                     font=("Consolas", 12, "bold"), width=300).pack(pady=40)

    def switch_tab(self, value):
        frames = {
            "ALERTS": self.frame_alerts,
            "LOGS": self.frame_logs,
            "PORTFOLIO": self.frame_portfolio,
            "SOURCES": self.frame_sources,
            "KEYWORDS": self.frame_keywords,
        }
        for frame in frames.values():
            frame.grid_forget()
        target = frames.get(value)
        if target:
            target.grid(row=1, column=0, sticky="nsew")

    def log_callback(self, msg):
        self.log_queue.put(msg)

    # The watcher is meant to run for days; without a cap the log textbox
    # grows until it exhausts memory.
    MAX_LOG_LINES = 2000

    def process_log_queue(self):
        drained = False
        while not self.log_queue.empty():
            msg = self.log_queue.get()
            tag, is_ai = self._classify_log(msg)
            # The LLM prompt/response dumps are enormous and drown everything
            # else, so they are hidden unless explicitly requested.
            if is_ai and not self.show_ai_traffic.get():
                continue
            self.log_area.configure(state='normal')
            start = self.log_area.index("end-1c")
            self.log_area.insert(tk.END, f"> {msg}\n")
            try:
                self.log_area._textbox.tag_add(tag, start, self.log_area.index("end-1c"))
            except AttributeError:
                pass
            drained = True

        if drained:
            line_count = int(self.log_area.index("end-1c").split('.')[0])
            if line_count > self.MAX_LOG_LINES:
                trim_to = line_count - self.MAX_LOG_LINES
                self.log_area.delete("1.0", f"{trim_to}.0")
            if self.autoscroll.get():
                self.log_area.see(tk.END)
            self.log_area.configure(state='disabled')

        # Drain alerts raised by the backend thread
        new_alerts = False
        while not self.alert_queue.empty():
            self.alerts.append(self.alert_queue.get())
            new_alerts = True
        if new_alerts:
            self.refresh_alerts_list()

        # Latest status wins; discard any backlog
        latest = None
        while not self.status_queue.empty():
            latest = self.status_queue.get()
        if latest:
            activity, stats = latest
            self.activity_label.configure(text=activity)
            for key, label in self.stat_labels.items():
                label.configure(text=str(stats.get(key, 0)))

        self.after(100, self.process_log_queue)

    def start_backend(self):
        if self.backend and self.backend.running: return
        self.log_queue.put("Initializing System Core...")
        self.backend = StockAppBackend(
            log_callback=self.log_callback,
            alert_callback=self.add_alert,
            status_callback=lambda text, stats: self.status_queue.put((text, stats)),
        )
        self.backend.start()
        self.btn_2.configure(state='disabled', border_color="#333", text_color="#333")
        self.btn_3.configure(state='normal', border_color=COLOR_DANGER, text_color=COLOR_DANGER)
        self.status_label.configure(text="● ONLINE", text_color=COLOR_SUCCESS)
        self.engine_label.configure(text=self._engine_text())
        self.activity_label.configure(text="Starting up...")

    def stop_backend(self):
        if self.backend:
            self.log_queue.put("Terminating System Core...")
            self.backend.stop()
            self.backend = None
        self.btn_2.configure(state='normal', border_color=COLOR_SUCCESS, text_color=COLOR_SUCCESS)
        self.btn_3.configure(state='disabled', border_color="#333", text_color="#333")
        self.status_label.configure(text="● OFFLINE", text_color=COLOR_DANGER)
        self.activity_label.configure(text="Idle")

    def send_test_alert(self):
        try:
            from notifier import Notifier
            n = Notifier()
            self.log_queue.put("Running Diagnostic...")
            dummy_article = {'title': 'System Diagnostic Test', 'url': 'about:blank'}
            dummy_analysis = {
                'ticker': 'TEST_SYS', 'sentiment': 'POSITIVE', 'impact': 'HIGH',
                'prediction': 'OPTIMAL', 'explanation': 'Diagnostic check passed.'
            }
            n.notify("SYSTEM DIAGNOSTIC", dummy_article, dummy_analysis)
            messagebox.showinfo("Diagnostic", "Signal sent.")
        except Exception as e:
            self.log_queue.put(f"Diagnostic Error: {e}")

    def reload_settings(self):
        """Reload keywords, sources, and settings without restarting the watcher."""
        try:
            self.log_queue.put("Reloading system settings...")
            
            # Reload keyword manager
            self.keyword_mgr = KeywordManager()
            self.refresh_keywords_list()
            self.log_queue.put("✓ Keywords reloaded")
            
            # Reload source manager
            self.source_mgr = SourceManager()
            self.refresh_sources_list()
            self.log_queue.put("✓ Sources reloaded")
            
            # Reload config module
            import importlib
            import config
            importlib.reload(config)
            self.log_queue.put("✓ Config reloaded")
            
            # If backend is running, reload analyzer keywords
            # The LLM analyzer has no keyword table to reload.
            if self.backend and hasattr(self.backend.analyzer, 'reload_keywords'):
                self.backend.analyzer.reload_keywords()
                self.log_queue.put("✓ Analyzer updated with new keywords")
            
            messagebox.showinfo("Reload Complete", "Settings, keywords, and sources have been reloaded successfully!")
            self.log_queue.put("System settings reload complete.")
            
        except Exception as e:
            self.log_queue.put(f"Reload Error: {e}")
            messagebox.showerror("Reload Error", f"Failed to reload settings: {e}")

    def _setup_portfolio_tab(self):
        self.frame_portfolio.grid_rowconfigure(0, weight=0)
        self.frame_portfolio.grid_rowconfigure(1, weight=1)
        self.frame_portfolio.grid_rowconfigure(2, weight=1)
        self.frame_portfolio.grid_columnconfigure(0, weight=1)

        # Top Controls
        bar = ctk.CTkFrame(self.frame_portfolio, fg_color="#0F0F12", corner_radius=0, border_width=1, border_color="#333")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        
        self.entry_ticker = ctk.CTkEntry(bar, placeholder_text="TICKER", width=120,
                                        fg_color="#000", border_color="#333", text_color=COLOR_ACCENT, corner_radius=0, font=("Consolas", 12))
        self.entry_ticker.pack(side='left', padx=10, pady=10)
        
        self.entry_price = ctk.CTkEntry(bar, placeholder_text="BUY PRICE", width=100,
                                        fg_color="#000", border_color="#333", text_color="#FFF", corner_radius=0, font=("Consolas", 12))
        self.entry_price.pack(side='left', padx=(0, 10), pady=10)
        
        ctk.CTkButton(bar, text="+ ADD", command=self.add_to_portfolio, width=80,
                     fg_color=COLOR_ACCENT, text_color="#000", hover_color=COLOR_ACCENT_HOVER, corner_radius=0, font=("Consolas", 11, "bold")).pack(side='left', padx=10)

        ctk.CTkButton(bar, text="RESET DATA", command=self.reset_portfolio, width=100,
                     fg_color="transparent", border_width=1, border_color=COLOR_DANGER, text_color=COLOR_DANGER,
                     hover_color="#220000", corner_radius=0, font=("Consolas", 11, "bold")).pack(side='right', padx=10)

        ctk.CTkButton(bar, text="REFRESH", command=self.refresh_portfolio_list, width=80,
                     fg_color="transparent", border_width=1, border_color="#666", text_color="#AAA", corner_radius=0, font=("Consolas", 11)).pack(side='right', padx=10)

        # List Container
        self.frame_list_container = ctk.CTkFrame(self.frame_portfolio, fg_color="transparent")
        self.frame_list_container.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        
        headers = [("TICKER", 80), ("BUY PRICE", 100), ("LIVE PRICE", 100), ("P/L %", 100), ("GAIN/LOSS", 100)]
        header_inner = ctk.CTkFrame(self.frame_list_container, fg_color="transparent")
        header_inner.pack(fill='x')
        for text, width in headers:
             ctk.CTkLabel(header_inner, text=text, width=width, anchor="w", font=("Consolas", 11, "bold"), text_color="#666").pack(side='left', padx=10)

        self.portfolio_list_frame = ctk.CTkScrollableFrame(self.frame_list_container, fg_color="transparent", corner_radius=0, height=200)
        self.portfolio_list_frame.pack(expand=True, fill='both')

        # Charts Area
        self.frame_charts = ctk.CTkFrame(self.frame_portfolio, fg_color="#080808", corner_radius=0)
        self.frame_charts.grid(row=2, column=0, sticky="nsew", padx=10, pady=10)
        self.frame_charts.grid_columnconfigure(0, weight=1)
        self.frame_charts.grid_columnconfigure(1, weight=1)
        self.frame_charts.grid_rowconfigure(0, weight=1)
        
        self.chart_frame_left = ctk.CTkFrame(self.frame_charts, fg_color="transparent")
        self.chart_frame_left.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        
        self.chart_frame_right = ctk.CTkFrame(self.frame_charts, fg_color="transparent")
        self.chart_frame_right.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)

    def add_to_portfolio(self):
        ticker = self.entry_ticker.get().strip().upper()
        price_str = self.entry_price.get().strip()
        if not ticker: return
        try:
            buy_price = float(price_str) if price_str else 0.0
        except ValueError:
            messagebox.showerror("Error", "Invalid Buy Price")
            return
        if self.portfolio_mgr.add_stock(ticker, buy_price):
            self.log_queue.put(f"Target Acquired: {ticker} @ ${buy_price}")
            self.entry_ticker.delete(0, tk.END)
            self.entry_price.delete(0, tk.END)
            self.refresh_portfolio_list()
        else:
            self.log_queue.put(f"Updated Target: {ticker}")
            self.refresh_portfolio_list()

    def reset_portfolio(self):
        if messagebox.askyesno("Confirm Reset", "Are you sure you want to WIPE ALL portfolio data?"):
            self.portfolio_mgr.reset_portfolio()
            self.log_queue.put("SYSTEM ALERT: Portfolio Database Purged.")
            self.refresh_portfolio_list()

    def delete_stock(self, ticker):
        self.portfolio_mgr.remove_stock(ticker)
        self.log_queue.put(f"Target Dropped: {ticker}")
        self.refresh_portfolio_list(fetch_prices=False)

    def refresh_portfolio_list(self, fetch_prices=True):
        for widget in self.portfolio_list_frame.winfo_children():
            widget.destroy()
        portfolio = self.portfolio_mgr.get_portfolio()
        self.portfolio_rows = {} 
        if not portfolio:
             ctk.CTkLabel(self.portfolio_list_frame, text="NO TARGETS ACQUIRED", font=("Consolas", 14), text_color="#444").pack(pady=40)
             self._update_charts({})
             return

        for ticker, data in portfolio.items():
            buy_price = data.get('buy_price', 0.0)
            row = ctk.CTkFrame(self.portfolio_list_frame, fg_color="#0F0F12", corner_radius=0, border_color="#333", border_width=1)
            row.pack(fill='x', pady=2)
            ctk.CTkFrame(row, fg_color=COLOR_ACCENT, width=4, height=40, corner_radius=0).pack(side='left', fill='y')
            ctk.CTkLabel(row, text=ticker, width=80, font=("Consolas", 14, "bold"), text_color="#FFF").pack(side='left', padx=10)
            ctk.CTkLabel(row, text=f"${buy_price:.2f}", width=100, font=("Consolas", 13), text_color="#AAA").pack(side='left', padx=10)
            lbl_live = ctk.CTkLabel(row, text="---", width=100, font=("Consolas", 13), text_color="#FFF")
            lbl_live.pack(side='left', padx=10)
            lbl_pct = ctk.CTkLabel(row, text="---", width=100, font=("Consolas", 13, "bold"))
            lbl_pct.pack(side='left', padx=10)
            lbl_gain = ctk.CTkLabel(row, text="---", width=100, font=("Consolas", 13))
            lbl_gain.pack(side='left', padx=10)
            ctk.CTkButton(row, text="×", width=30, height=30, fg_color="transparent", text_color="#666", hover_color="#222", 
                         font=("Arial", 16), command=lambda t=ticker: self.delete_stock(t)).pack(side='right', padx=10)
            self.portfolio_rows[ticker] = {'lbl_live': lbl_live, 'lbl_pct': lbl_pct, 'lbl_gain': lbl_gain, 'buy_price': buy_price}
        
        if fetch_prices:
            threading.Thread(target=self._fetch_and_update_prices, args=(list(portfolio.keys()),), daemon=True).start()
        else:
             self._update_charts(self._mock_chart_data(portfolio)) if not fetch_prices else None

    def _mock_chart_data(self, portfolio):
        # Quick helper to preserve prev values or show 0 if no fetch available yet
        return {t: {'buy': d['buy_price'], 'current': d['buy_price'], 'pl_pct': 0.0} for t, d in portfolio.items()}

    def _fetch_and_update_prices(self, tickers):
        data_map = {}
        try:
            if not tickers: return
            tickers_str = " ".join(tickers)
            data = yf.Tickers(tickers_str)
            for ticker in tickers:
                try:
                    # fast_info is a lightweight quote lookup; .info pulls the
                    # full company profile and is far slower per ticker.
                    price = None
                    try:
                        price = data.tickers[ticker].fast_info.get('lastPrice')
                    except Exception:
                        price = None
                    if not price:
                        info = data.tickers[ticker].info
                        price = info.get('currentPrice') or info.get('regularMarketPrice')
                    data_map[ticker] = float(price) if price else None
                except Exception:
                    data_map[ticker] = None
        except Exception as e:
            print(f"Price fetch failed: {e}")
        # The window may have been closed while this thread was waiting on the
        # network; scheduling onto a destroyed widget raises RuntimeError.
        try:
            if self.winfo_exists():
                self.after(0, lambda: self._update_ui_prices(data_map))
        except Exception:
            pass

    def _update_ui_prices(self, price_map):
        portfolio_data_for_charts = {}
        for ticker, live_price in price_map.items():
            if ticker in self.portfolio_rows:
                row_data = self.portfolio_rows[ticker]
                lbl_live = row_data['lbl_live']
                lbl_pct = row_data['lbl_pct']
                lbl_gain = row_data['lbl_gain']
                buy_price = row_data['buy_price']
                
                if live_price:
                    lbl_live.configure(text=f"${live_price:.2f}")
                    if buy_price > 0:
                        diff = live_price - buy_price
                        pct = (diff / buy_price) * 100
                        color = COLOR_SUCCESS if diff >= 0 else COLOR_DANGER
                        prefix = "+" if diff >= 0 else ""
                        lbl_pct.configure(text=f"{prefix}{pct:.2f}%", text_color=color)
                        lbl_gain.configure(text=f"{prefix}${diff:.2f}", text_color=color)
                        portfolio_data_for_charts[ticker] = {'buy': buy_price, 'current': live_price, 'pl_pct': pct}
                    else:
                        lbl_pct.configure(text="N/A", text_color="#666")
                        lbl_gain.configure(text="Log Price", text_color="#666")
                        portfolio_data_for_charts[ticker] = {'buy': 0, 'current': live_price, 'pl_pct': 0}
                else:
                    lbl_live.configure(text="ERR", text_color=COLOR_DANGER)
        self._update_charts(portfolio_data_for_charts)

    def _update_charts(self, data):
        if not data: return # Maybe clear
        tickers = list(data.keys())
        current_vals = [d['current'] for d in data.values()]
        pl_pcts = [d['pl_pct'] for d in data.values()]
        colors = [COLOR_SUCCESS if p >= 0 else COLOR_DANGER for p in pl_pcts]
        
        # Pie
        if self.figure_pie: self.figure_pie.clear()
        else: self.figure_pie = plt.Figure(figsize=(4, 3), dpi=80, facecolor="#080808")
        ax_pie = self.figure_pie.add_subplot(111)
        ax_pie.pie(current_vals, labels=tickers, autopct='%1.1f%%', startangle=90, 
                  colors=['#00E5FF', '#05FFA1', '#FF2A6D', '#FFFF00', '#FFFFFF', '#888888'],
                  textprops={'color':"w"})
        # Honest label: the portfolio stores a buy price but no share count,
        # so this is the relative share price of each holding - NOT how much
        # money is in each. Calling it "allocation" implied otherwise.
        ax_pie.set_title("SHARE PRICE WEIGHT (not position size)", color="white", fontsize=9)
        
        if self.canvas_pie: self.canvas_pie.draw()
        else:
            self.canvas_pie = FigureCanvasTkAgg(self.figure_pie, master=self.chart_frame_left)
            self.canvas_pie.draw()
            self.canvas_pie.get_tk_widget().pack(fill='both', expand=True)

        # Bar
        if self.figure_bar: self.figure_bar.clear()
        else: self.figure_bar = plt.Figure(figsize=(4, 3), dpi=80, facecolor="#080808")
        ax_bar = self.figure_bar.add_subplot(111)
        ax_bar.set_facecolor("#111")
        bars = ax_bar.bar(tickers, pl_pcts, color=colors)
        ax_bar.axhline(0, color='white', linewidth=0.5)
        ax_bar.set_title("PERFORMANCE (P/L %)", color="white", fontsize=10)
        ax_bar.tick_params(axis='x', colors='white')
        ax_bar.tick_params(axis='y', colors='white')
        ax_bar.spines['bottom'].set_color('white')
        ax_bar.spines['left'].set_color('white')
        ax_bar.spines['top'].set_color('#333')
        ax_bar.spines['right'].set_color('#333')
        
        if self.canvas_bar: self.canvas_bar.draw()
        else:
            self.canvas_bar = FigureCanvasTkAgg(self.figure_bar, master=self.chart_frame_right)
            self.canvas_bar.draw()
            self.canvas_bar.get_tk_widget().pack(fill='both', expand=True)

    def _setup_sources_tab(self):
        """Setup the Sources management tab."""
        self.frame_sources.grid_rowconfigure(1, weight=1)
        self.frame_sources.grid_columnconfigure(0, weight=1)
        
        # Top Controls
        bar = ctk.CTkFrame(self.frame_sources, fg_color="#0F0F12", corner_radius=0, border_width=1, border_color="#333")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        
        self.entry_source_name = ctk.CTkEntry(bar, placeholder_text="Source Name", width=200,
                                             fg_color="#000", border_color="#333", text_color=COLOR_ACCENT, corner_radius=0, font=("Consolas", 12))
        self.entry_source_name.pack(side='left', padx=10, pady=10)
        
        self.entry_source_url = ctk.CTkEntry(bar, placeholder_text="Source URL", width=400,
                                            fg_color="#000", border_color="#333", text_color="#FFF", corner_radius=0, font=("Consolas", 12))
        self.entry_source_url.pack(side='left', padx=(0, 10), pady=10)
        
        ctk.CTkButton(bar, text="+ ADD SOURCE", command=self.add_source, width=120,
                     fg_color=COLOR_ACCENT, text_color="#000", hover_color=COLOR_ACCENT_HOVER, corner_radius=0, font=("Consolas", 11, "bold")).pack(side='left', padx=10)
        
        ctk.CTkButton(bar, text="RESET TO DEFAULTS", command=self.reset_sources, width=140,
                     fg_color="transparent", border_width=1, border_color=COLOR_DANGER, text_color=COLOR_DANGER,
                     hover_color="#220000", corner_radius=0, font=("Consolas", 11, "bold")).pack(side='right', padx=10)
        
        # Sources List
        list_frame = ctk.CTkScrollableFrame(self.frame_sources, fg_color="transparent", corner_radius=0)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        self.sources_list_frame = list_frame
        
        self.refresh_sources_list()
    
    def add_source(self):
        """Add a new source."""
        name = self.entry_source_name.get().strip()
        url = self.entry_source_url.get().strip()
        
        if not name or not url:
            messagebox.showwarning("Input Required", "Please enter both name and URL")
            return
        
        try:
            self.source_mgr.add_source(name, url)
            self.log_queue.put(f"Added source: {name} ({url})")
            self.entry_source_name.delete(0, tk.END)
            self.entry_source_url.delete(0, tk.END)
            self.refresh_sources_list()
        except ValueError as e:
            messagebox.showerror("Invalid URL", str(e))
    
    def delete_source(self, source_id):
        """Delete a source."""
        if self.source_mgr.remove_source(source_id):
            self.log_queue.put(f"Removed source: {source_id}")
            self.refresh_sources_list()
    
    def toggle_source(self, source_id):
        """Toggle source enabled/disabled."""
        enabled = self.source_mgr.toggle_source(source_id)
        status = "enabled" if enabled else "disabled"
        self.log_queue.put(f"Source {status}")
        self.refresh_sources_list()
    
    def reset_sources(self):
        """Reset sources to defaults."""
        if messagebox.askyesno("Confirm Reset", "Reset to default sources?"):
            self.source_mgr.reset_to_defaults()
            self.log_queue.put("Sources reset to defaults")
            self.refresh_sources_list()
    
    def refresh_sources_list(self):
        """Refresh the sources list display."""
        for widget in self.sources_list_frame.winfo_children():
            widget.destroy()
        
        sources = self.source_mgr.get_sources()
        
        if not sources:
            ctk.CTkLabel(self.sources_list_frame, text="NO SOURCES CONFIGURED", font=("Consolas", 14), text_color="#444").pack(pady=40)
            return
        
        for source in sources:
            source_id = source['id']
            name = source['name']
            url = source['url']
            enabled = source.get('enabled', True)
            
            row = ctk.CTkFrame(self.sources_list_frame, fg_color="#0F0F12", corner_radius=0, border_color="#333", border_width=1)
            row.pack(fill='x', pady=3, padx=5)
            
            # Status indicator
            status_color = COLOR_SUCCESS if enabled else "#666"
            ctk.CTkFrame(row, fg_color=status_color, width=4, height=50, corner_radius=0).pack(side='left', fill='y')
            
            # Source info
            info_frame = ctk.CTkFrame(row, fg_color="transparent")
            info_frame.pack(side='left', fill='both', expand=True, padx=10, pady=5)
            
            ctk.CTkLabel(info_frame, text=name, font=("Consolas", 13, "bold"), text_color=COLOR_ACCENT, anchor="w").pack(anchor="w")
            ctk.CTkLabel(info_frame, text=url, font=("Consolas", 10), text_color="#888", anchor="w").pack(anchor="w")
            
            # Controls
            ctk.CTkButton(row, text="✓" if enabled else "○", width=40, height=40,
                         fg_color="transparent", text_color=status_color, hover_color="#222",
                         font=("Arial", 18, "bold"), command=lambda sid=source_id: self.toggle_source(sid)).pack(side='right', padx=5)
            
            ctk.CTkButton(row, text="×", width=40, height=40,
                         fg_color="transparent", text_color="#666", hover_color="#220000",
                         font=("Arial", 18), command=lambda sid=source_id: self.delete_source(sid)).pack(side='right', padx=5)

    def _setup_keywords_tab(self):
        """Setup the Keywords management tab."""
        self.frame_keywords.grid_rowconfigure(1, weight=1)
        self.frame_keywords.grid_columnconfigure(0, weight=1)
        self.frame_keywords.grid_columnconfigure(1, weight=1)
        
        # Top Controls
        bar = ctk.CTkFrame(self.frame_keywords, fg_color="#0F0F12", corner_radius=0, border_width=1, border_color="#333")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        
        ctk.CTkLabel(bar, text="KEYWORD MANAGEMENT", font=("Segoe UI", 14, "bold"), text_color=COLOR_ACCENT).pack(side='left', padx=20, pady=10)

        # These keywords only drive scoring when the LLM is switched off.
        # Without this notice, editing them while the LLM is active looks
        # like it should change the alerts, and silently does nothing.
        try:
            import config
            llm_active = config.USE_LOCAL_LLM
        except Exception:
            llm_active = False
        if llm_active:
            notice = ctk.CTkFrame(self.frame_keywords, fg_color="#2A1D05", corner_radius=0,
                                  border_width=1, border_color=COLOR_WARN)
            notice.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
            ctk.CTkLabel(notice,
                         text="⚠  INACTIVE - the local LLM is doing the analysis. "
                              "These keywords are only used when USE_LOCAL_LLM = False.",
                         font=("Consolas", 11), text_color=COLOR_WARN,
                         justify="left").pack(padx=16, pady=8)
            self.frame_keywords.grid_rowconfigure(1, weight=0)
            self.frame_keywords.grid_rowconfigure(2, weight=1)
            self._keyword_list_row = 2
        else:
            self._keyword_list_row = 1
        
        ctk.CTkButton(bar, text="RESET TO DEFAULTS", command=self.reset_keywords, width=140,
                     fg_color="transparent", border_width=1, border_color=COLOR_DANGER, text_color=COLOR_DANGER,
                     hover_color="#220000", corner_radius=0, font=("Consolas", 11, "bold")).pack(side='right', padx=10)
        
        # Positive Keywords Section
        pos_frame = ctk.CTkFrame(self.frame_keywords, fg_color="transparent")
        pos_frame.grid(row=self._keyword_list_row, column=0, sticky="nsew", padx=(10, 5), pady=5)
        
        # Positive Header
        pos_header = ctk.CTkFrame(pos_frame, fg_color="#0A2010", corner_radius=0, border_width=1, border_color=COLOR_SUCCESS)
        pos_header.pack(fill='x', pady=(0, 5))
        ctk.CTkLabel(pos_header, text="⬆ POSITIVE KEYWORDS", font=("Consolas", 12, "bold"), text_color=COLOR_SUCCESS).pack(pady=5)
        
        # Positive Input
        pos_input = ctk.CTkFrame(pos_frame, fg_color="#0F0F12", corner_radius=0)
        pos_input.pack(fill='x', pady=5)
        
        self.entry_pos_keyword = ctk.CTkEntry(pos_input, placeholder_text="Keyword", width=150,
                                              fg_color="#000", border_color="#333", text_color=COLOR_SUCCESS, corner_radius=0)
        self.entry_pos_keyword.pack(side='left', padx=5, pady=5)
        
        self.entry_pos_weight = ctk.CTkEntry(pos_input, placeholder_text="Weight (1-10)", width=100,
                                             fg_color="#000", border_color="#333", text_color="#FFF", corner_radius=0)
        self.entry_pos_weight.pack(side='left', padx=5, pady=5)
        
        ctk.CTkButton(pos_input, text="+ ADD", command=lambda: self.add_keyword("positive"), width=70,
                     fg_color=COLOR_SUCCESS, text_color="#000", hover_color="#03CC81", corner_radius=0, font=("Consolas", 10, "bold")).pack(side='left', padx=5)
        
        # Positive List
        self.pos_keywords_list = ctk.CTkScrollableFrame(pos_frame, fg_color="transparent", corner_radius=0)
        self.pos_keywords_list.pack(fill='both', expand=True, pady=5)
        
        # Negative Keywords Section
        neg_frame = ctk.CTkFrame(self.frame_keywords, fg_color="transparent")
        neg_frame.grid(row=self._keyword_list_row, column=1, sticky="nsew", padx=(5, 10), pady=5)
        
        # Negative Header
        neg_header = ctk.CTkFrame(neg_frame, fg_color="#201010", corner_radius=0, border_width=1, border_color=COLOR_DANGER)
        neg_header.pack(fill='x', pady=(0, 5))
        ctk.CTkLabel(neg_header, text="⬇ NEGATIVE KEYWORDS", font=("Consolas", 12, "bold"), text_color=COLOR_DANGER).pack(pady=5)
        
        # Negative Input
        neg_input = ctk.CTkFrame(neg_frame, fg_color="#0F0F12", corner_radius=0)
        neg_input.pack(fill='x', pady=5)
        
        self.entry_neg_keyword = ctk.CTkEntry(neg_input, placeholder_text="Keyword", width=150,
                                              fg_color="#000", border_color="#333", text_color=COLOR_DANGER, corner_radius=0)
        self.entry_neg_keyword.pack(side='left', padx=5, pady=5)
        
        self.entry_neg_weight = ctk.CTkEntry(neg_input, placeholder_text="Weight (1-10)", width=100,
                                             fg_color="#000", border_color="#333", text_color="#FFF", corner_radius=0)
        self.entry_neg_weight.pack(side='left', padx=5, pady=5)
        
        ctk.CTkButton(neg_input, text="+ ADD", command=lambda: self.add_keyword("negative"), width=70,
                     fg_color=COLOR_DANGER, text_color="#FFF", hover_color="#CC2055", corner_radius=0, font=("Consolas", 10, "bold")).pack(side='left', padx=5)
        
        # Negative List
        self.neg_keywords_list = ctk.CTkScrollableFrame(neg_frame, fg_color="transparent", corner_radius=0)
        self.neg_keywords_list.pack(fill='both', expand=True, pady=5)
        
        self.refresh_keywords_list()
    
    def add_keyword(self, keyword_type):
        """Add a new keyword (positive or negative)."""
        if keyword_type == "positive":
            keyword = self.entry_pos_keyword.get().strip()
            weight_str = self.entry_pos_weight.get().strip()
            entry_k = self.entry_pos_keyword
            entry_w = self.entry_pos_weight
        else:
            keyword = self.entry_neg_keyword.get().strip()
            weight_str = self.entry_neg_weight.get().strip()
            entry_k = self.entry_neg_keyword
            entry_w = self.entry_neg_weight
        
        if not keyword:
            messagebox.showwarning("Input Required", "Please enter a keyword")
            return
        
        try:
            weight = int(weight_str) if weight_str else 5
            if weight < 1 or weight > 10:
                raise ValueError("Weight must be between 1 and 10")
        except ValueError as e:
            messagebox.showerror("Invalid Weight", str(e))
            return
        
        if self.keyword_mgr.add_keyword(keyword, weight, keyword_type):
            self.log_queue.put(f"Added {keyword_type} keyword: {keyword} (weight: {weight})")
            entry_k.delete(0, tk.END)
            entry_w.delete(0, tk.END)
            self.refresh_keywords_list()
            # Reload keywords in analyzer
            # The LLM analyzer has no keyword table to reload.
            if self.backend and hasattr(self.backend.analyzer, 'reload_keywords'):
                self.backend.analyzer.reload_keywords()
    
    def delete_keyword(self, keyword, keyword_type):
        """Delete a keyword."""
        if self.keyword_mgr.remove_keyword(keyword, keyword_type):
            self.log_queue.put(f"Removed {keyword_type} keyword: {keyword}")
            self.refresh_keywords_list()
            # Reload keywords in analyzer
            # The LLM analyzer has no keyword table to reload.
            if self.backend and hasattr(self.backend.analyzer, 'reload_keywords'):
                self.backend.analyzer.reload_keywords()
    
    def reset_keywords(self):
        """Reset keywords to defaults."""
        if messagebox.askyesno("Confirm Reset", "Reset to default keywords?"):
            self.keyword_mgr.reset_to_defaults()
            self.log_queue.put("Keywords reset to defaults")
            self.refresh_keywords_list()
            # Reload keywords in analyzer
            # The LLM analyzer has no keyword table to reload.
            if self.backend and hasattr(self.backend.analyzer, 'reload_keywords'):
                self.backend.analyzer.reload_keywords()
    
    def refresh_keywords_list(self):
        """Refresh the keyword lists display."""
        # Clear existing
        for widget in self.pos_keywords_list.winfo_children():
            widget.destroy()
        for widget in self.neg_keywords_list.winfo_children():
            widget.destroy()
        
        # Positive keywords
        pos_keywords = self.keyword_mgr.get_positive_keywords()
        for keyword, weight in sorted(pos_keywords.items(), key=lambda x: -x[1]):  # Sort by weight descending
            row = ctk.CTkFrame(self.pos_keywords_list, fg_color="#0F0F12", corner_radius=0, border_color="#333", border_width=1)
            row.pack(fill='x', pady=2, padx=3)
            
            # Weight indicator (colored bar)
            bar_width = max(3, int(weight * 0.5))
            ctk.CTkFrame(row, fg_color=COLOR_SUCCESS, width=bar_width, height=30, corner_radius=0).pack(side='left', fill='y')
            
            ctk.CTkLabel(row, text=keyword, font=("Consolas", 11), text_color="#FFF", anchor="w", width=150).pack(side='left', padx=10)
            ctk.CTkLabel(row, text=f"+{weight}", font=("Consolas", 11, "bold"), text_color=COLOR_SUCCESS, width=50).pack(side='left')
            ctk.CTkButton(row, text="×", width=30, height=30, fg_color="transparent", text_color="#666", hover_color="#220000",
                         font=("Arial", 14), command=lambda k=keyword: self.delete_keyword(k, "positive")).pack(side='right', padx=5)
        
        if not pos_keywords:
            ctk.CTkLabel(self.pos_keywords_list, text="No positive keywords", font=("Consolas", 11), text_color="#444").pack(pady=20)
        
        # Negative keywords
        neg_keywords = self.keyword_mgr.get_negative_keywords()
        for keyword, weight in sorted(neg_keywords.items(), key=lambda x: x[1]):  # Sort by weight ascending (most negative first)
            row = ctk.CTkFrame(self.neg_keywords_list, fg_color="#0F0F12", corner_radius=0, border_color="#333", border_width=1)
            row.pack(fill='x', pady=2, padx=3)
            
            # Weight indicator (colored bar)
            bar_width = max(3, int(abs(weight) * 0.5))
            ctk.CTkFrame(row, fg_color=COLOR_DANGER, width=bar_width, height=30, corner_radius=0).pack(side='left', fill='y')
            
            ctk.CTkLabel(row, text=keyword, font=("Consolas", 11), text_color="#FFF", anchor="w", width=150).pack(side='left', padx=10)
            ctk.CTkLabel(row, text=f"{weight}", font=("Consolas", 11, "bold"), text_color=COLOR_DANGER, width=50).pack(side='left')
            ctk.CTkButton(row, text="×", width=30, height=30, fg_color="transparent", text_color="#666", hover_color="#220000",
                         font=("Arial", 14), command=lambda k=keyword: self.delete_keyword(k, "negative")).pack(side='right', padx=5)
        
        if not neg_keywords:
            ctk.CTkLabel(self.neg_keywords_list, text="No negative keywords", font=("Consolas", 11), text_color="#444").pack(pady=20)

if __name__ == "__main__":
    app = StockAppGUI()
    app.mainloop()
