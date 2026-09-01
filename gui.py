import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox
import threading
import queue
import webbrowser
import datetime
import price_lookup
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from main import StockAppBackend
from portfolio_manager import PortfolioManager
from paper_trader import PaperTrader
import config
from config import (PAPER_COST_PCT, PAPER_BENCHMARK,
                    PAPER_START_CAPITAL, PAPER_POSITION_PCT)
from source_manager import SourceManager
from keyword_manager import KeywordManager

# --- THEME ---------------------------------------------------------------
# Depth comes from layered surfaces rather than glowing 1px outlines, and
# colour is reserved for meaning (state, gain/loss) instead of decoration.
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")
plt.style.use('dark_background')

# Surfaces, lightest-on-top
COLOR_BG = "#0B0D11"        # app canvas
COLOR_SIDEBAR = "#0F1218"   # sidebar
COLOR_PANEL = "#141821"     # cards, toolbars
COLOR_PANEL_HI = "#1A1F2A"  # hover / raised
COLOR_LINE = "#222835"      # hairline border
COLOR_LINE_HI = "#2C3444"

# Text
COLOR_TEXT = "#E7EAF0"
COLOR_TEXT_DIM = "#97A0B2"
COLOR_TEXT_MUTE = "#5B6474"

# Accent + semantics. Desaturated so long sessions are not tiring, and so
# green/red read as gain/loss rather than as decoration.
COLOR_ACCENT = "#5B8CFF"
COLOR_ACCENT_HOVER = "#4A7AEE"
COLOR_ACCENT_SOFT = "#182338"   # tinted fill behind accent elements
COLOR_SUCCESS = "#3DD68C"
COLOR_SUCCESS_SOFT = "#12271F"
COLOR_DANGER = "#FF6B81"
COLOR_DANGER_SOFT = "#2A161C"
COLOR_WARN = "#F5B54A"
COLOR_WARN_SOFT = "#2A2113"

# Categorical ramp for charts: distinguishable without shouting.
CHART_SERIES = ["#5B8CFF", "#3DD68C", "#F5B54A", "#B98CFF", "#4ECDC4", "#FF8FA3"]

# Corner radii
RADIUS = 10
RADIUS_SM = 7

# Type. Segoe UI Variable is the Windows 11 UI face; Cascadia Mono is used
# only where characters must line up (log output, tickers, figures).
_UI_TEXT = "Segoe UI Variable Text"
_UI_DISPLAY = "Segoe UI Variable Display"
_MONO = "Cascadia Mono"


def UI(size, weight=None):
    """UI face. Uses the Display optical size for headings."""
    family = _UI_DISPLAY if size >= 16 else _UI_TEXT
    return (family, size, weight) if weight else (family, size)


def MONO(size, weight=None):
    return (_MONO, size, weight) if weight else (_MONO, size)


FONT_MONO = MONO(12)
FONT_HEAD = UI(16, "bold")
FONT_DATA = UI(13)

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

        self.title("Stocks Watcher")
        self.geometry("1100x800") # Increased height for charts
        self.configure(fg_color=COLOR_BG) 

        self.portfolio_mgr = PortfolioManager()
        # Read straight off disk so the record is visible whether or not the
        # watcher is running - the backend owns writing it, not showing it.
        self.paper = PaperTrader(cost_pct=PAPER_COST_PCT, benchmark=PAPER_BENCHMARK)
        self.source_mgr = SourceManager()
        self.keyword_mgr = KeywordManager()
        self.backend = None
        self.notifications_on = ctk.BooleanVar(value=config.NOTIFICATIONS_ENABLED)
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
        self.sidebar_frame = ctk.CTkFrame(self, width=232, corner_radius=0, fg_color=COLOR_SIDEBAR)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_columnconfigure(0, weight=1)
        # Row 6 is an empty spacer: it pushes the live counters and the
        # diagnostic button to the bottom instead of leaving a dead gap.
        self.sidebar_frame.grid_rowconfigure(6, weight=1)

        # Wordmark
        brand = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        brand.grid(row=0, column=0, padx=20, pady=(28, 22), sticky="w")
        self.logo_label = ctk.CTkLabel(brand, text="Stocks Watcher",
                                       font=UI(19, "bold"), text_color=COLOR_TEXT, anchor="w")
        self.logo_label.pack(anchor="w")
        ctk.CTkLabel(brand, text="Market news monitor", font=UI(11),
                     text_color=COLOR_TEXT_MUTE, anchor="w").pack(anchor="w", pady=(1, 0))

        # --- Status panel: state, active engine, and current activity ---
        status_box = ctk.CTkFrame(self.sidebar_frame, fg_color=COLOR_PANEL, corner_radius=RADIUS,
                                  border_width=1, border_color=COLOR_LINE)
        status_box.grid(row=1, column=0, padx=16, pady=(0, 18), sticky="ew")

        self.status_label = ctk.CTkLabel(status_box, text="●  Offline", text_color=COLOR_DANGER,
                                         font=UI(13, "bold"), anchor="w")
        self.status_label.pack(fill='x', padx=14, pady=(12, 2))

        self.engine_label = ctk.CTkLabel(status_box, text=self._engine_text(), text_color=COLOR_TEXT_MUTE,
                                         font=UI(11), anchor="w", justify="left")
        self.engine_label.pack(fill='x', padx=14, pady=(0, 10))

        ctk.CTkFrame(status_box, height=1, fg_color=COLOR_LINE).pack(fill='x', padx=14)

        self.activity_label = ctk.CTkLabel(status_box, text="Idle", text_color=COLOR_TEXT_DIM,
                                           font=UI(11), anchor="w", justify="left",
                                           wraplength=176)
        self.activity_label.pack(fill='x', padx=14, pady=(9, 12))

        # Controls
        self._create_sidebar_btn("Start watching", self.start_backend, COLOR_SUCCESS, 2)
        self._create_sidebar_btn("Stop", self.stop_backend, COLOR_DANGER, 3, state="disabled")
        self._create_sidebar_btn("Reload config", self.reload_settings, COLOR_WARN, 4)
        self._create_sidebar_btn("Settings", self.open_settings, COLOR_ACCENT, 5)

        # --- Live counters (bottom) ---
        stats_box = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        stats_box.grid(row=7, column=0, padx=16, pady=(0, 10), sticky="ew")
        stats_box.grid_columnconfigure((0, 1, 2), weight=1)
        self.stat_labels = {}
        for col, (key, caption, color) in enumerate([
            ('scanned', 'Scanned', COLOR_TEXT),
            ('alerts', 'Alerts', COLOR_SUCCESS),
            ('skipped', 'Skipped', COLOR_TEXT_DIM),
        ]):
            cell = ctk.CTkFrame(stats_box, fg_color="transparent")
            cell.grid(row=0, column=col, sticky="ew")
            value = ctk.CTkLabel(cell, text="0", text_color=color, font=MONO(18, "bold"))
            value.pack()
            ctk.CTkLabel(cell, text=caption, text_color=COLOR_TEXT_MUTE, font=UI(10)).pack(pady=(1, 0))
            self.stat_labels[key] = value

        # Master mute, next to the test button because that is where someone
        # looks when wondering about their phone. Live and persistent - no
        # restart, and the choice survives one.
        # Both live controls share row 6 - the flexible spacer that pushes the
        # counters and test button to the bottom. Anchored "new" so they sit
        # under the buttons and the slack stays below them.
        controls_box = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        controls_box.grid(row=6, column=0, padx=16, pady=(10, 0), sticky="new")

        notif_row = ctk.CTkFrame(controls_box, fg_color="transparent")
        notif_row.pack(fill='x')
        self.notif_switch = ctk.CTkSwitch(
            notif_row, text="Phone alerts", command=self.toggle_notifications,
            variable=self.notifications_on, onvalue=True, offvalue=False,
            progress_color=COLOR_SUCCESS, button_color=COLOR_TEXT,
            button_hover_color=COLOR_TEXT_DIM, fg_color=COLOR_LINE_HI,
            text_color=COLOR_TEXT_DIM, font=UI(12, "bold"),
            switch_width=38, switch_height=18)
        self.notif_switch.pack(anchor="w")
        self.notif_hint = ctk.CTkLabel(notif_row, text="", font=UI(9),
                                       text_color=COLOR_TEXT_MUTE, anchor="w",
                                       justify="left", wraplength=176)
        self.notif_hint.pack(fill='x', pady=(3, 0))
        self._refresh_notif_hint()

        # --- Alert sensitivity ---
        # Which impact ratings are strong enough to alert on. Four discrete
        # stops rather than a continuous range, because the analysers emit
        # four labels - there is nothing between HIGH and CRITICAL to select.
        sens_row = ctk.CTkFrame(controls_box, fg_color="transparent")
        sens_row.pack(fill='x', pady=(14, 0))

        head = ctk.CTkFrame(sens_row, fg_color="transparent")
        head.pack(fill='x')
        ctk.CTkLabel(head, text="Alert on", font=UI(12, "bold"),
                     text_color=COLOR_TEXT_DIM).pack(side='left')
        self.sens_value_label = ctk.CTkLabel(head, text="", font=MONO(11, "bold"),
                                             text_color=COLOR_ACCENT)
        self.sens_value_label.pack(side='right')

        self.sens_slider = ctk.CTkSlider(
            sens_row, from_=0, to=len(config.IMPACT_LEVELS) - 1,
            number_of_steps=len(config.IMPACT_LEVELS) - 1,
            command=self.on_sensitivity_slide,
            progress_color=COLOR_ACCENT, button_color=COLOR_ACCENT,
            button_hover_color=COLOR_ACCENT_HOVER, fg_color=COLOR_LINE_HI,
            height=14)
        self.sens_slider.set(config.impact_rank(config.MIN_IMPACT))
        self.sens_slider.pack(fill='x', pady=(6, 0))
        # Committing on release rather than on every pixel of travel: each
        # change writes settings.json, and dragging fires the callback
        # continuously.
        self.sens_slider.bind("<ButtonRelease-1>", self.commit_sensitivity)

        self.sens_hint = ctk.CTkLabel(sens_row, text="", font=UI(9),
                                      text_color=COLOR_TEXT_MUTE, anchor="w",
                                      justify="left", wraplength=176)
        self.sens_hint.pack(fill='x', pady=(3, 0))
        self._refresh_sensitivity_labels(config.MIN_IMPACT)

        self._create_sidebar_btn("Send test alert", self.send_test_alert, COLOR_TEXT, 8)

        # --- Main View ---
        self.main_area = ctk.CTkFrame(self, fg_color="transparent")
        self.main_area.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.main_area.grid_rowconfigure(1, weight=1)
        self.main_area.grid_columnconfigure(0, weight=1)

        # Styled Tabs
        self.tab_var =  ctk.StringVar(value="Alerts")
        self.seg_button = ctk.CTkSegmentedButton(self.main_area, values=["Alerts", "Logs", "Portfolio", "Sources", "Keywords"],
                                                command=self.switch_tab,
                                                selected_color=COLOR_ACCENT,
                                                selected_hover_color=COLOR_ACCENT_HOVER,
                                                unselected_color=COLOR_PANEL,
                                                unselected_hover_color=COLOR_PANEL_HI,
                                                text_color=COLOR_TEXT_DIM,
                                                corner_radius=RADIUS_SM,
                                                border_width=3,
                                                bg_color="transparent",
                                                font=UI(12, "bold"))
        self.seg_button.set("Alerts")
        self.seg_button.grid(row=0, column=0, sticky="w", pady=(0, 16))

        # -- Views --
        self.frame_alerts = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.frame_logs = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.frame_portfolio = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.frame_sources = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.frame_keywords = ctk.CTkFrame(self.main_area, fg_color="transparent")

        # Alerts is the default view - it is what the app exists to produce.
        self.frame_alerts.grid(row=1, column=0, sticky="nsew")

        # Tab: Terminal Logs (toolbar + colour-coded output)
        log_bar = ctk.CTkFrame(self.frame_logs, fg_color=COLOR_PANEL, corner_radius=RADIUS,
                               border_width=1, border_color=COLOR_LINE)
        log_bar.pack(fill='x', pady=(0, 8))

        ctk.CTkLabel(log_bar, text="Activity", font=UI(13, "bold"),
                     text_color=COLOR_ACCENT).pack(side='left', padx=16, pady=8)

        ctk.CTkButton(log_bar, text="Clear", command=self.clear_log, width=80,
                      fg_color="transparent", border_width=0,
                      text_color=COLOR_TEXT_DIM, hover_color=COLOR_PANEL_HI, corner_radius=RADIUS,
                      font=UI(10, "bold")).pack(side='right', padx=10)

        ctk.CTkCheckBox(log_bar, text="AI traffic", variable=self.show_ai_traffic,
                        onvalue=True, offvalue=False, checkbox_width=16, checkbox_height=16,
                        fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                        text_color=COLOR_TEXT_DIM, font=UI(10),
                        corner_radius=RADIUS, border_color=COLOR_TEXT_MUTE).pack(side='right', padx=10)

        ctk.CTkCheckBox(log_bar, text="Auto-scroll", variable=self.autoscroll,
                        onvalue=True, offvalue=False, checkbox_width=16, checkbox_height=16,
                        fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                        text_color=COLOR_TEXT_DIM, font=UI(10),
                        corner_radius=RADIUS, border_color=COLOR_TEXT_MUTE).pack(side='right', padx=10)

        self.log_area = ctk.CTkTextbox(self.frame_logs, state='disabled',
                                      font=MONO(11),
                                      fg_color=COLOR_BG,
                                      text_color=COLOR_TEXT_DIM,
                                      border_color=COLOR_SIDEBAR, border_width=2, corner_radius=RADIUS)
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
            if config.USE_CLOUD_AI:
                return f"Cloud AI  ·  {config.CLOUD_AI_MODEL}"
            if config.USE_LOCAL_LLM:
                return f"Local AI  ·  {config.LOCAL_MODEL_NAME}"
            return "Keyword scoring  ·  offline"
        except Exception:
            return "Engine unknown"

    def _setup_alerts_tab(self):
        """Alert history - the app's actual output, which used to only ever
        scroll past in the log."""
        self.frame_alerts.grid_rowconfigure(1, weight=1)
        self.frame_alerts.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(self.frame_alerts, fg_color=COLOR_PANEL, corner_radius=RADIUS,
                           border_width=1, border_color=COLOR_LINE)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        self.alerts_title = ctk.CTkLabel(bar, text="Alerts", font=UI(14, "bold"),
                                         text_color=COLOR_ACCENT)
        self.alerts_title.pack(side='left', padx=20, pady=10)

        ctk.CTkButton(bar, text="Clear", command=self.clear_alerts, width=90,
                      fg_color="transparent", border_width=0,
                      text_color=COLOR_DANGER, hover_color=COLOR_DANGER_SOFT, corner_radius=RADIUS,
                      font=UI(11, "bold")).pack(side='right', padx=10)

        self.alerts_list = ctk.CTkScrollableFrame(self.frame_alerts, fg_color="transparent",
                                                  corner_radius=RADIUS)
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
            text=f"Alerts  ·  {len(self.alerts)}" if self.alerts else "Alerts"
        )

        if not self.alerts:
            empty = ctk.CTkFrame(self.alerts_list, fg_color="transparent")
            empty.pack(expand=True, fill='both', pady=70)
            ctk.CTkLabel(empty, text="No alerts yet", font=UI(16, "bold"),
                         text_color=COLOR_LINE).pack()
            ctk.CTkLabel(empty,
                         text="Alerts appear here when the analyzer finds\n"
                              "HIGH or CRITICAL impact news.\n\n"
                              "Most articles correctly produce no alert -\n"
                              "check the LOGS tab to confirm it is scanning.",
                         font=UI(10), text_color=COLOR_TEXT_MUTE, justify="center").pack(pady=12)
            return

        # Newest first
        for alert in reversed(self.alerts):
            self._build_alert_card(alert)

    def _build_alert_card(self, alert):
        if alert.get('kind') == 'sell_signal':
            self._build_exit_card(alert)
            return

        positive = alert.get('sentiment') == 'POSITIVE'
        accent = COLOR_SUCCESS if positive else COLOR_DANGER

        card = ctk.CTkFrame(self.alerts_list, fg_color=COLOR_PANEL, corner_radius=RADIUS,
                            border_width=1, border_color=COLOR_LINE)
        card.pack(fill='x', pady=4, padx=2)

        # Sentiment stripe down the left edge
        stripe = ctk.CTkFrame(card, fg_color=accent, width=4, corner_radius=RADIUS)
        stripe.pack(side='left', fill='y')

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(side='left', fill='both', expand=True, padx=14, pady=10)

        header = ctk.CTkFrame(body, fg_color="transparent")
        header.pack(fill='x')

        ticker = alert.get('ticker')
        name = alert.get('company') or 'Unknown'
        title = f"{name} ({ticker})" if ticker else name
        ctk.CTkLabel(header, text=title, font=UI(14, "bold"),
                     text_color=COLOR_TEXT, anchor="w").pack(side='left')

        impact = alert.get('impact', 'HIGH')
        badge_color = COLOR_DANGER if impact == 'CRITICAL' else COLOR_WARN
        ctk.CTkLabel(header, text=f" {impact} ", font=UI(9, "bold"),
                     text_color=COLOR_BG, fg_color=badge_color, corner_radius=RADIUS).pack(side='left', padx=8)

        ctk.CTkLabel(header, text=f"{'▲' if positive else '▼'} {alert.get('sentiment', '')}",
                     font=UI(10, "bold"), text_color=accent).pack(side='left', padx=4)

        if alert.get('is_owned'):
            ctk.CTkLabel(header, text="  Owned  ", font=UI(9, "bold"),
                         text_color=COLOR_BG, fg_color=COLOR_ACCENT, corner_radius=RADIUS).pack(side='left', padx=6)

        stamp = alert.get('time')
        stamp_text = stamp.strftime('%H:%M:%S') if hasattr(stamp, 'strftime') else str(stamp or '')
        ctk.CTkLabel(header, text=stamp_text, font=MONO(10),
                     text_color=COLOR_TEXT_MUTE).pack(side='right')

        headline = alert.get('headline') or ''
        if headline:
            ctk.CTkLabel(body, text=headline, font=UI(11), text_color=COLOR_TEXT_DIM,
                         anchor="w", justify="left", wraplength=740).pack(fill='x', pady=(6, 0))

        explanation = (alert.get('explanation') or '').strip()
        if explanation:
            ctk.CTkLabel(body, text=explanation, font=UI(11), text_color=COLOR_TEXT,
                         anchor="w", justify="left", wraplength=740).pack(fill='x', pady=(6, 0))

        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.pack(fill='x', pady=(8, 0))

        prediction = alert.get('prediction')
        if prediction:
            ctk.CTkLabel(footer, text=f"Prediction · {prediction}", font=UI(10, "bold"),
                         text_color=accent).pack(side='left')

        url = alert.get('url')
        if url:
            ctk.CTkButton(footer, text="Open article  ↗", width=130, height=26,
                          command=lambda u=url: webbrowser.open(u),
                          fg_color=COLOR_ACCENT_SOFT, border_width=0,
                          text_color=COLOR_ACCENT, hover_color=COLOR_PANEL_HI, corner_radius=RADIUS_SM,
                          font=UI(10, "bold")).pack(side='right')

    EXIT_REASONS = {
        'target_hit': "Target price reached - the alerted-on move played out.",
        'stop_loss': "Stop-loss reached - closing to cap the loss.",
        'horizon_expired': "Time window passed without the move - reassess.",
    }

    def _build_exit_card(self, alert):
        """A SELL / COVER SHORT signal from the watch check. These used to be
        drawn by the news-alert card, which has no idea what they are: no
        sentiment, so a red stripe and a blank '▼', and a made-up HIGH badge."""
        is_short = alert.get('direction') == 'SHORT'
        accent = COLOR_ACCENT if is_short else COLOR_WARN

        card = ctk.CTkFrame(self.alerts_list, fg_color=COLOR_PANEL, corner_radius=RADIUS,
                            border_width=1, border_color=COLOR_LINE)
        card.pack(fill='x', pady=4, padx=2)
        ctk.CTkFrame(card, fg_color=accent, width=4, corner_radius=RADIUS).pack(side='left', fill='y')

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(side='left', fill='both', expand=True, padx=14, pady=10)

        header = ctk.CTkFrame(body, fg_color="transparent")
        header.pack(fill='x')
        ticker = alert.get('ticker')
        name = alert.get('company') or ticker or 'Unknown'
        title = f"{name} ({ticker})" if ticker and ticker != name else name
        ctk.CTkLabel(header, text=title, font=UI(14, "bold"),
                     text_color=COLOR_TEXT, anchor="w").pack(side='left')
        ctk.CTkLabel(header, text=f" {'COVER SHORT' if is_short else 'SELL SIGNAL'} ",
                     font=UI(9, "bold"), text_color=COLOR_BG, fg_color=accent,
                     corner_radius=RADIUS).pack(side='left', padx=8)

        stamp = alert.get('time')
        stamp_text = stamp.strftime('%H:%M:%S') if hasattr(stamp, 'strftime') else str(stamp or '')
        ctk.CTkLabel(header, text=stamp_text, font=MONO(10),
                     text_color=COLOR_TEXT_MUTE).pack(side='right')

        entry = alert.get('entry_price') or 0.0
        now_price = alert.get('current_price') or 0.0
        price_pct = ((now_price - entry) / entry * 100) if entry else 0.0
        position_pct = -price_pct if is_short else price_pct
        pl_colour = COLOR_SUCCESS if position_pct >= 0 else COLOR_DANGER

        action = "Buy back to close the short CFD" if is_short else "Sell to close the long CFD"
        reason = self.EXIT_REASONS.get(alert.get('reason'), str(alert.get('reason') or ''))
        ctk.CTkLabel(body, text=f"{action}  ·  {reason}", font=UI(11),
                     text_color=COLOR_TEXT, anchor="w", justify="left",
                     wraplength=740).pack(fill='x', pady=(6, 0))

        ctk.CTkLabel(body,
                     text=f"Entry {entry:.2f}  →  Now {now_price:.2f}   "
                          f"({price_pct:+.1f}% price, {position_pct:+.1f}% on the position)",
                     font=MONO(11), text_color=pl_colour, anchor="w").pack(fill='x', pady=(4, 0))

        headline = alert.get('headline') or ''
        url = alert.get('url')
        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.pack(fill='x', pady=(8, 0))
        if headline:
            ctk.CTkLabel(footer, text=headline, font=UI(10), text_color=COLOR_TEXT_MUTE,
                         anchor="w", justify="left", wraplength=560).pack(side='left')
        if url:
            ctk.CTkButton(footer, text="Original article  ↗", width=140, height=26,
                          command=lambda u=url: webbrowser.open(u),
                          fg_color=COLOR_ACCENT_SOFT, border_width=0,
                          text_color=COLOR_ACCENT, hover_color=COLOR_PANEL_HI, corner_radius=RADIUS_SM,
                          font=UI(10, "bold")).pack(side='right')

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
        box.tag_config('head', foreground=COLOR_TEXT)
        box.tag_config('ai', foreground="#4A5468")
        box.tag_config('muted', foreground=COLOR_TEXT_MUTE)
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
        """Sidebar action. Only the primary action is filled; the rest stay
        quiet so the eye lands on one thing instead of five outlined boxes."""
        primary = glow_color == COLOR_SUCCESS
        if primary:
            style = dict(fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                         text_color="#FFFFFF", border_width=0)
        else:
            style = dict(fg_color="transparent", hover_color=COLOR_PANEL_HI,
                         text_color=COLOR_TEXT_DIM, border_width=0)
        btn = ctk.CTkButton(self.sidebar_frame, text=text, command=cmd, state=state,
                            corner_radius=RADIUS_SM, font=UI(12, "bold"), height=36,
                            anchor="center", **style)
        btn.grid(row=row, column=0, padx=16, pady=4, sticky="ew")
        setattr(self, f"btn_{row}", btn)
        return btn

    def open_settings(self):
        win = ctk.CTkToplevel(self)
        win.title("Settings")
        win.geometry("500x720")
        win.configure(fg_color=COLOR_BG)
        win.transient(self)

        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(expand=True, fill='both', padx=10, pady=10)

        ctk.CTkLabel(scroll, text="Notifications", font=UI(16, "bold"), text_color=COLOR_ACCENT).pack(pady=(10, 20))

        def add_input(label, default_val, show=None):
            ctk.CTkLabel(scroll, text=label, text_color=COLOR_TEXT_DIM, font=UI(11)).pack(anchor="w", padx=20, pady=(10,0))
            entry = ctk.CTkEntry(scroll, width=350, fg_color=COLOR_PANEL, border_color=COLOR_LINE,
                                  text_color=COLOR_TEXT, font=UI(12), show=show)
            entry.pack(pady=(5,0))
            if default_val:
                entry.insert(0, str(default_val))
            return entry

        try:
            import config
            current_topic = config.NTFY_TOPIC
            current_model = config.LOCAL_MODEL_NAME
            current_threads = config.OLLAMA_NUM_THREADS
            current_cloud_on = config.USE_CLOUD_AI
            current_cloud_provider = config.CLOUD_AI_PROVIDER
            current_cloud_model = config.CLOUD_AI_MODEL
            current_cloud_key = config.CLOUD_AI_API_KEY
            current_cloud_base_url = config.CLOUD_AI_BASE_URL
            current_stop_loss_pct = config.STOP_LOSS_PCT
        except:
            current_topic = "stocks_ai_secret"
            current_model = "phi3:mini"
            current_threads = 1
            current_cloud_on = False
            current_cloud_provider = "anthropic"
            current_cloud_model = "claude-opus-5"
            current_cloud_key = ""
            current_cloud_base_url = ""
            current_stop_loss_pct = 0.0

        from cloud_providers import PROVIDERS
        provider_names = list(PROVIDERS.keys())

        e_topic = add_input("Notification topic", current_topic)

        ctk.CTkLabel(scroll, text="ℹ️ Change this to a unique value for privacy",
                    text_color=COLOR_TEXT_MUTE, font=UI(9)).pack(pady=(5, 20))

        ctk.CTkLabel(scroll, text="Risk management", font=UI(16, "bold"),
                    text_color=COLOR_ACCENT).pack(pady=(10, 10))

        e_stop_loss = add_input("Stop-loss %", round(current_stop_loss_pct * 100, 4))

        ctk.CTkLabel(scroll,
                    text="ℹ️ 0 = disabled: watches close on schedule as before.\n"
                         "Above 0: a watch whose exit window expires while it's\n"
                         "at a loss is postponed instead of sold, and only closes\n"
                         "early if the price falls this far past entry (caps the\n"
                         "downside) - so the app only sells for a profit, unless\n"
                         "the stop-loss forces it.",
                    text_color=COLOR_TEXT_MUTE, font=UI(9), justify="left").pack(anchor="w", padx=20, pady=(5, 20))

        ctk.CTkLabel(scroll, text="Local AI model", font=UI(16, "bold"),
                    text_color=COLOR_ACCENT).pack(pady=(10, 10))

        e_model = add_input("Ollama model", current_model)
        e_threads = add_input("Threads", current_threads)

        ctk.CTkLabel(scroll, text="ℹ️ Model must be pulled first: ollama pull <name>",
                    text_color=COLOR_TEXT_MUTE, font=UI(9)).pack(pady=(5, 20))

        ctk.CTkLabel(scroll, text="Cloud AI (API key)", font=UI(16, "bold"),
                    text_color=COLOR_ACCENT).pack(pady=(10, 10))

        cloud_enabled = ctk.BooleanVar(value=current_cloud_on)
        ctk.CTkCheckBox(scroll, text="Use Cloud AI instead of the local model",
                        variable=cloud_enabled, onvalue=True, offvalue=False,
                        checkbox_width=18, checkbox_height=18,
                        fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                        text_color=COLOR_TEXT, font=UI(11),
                        border_color=COLOR_TEXT_MUTE).pack(anchor="w", padx=20, pady=(0, 10))

        ctk.CTkLabel(scroll, text="Provider", text_color=COLOR_TEXT_DIM, font=UI(11)).pack(anchor="w", padx=20, pady=(10, 0))
        provider_var = ctk.StringVar(value=current_cloud_provider if current_cloud_provider in provider_names else provider_names[0])
        ctk.CTkOptionMenu(scroll, values=provider_names, variable=provider_var, width=350,
                          fg_color=COLOR_PANEL, button_color=COLOR_PANEL_HI, button_hover_color=COLOR_LINE_HI,
                          text_color=COLOR_TEXT, font=UI(12), dropdown_font=UI(12),
                          corner_radius=RADIUS_SM).pack(pady=(5, 0))

        e_cloud_key = add_input("API key", current_cloud_key, show="*")
        e_cloud_model = add_input("Model", current_cloud_model)
        e_cloud_base_url = add_input("Base URL (optional - for OpenAI-compatible hosts)", current_cloud_base_url)

        ctk.CTkLabel(scroll,
                    text="ℹ️ Runs on every scanned article - pick a cheaper/faster\n"
                         "model if you scan frequently. Overrides the local model\n"
                         "above when enabled. 'openai' + a base URL also reaches\n"
                         "OpenAI-compatible third-party hosts (Groq, Together, a\n"
                         "local server, ...); 'openrouter' and 'routera' each route\n"
                         "one key to many hosted models (model names look like\n"
                         "\"anthropic/claude-opus-5\"). Add more providers in\n"
                         "cloud_providers.py. Requires: pip install anthropic / openai",
                    text_color=COLOR_TEXT_MUTE, font=UI(9), justify="left").pack(anchor="w", padx=20, pady=(5, 20))

        def save():
            import json
            import os
            try:
                stop_loss_input = e_stop_loss.get().strip()
                try:
                    stop_loss_pct = max(0.0, float(stop_loss_input)) / 100.0 if stop_loss_input else 0.0
                except ValueError:
                    messagebox.showerror("Error", "Stop-loss % must be a number.")
                    return

                threads_input = e_threads.get().strip()
                try:
                    threads = max(1, int(threads_input)) if threads_input else config.OLLAMA_NUM_THREADS
                except ValueError:
                    messagebox.showerror("Error", "Threads must be a whole number.")
                    return

                # save_settings merges into the file (so settings this dialog
                # does not expose survive) and updates the config module, so
                # a running watcher can apply the change immediately.
                config.save_settings({
                    "NTFY_TOPIC": e_topic.get().strip(),
                    "LOCAL_MODEL_NAME": e_model.get().strip() or config.LOCAL_MODEL_NAME,
                    "OLLAMA_NUM_THREADS": threads,
                    "USE_CLOUD_AI": cloud_enabled.get(),
                    "CLOUD_AI_PROVIDER": provider_var.get().strip(),
                    "CLOUD_AI_MODEL": e_cloud_model.get().strip() or config.CLOUD_AI_MODEL,
                    "CLOUD_AI_API_KEY": e_cloud_key.get().strip(),
                    "CLOUD_AI_BASE_URL": e_cloud_base_url.get().strip(),
                    "STOP_LOSS_PCT": stop_loss_pct,
                })
                if self.backend:
                    self.backend.apply_settings()
                self.engine_label.configure(text=self._engine_text())
                self._refresh_notif_hint()
                self._setup_keywords_banner()
                messagebox.showinfo("Settings saved", "Saved and applied.")
                win.destroy()
            except Exception as e:
                messagebox.showerror("Error", f"Save failed: {e}")

        ctk.CTkButton(scroll, text="Save", command=save,
                     fg_color=COLOR_ACCENT, text_color=COLOR_BG, hover_color=COLOR_ACCENT_HOVER,
                     font=UI(12, "bold"), width=300).pack(pady=40)

    def switch_tab(self, value):
        frames = {
            "Alerts": self.frame_alerts,
            "Logs": self.frame_logs,
            "Portfolio": self.frame_portfolio,
            "Sources": self.frame_sources,
            "Keywords": self.frame_keywords,
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
        self.log_queue.put("Starting up...")
        self.backend = StockAppBackend(
            log_callback=self.log_callback,
            alert_callback=self.add_alert,
            status_callback=lambda text, stats: self.status_queue.put((text, stats)),
            # Share the tab's own managers so a holding or source added
            # while watching reaches the scan without a restart.
            portfolio_mgr=self.portfolio_mgr,
            source_mgr=self.source_mgr,
        )
        self.backend.start()
        self.btn_2.configure(state='disabled', fg_color=COLOR_PANEL, text_color=COLOR_TEXT_MUTE)
        self.btn_3.configure(state='normal', text_color=COLOR_DANGER)
        self.status_label.configure(text="●  Watching", text_color=COLOR_SUCCESS)
        self.engine_label.configure(text=self._engine_text())
        self.activity_label.configure(text="Starting up...")

    def stop_backend(self):
        if self.backend:
            self.log_queue.put("Stopping...")
            self.backend.stop()
            self.backend = None
        self.btn_2.configure(state='normal', fg_color=COLOR_ACCENT, text_color='#FFFFFF')
        self.btn_3.configure(state='disabled', text_color=COLOR_TEXT_MUTE)
        self.status_label.configure(text="●  Offline", text_color=COLOR_DANGER)
        self.activity_label.configure(text="Idle")

    def toggle_notifications(self):
        """Mute or unmute phone alerts, live and persistently.

        Applies to the running watcher's notifier if there is one, and is
        saved so a notifier built later (or after a restart) starts in the
        same state.
        """
        enabled = bool(self.notifications_on.get())
        try:
            if self.backend and self.backend.notifier:
                self.backend.notifier.set_enabled(enabled)
            else:
                # Nothing running to update - persist it directly so the
                # choice still applies when the watcher next starts.
                config.save_setting("NOTIFICATIONS_ENABLED", enabled)
        except Exception as e:
            self.log_queue.put(f"Could not save notification setting: {e}")
            # Put the switch back rather than showing a state that was not saved.
            self.notifications_on.set(not enabled)
        self._refresh_notif_hint()
        self.log_queue.put(f"Phone alerts {'enabled' if self.notifications_on.get() else 'muted'}.")

    # How many alerts each threshold tends to produce, and what it costs.
    # Phrased as a trade-off rather than a recommendation: the whole point of
    # the slider is that the right setting depends on the paper record.
    SENSITIVITY_HINTS = {
        "CRITICAL": "Rarest. Only game-changing news; a handful a month.",
        "HIGH": "Default. Significant events, 5-15% expected moves.",
        "MEDIUM": "Louder. Routine news too — more alerts, more misreads.",
        "LOW": "Everything not rejected outright. Expect a lot of noise.",
    }

    def _slider_level(self):
        """The impact rating the slider currently points at."""
        index = int(round(self.sens_slider.get()))
        index = max(0, min(len(config.IMPACT_LEVELS) - 1, index))
        return config.IMPACT_LEVELS[index]

    def _refresh_sensitivity_labels(self, level):
        self.sens_value_label.configure(text=f"{level}+")
        self.sens_hint.configure(text=self.SENSITIVITY_HINTS.get(level, ""))

    def on_sensitivity_slide(self, _value):
        """Live label update while dragging. Nothing is saved until release -
        a drag from CRITICAL to LOW would otherwise write the file four
        times and briefly leave the watcher on an unintended setting."""
        self._refresh_sensitivity_labels(self._slider_level())

    def commit_sensitivity(self, _event=None):
        """Apply the released slider position, live and persistently."""
        level = self._slider_level()
        if level == config.MIN_IMPACT:
            return
        try:
            config.save_setting("MIN_IMPACT", level)
        except Exception as e:
            self.log_queue.put(f"Could not save alert sensitivity: {e}")
            # Snap back rather than showing a threshold that was not saved.
            self.sens_slider.set(config.impact_rank(config.MIN_IMPACT))
        self._refresh_sensitivity_labels(config.MIN_IMPACT)
        self.log_queue.put(f"Alerting on {config.MIN_IMPACT} impact and above.")

    def _refresh_notif_hint(self):
        """Explain whichever way alerts are currently not reaching the phone."""
        if not self.notifications_on.get():
            text = "Muted. Scanning and the paper record carry on."
        elif not config.NTFY_TOPIC:
            text = "No topic set — add one in Settings."
        else:
            text = ""
        self.notif_hint.configure(text=text)

    def send_test_alert(self):
        try:
            from notifier import Notifier
            # Reuse the running watcher's notifier when there is one - it
            # reflects the live mute state; a fresh one reads the file.
            n = self.backend.notifier if self.backend else Notifier()
            self.log_queue.put("Sending test alert...")
            # notify_system rather than notify(): the latter applies the
            # sensitivity threshold, so with the slider on CRITICAL a HIGH
            # test alert was silently filtered and reported as "ntfy.sh
            # did not accept the notification".
            sent = n.notify_system(
                "Test Notification",
                f"Diagnostic check from Stocks Watcher at {datetime.datetime.now().strftime('%H:%M:%S')}.")
            # Say what actually happened. Claiming "sent" while muted, or with
            # no topic set, sends someone hunting for a phone problem that is
            # really a setting two inches away.
            if sent:
                messagebox.showinfo("Diagnostic", "Signal sent - check your phone.")
            elif not n.enabled:
                messagebox.showwarning(
                    "Diagnostic",
                    "Phone alerts are muted, so nothing was sent.\n\n"
                    "Turn on the 'Phone alerts' switch in the sidebar.")
            elif not n.ntfy_topic:
                messagebox.showwarning(
                    "Diagnostic",
                    "No notification topic is set, so nothing was sent.\n\n"
                    "Set one in Settings, then subscribe to it in the ntfy app.")
            else:
                messagebox.showerror(
                    "Diagnostic",
                    "ntfy.sh did not accept the notification. Check the topic "
                    "name and your connection.")
        except Exception as e:
            self.log_queue.put(f"Diagnostic Error: {e}")

    def reload_settings(self):
        """Reload keywords, sources, and settings without restarting the watcher."""
        try:
            self.log_queue.put("Reloading configuration...")
            
            # Re-read in place rather than replacing the instances: the
            # running backend holds references to these same objects.
            self.keyword_mgr.reload()
            self.refresh_keywords_list()
            self.log_queue.put("✓ Keywords reloaded")

            self.source_mgr.reload()
            self.refresh_sources_list()
            self.log_queue.put("✓ Sources reloaded")

            self.portfolio_mgr.reload()
            self.refresh_portfolio_list()

            # Re-read data/settings.json and apply it to the running watcher
            config.reload_from_disk(verbose=False)
            self.sens_slider.set(config.impact_rank(config.MIN_IMPACT))
            self._refresh_sensitivity_labels(config.MIN_IMPACT)
            self.notifications_on.set(config.NOTIFICATIONS_ENABLED)
            self._refresh_notif_hint()
            self._setup_keywords_banner()
            if self.backend:
                self.backend.apply_settings()
            self.engine_label.configure(text=self._engine_text())
            self.log_queue.put("✓ Config reloaded")

            # If backend is running, reload analyzer keywords
            # The LLM analyzer has no keyword table to reload.
            if self.backend and hasattr(self.backend.analyzer, 'reload_keywords'):
                self.backend.analyzer.reload_keywords()
                self.log_queue.put("✓ Analyzer updated with new keywords")
            
            messagebox.showinfo("Reload Complete", "Settings, keywords, and sources have been reloaded successfully!")
            self.log_queue.put("Configuration reloaded.")
            
        except Exception as e:
            self.log_queue.put(f"Reload Error: {e}")
            messagebox.showerror("Reload Error", f"Failed to reload settings: {e}")

    def _setup_portfolio_tab(self):
        # Two views under one tab: the stocks you actually own, and the app's
        # own paper-trading record. Kept apart rather than merged - the paper
        # positions are simulated, and portfolio.json feeds the analyzer
        # prompt, so mixing them in would change what the app alerts on and
        # corrupt the very record being measured.
        self.frame_portfolio.grid_rowconfigure(0, weight=0)
        self.frame_portfolio.grid_rowconfigure(1, weight=1)
        self.frame_portfolio.grid_columnconfigure(0, weight=1)

        self.pf_view_var = ctk.StringVar(value="Holdings")
        self.pf_seg = ctk.CTkSegmentedButton(
            self.frame_portfolio, values=["Holdings", "Paper trades"],
            command=self._switch_pf_view, variable=self.pf_view_var,
            selected_color=COLOR_ACCENT, selected_hover_color=COLOR_ACCENT_HOVER,
            unselected_color=COLOR_PANEL, unselected_hover_color=COLOR_PANEL_HI,
            text_color=COLOR_TEXT_DIM, corner_radius=RADIUS_SM,
            border_width=3, bg_color="transparent", font=UI(11, "bold"))
        self.pf_seg.grid(row=0, column=0, sticky="w", pady=(0, 12))

        self.frame_pf_holdings = ctk.CTkFrame(self.frame_portfolio, fg_color="transparent")
        self.frame_pf_paper = ctk.CTkFrame(self.frame_portfolio, fg_color="transparent")
        self.frame_pf_holdings.grid(row=1, column=0, sticky="nsew")

        self._setup_holdings_view()
        self._setup_paper_view()

    def _switch_pf_view(self, value):
        for frame in (self.frame_pf_holdings, self.frame_pf_paper):
            frame.grid_forget()
        if value == "Paper trades":
            self.frame_pf_paper.grid(row=1, column=0, sticky="nsew")
            self.refresh_paper_view()
        else:
            self.frame_pf_holdings.grid(row=1, column=0, sticky="nsew")

    def _setup_holdings_view(self):
        self.frame_pf_holdings.grid_rowconfigure(0, weight=0)
        self.frame_pf_holdings.grid_rowconfigure(1, weight=1)
        self.frame_pf_holdings.grid_rowconfigure(2, weight=1)
        self.frame_pf_holdings.grid_columnconfigure(0, weight=1)

        # Top Controls
        bar = ctk.CTkFrame(self.frame_pf_holdings, fg_color=COLOR_PANEL, corner_radius=RADIUS, border_width=1, border_color=COLOR_LINE)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        
        self.entry_ticker = ctk.CTkEntry(bar, placeholder_text="Ticker", width=120,
                                        fg_color=COLOR_BG, border_color=COLOR_LINE, text_color=COLOR_TEXT, corner_radius=RADIUS_SM, font=UI(12))
        self.entry_ticker.pack(side='left', padx=10, pady=10)
        
        self.entry_price = ctk.CTkEntry(bar, placeholder_text="Buy price", width=100,
                                        fg_color=COLOR_BG, border_color=COLOR_LINE, text_color=COLOR_TEXT, corner_radius=RADIUS_SM, font=UI(12))
        self.entry_price.pack(side='left', padx=(0, 10), pady=10)
        
        ctk.CTkButton(bar, text="Add", command=self.add_to_portfolio, width=80,
                     fg_color=COLOR_ACCENT, text_color="#FFFFFF", hover_color=COLOR_ACCENT_HOVER, corner_radius=RADIUS_SM, border_width=0, font=UI(11, "bold")).pack(side='left', padx=10)

        ctk.CTkButton(bar, text="Clear all", command=self.reset_portfolio, width=100,
                     fg_color="transparent", border_width=0, text_color=COLOR_DANGER,
                     hover_color=COLOR_DANGER_SOFT, corner_radius=RADIUS_SM, font=UI(11, "bold")).pack(side='right', padx=10)

        ctk.CTkButton(bar, text="Refresh", command=self.refresh_portfolio_list, width=80,
                     fg_color="transparent", border_width=0, text_color=COLOR_TEXT_DIM, hover_color=COLOR_PANEL_HI, corner_radius=RADIUS_SM, font=UI(11)).pack(side='right', padx=10)

        # List Container
        self.frame_list_container = ctk.CTkFrame(self.frame_pf_holdings, fg_color="transparent")
        self.frame_list_container.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        
        headers = [("Ticker", 80), ("Buy price", 100), ("Live price", 100), ("P/L %", 100), ("Gain / loss", 100)]
        header_inner = ctk.CTkFrame(self.frame_list_container, fg_color="transparent")
        header_inner.pack(fill='x')
        for text, width in headers:
             ctk.CTkLabel(header_inner, text=text, width=width, anchor="w", font=UI(11, "bold"), text_color=COLOR_TEXT_MUTE).pack(side='left', padx=10)

        self.portfolio_list_frame = ctk.CTkScrollableFrame(self.frame_list_container, fg_color="transparent", corner_radius=RADIUS, height=200)
        self.portfolio_list_frame.pack(expand=True, fill='both')

        # Charts Area
        self.frame_charts = ctk.CTkFrame(self.frame_pf_holdings, fg_color=COLOR_PANEL, corner_radius=RADIUS)
        self.frame_charts.grid(row=2, column=0, sticky="nsew", padx=10, pady=10)
        self.frame_charts.grid_columnconfigure(0, weight=1)
        self.frame_charts.grid_columnconfigure(1, weight=1)
        self.frame_charts.grid_rowconfigure(0, weight=1)
        
        self.chart_frame_left = ctk.CTkFrame(self.frame_charts, fg_color="transparent")
        self.chart_frame_left.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        
        self.chart_frame_right = ctk.CTkFrame(self.frame_charts, fg_color="transparent")
        self.chart_frame_right.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)

    # --- Paper trading view -----------------------------------------------
    # The app's own track record: what each alert would have earned or lost.
    # Reads data/paper_trades.json straight from disk rather than through
    # self.backend, so it still works when the watcher is stopped - and so a
    # copy of the app that never started shows the accumulated history.

    def _setup_paper_view(self):
        self.frame_pf_paper.grid_rowconfigure(2, weight=1)
        self.frame_pf_paper.grid_columnconfigure(0, weight=1)

        strip = ctk.CTkFrame(self.frame_pf_paper, fg_color=COLOR_PANEL,
                             corner_radius=RADIUS, border_width=1, border_color=COLOR_LINE)
        strip.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        self.paper_stat_labels = {}
        for key, label in (("expectancy", "Expectancy / trade"),
                           ("win_rate", "Win rate"),
                           ("alpha", "Alpha vs market"),
                           ("trades", "Closed"),
                           ("open", "Open"),
                           ("drawdown", "Max drawdown")):
            cell = ctk.CTkFrame(strip, fg_color="transparent")
            cell.pack(side='left', padx=18, pady=12)
            value = ctk.CTkLabel(cell, text="—", font=MONO(17, "bold"), text_color=COLOR_TEXT)
            value.pack(anchor='w')
            ctk.CTkLabel(cell, text=label.upper(), font=UI(9), text_color=COLOR_TEXT_MUTE).pack(anchor='w')
            self.paper_stat_labels[key] = value

        ctk.CTkButton(strip, text="Refresh", command=self.refresh_paper_view, width=80,
                      fg_color="transparent", border_width=0, text_color=COLOR_TEXT_DIM,
                      hover_color=COLOR_PANEL_HI, corner_radius=RADIUS_SM,
                      font=UI(11)).pack(side='right', padx=14)

        # One caveat at a time, most important first - a small sample is the
        # thing most likely to be misread here.
        self.paper_note = ctk.CTkLabel(self.frame_pf_paper, text="", font=UI(11),
                                       text_color=COLOR_WARN, anchor='w', justify='left',
                                       wraplength=900)
        self.paper_note.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 8))

        self.paper_scroll = ctk.CTkScrollableFrame(self.frame_pf_paper, fg_color="transparent")
        self.paper_scroll.grid(row=2, column=0, sticky="nsew")

    def refresh_paper_view(self):
        """Reload the ledger and mark open positions to live prices."""
        try:
            self.paper.reload()
        except Exception as e:
            self.log_queue.put(f"Paper ledger read failed: {e}")
            return

        tickers = self.paper.tickers_open()
        if not tickers:
            self._render_paper({})
            return
        # Prices come off the network; keep it off the UI thread.
        self._render_paper({}, pending=True)
        threading.Thread(target=self._fetch_paper_prices, args=(tickers,), daemon=True).start()

    def _fetch_paper_prices(self, tickers):
        prices = {}
        try:
            prices = price_lookup.fetch_prices(tickers)
        except Exception as e:
            print(f"Paper price fetch failed: {e}")
        try:
            if self.winfo_exists():
                self.after(0, lambda: self._render_paper(prices))
        except Exception:
            pass

    def _render_paper(self, prices, pending=False):
        data = self.paper.overview(prices,
                                   start_capital=PAPER_START_CAPITAL,
                                   position_pct=PAPER_POSITION_PCT)
        stats = data['stats']

        def pct(v, places=2):
            return "—" if v is None else f"{v * 100:+.{places}f}%"

        def tint(label, value):
            label.configure(text_color=COLOR_TEXT_MUTE if value is None
                            else (COLOR_SUCCESS if value >= 0 else COLOR_DANGER))

        lbl = self.paper_stat_labels
        if stats:
            lbl['expectancy'].configure(text=pct(stats['expectancy']))
            tint(lbl['expectancy'], stats['expectancy'])
            lbl['win_rate'].configure(text=f"{stats['win_rate'] * 100:.0f}%", text_color=COLOR_TEXT)
            lbl['alpha'].configure(text=pct(stats['avg_alpha']))
            tint(lbl['alpha'], stats['avg_alpha'])
            lbl['trades'].configure(text=str(stats['trades']), text_color=COLOR_TEXT)
            lbl['drawdown'].configure(text=f"-{stats['max_drawdown'] * 100:.1f}%",
                                      text_color=COLOR_TEXT_DIM)
        else:
            for key in ('expectancy', 'win_rate', 'alpha', 'trades', 'drawdown'):
                lbl[key].configure(text="—", text_color=COLOR_TEXT_MUTE)
        lbl['open'].configure(text=str(data['open_count']), text_color=COLOR_TEXT)

        n = stats['trades'] if stats else 0
        if not stats:
            note = ("No closed trades yet — one is recorded when its sell/cover signal fires."
                    if data['open_count'] else
                    "Nothing recorded yet. The ledger fills as alerts fire; with the "
                    "HIGH/CRITICAL filter that is a slow drip.")
        elif n < 30:
            note = (f"Only {n} closed trade{'' if n == 1 else 's'}. Below about 30 these "
                    f"figures are noise — a run of luck looks the same as an edge.")
        elif stats['avg_alpha'] is not None and stats['avg_alpha'] <= 0 < stats['expectancy']:
            note = ("Profitable, but not beating the market over the same windows — the "
                    "gains look like drift rather than the analyser picking winners.")
        else:
            note = ""
        self.paper_note.configure(text=note)

        for widget in self.paper_scroll.winfo_children():
            widget.destroy()

        self._paper_heading("Open positions",
                            "marked to live prices — not yet part of the record")
        if pending:
            ctk.CTkLabel(self.paper_scroll, text="Fetching prices…", font=UI(12),
                         text_color=COLOR_TEXT_MUTE).pack(pady=14)
        elif data['positions']:
            for p in data['positions']:
                self._paper_row(
                    p['ticker'], p['direction'], p['entry_price'], p.get('current_price'),
                    p.get('unrealised_pct'), (p.get('opened_at') or '')[5:16].replace('T', ' '),
                    note=(f"{p['progress'] * 100:.0f}% to target"
                          if p.get('progress') is not None else ""))
        else:
            ctk.CTkLabel(self.paper_scroll, text="No open positions", font=UI(12),
                         text_color=COLOR_TEXT_MUTE).pack(pady=14)

        self._paper_heading("Closed trades",
                            f"the record, after {data['cost_pct'] * 100:.2f}% costs")
        if data['closed_recent']:
            for t in data['closed_recent']:
                self._paper_row(
                    t['ticker'], t['direction'], t['entry_price'], t['exit_price'],
                    t['net_pct'], (t.get('closed_at') or '')[5:16].replace('T', ' '),
                    note={'target_hit': "target hit", 'stop_loss': "stop-loss",
                          'horizon_expired': "time stop"}.get(t.get('reason'), t.get('reason') or ''))
        else:
            ctk.CTkLabel(self.paper_scroll, text="No closed trades yet", font=UI(12),
                         text_color=COLOR_TEXT_MUTE).pack(pady=14)

    def _paper_heading(self, title, subtitle):
        head = ctk.CTkFrame(self.paper_scroll, fg_color="transparent")
        head.pack(fill='x', pady=(14, 6))
        ctk.CTkLabel(head, text=title.upper(), font=UI(11, "bold"),
                     text_color=COLOR_TEXT_DIM).pack(side='left')
        ctk.CTkLabel(head, text=subtitle, font=UI(10),
                     text_color=COLOR_TEXT_MUTE).pack(side='left', padx=10)

    def _paper_row(self, ticker, direction, entry, exit_price, pct_value, when, note=""):
        is_short = direction == 'SHORT'
        accent = COLOR_DANGER if is_short else COLOR_SUCCESS
        row = ctk.CTkFrame(self.paper_scroll, fg_color=COLOR_PANEL, corner_radius=RADIUS,
                           border_color=COLOR_LINE, border_width=1)
        row.pack(fill='x', pady=2)
        ctk.CTkFrame(row, fg_color=accent, width=4, height=38, corner_radius=RADIUS).pack(side='left', fill='y')
        ctk.CTkLabel(row, text=ticker, width=70, anchor='w', font=MONO(13, "bold"),
                     text_color=COLOR_TEXT).pack(side='left', padx=10)
        ctk.CTkLabel(row, text="Short" if is_short else "Long", width=50, anchor='w',
                     font=UI(10, "bold"), text_color=accent).pack(side='left')
        ctk.CTkLabel(row, text=f"{entry:.2f}", width=80, anchor='w', font=MONO(12),
                     text_color=COLOR_TEXT_DIM).pack(side='left', padx=6)
        ctk.CTkLabel(row, text=f"{exit_price:.2f}" if exit_price else "—", width=80,
                     anchor='w', font=MONO(12), text_color=COLOR_TEXT).pack(side='left', padx=6)
        colour = COLOR_TEXT_MUTE if pct_value is None else (
            COLOR_SUCCESS if pct_value >= 0 else COLOR_DANGER)
        ctk.CTkLabel(row, text="—" if pct_value is None else f"{pct_value * 100:+.2f}%",
                     width=90, anchor='w', font=MONO(13, "bold"),
                     text_color=colour).pack(side='left', padx=6)
        ctk.CTkLabel(row, text=note, anchor='w', font=UI(10),
                     text_color=COLOR_TEXT_MUTE).pack(side='left', padx=6)
        ctk.CTkLabel(row, text=when, anchor='e', font=MONO(10),
                     text_color=COLOR_TEXT_MUTE).pack(side='right', padx=12)

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
            self.log_queue.put(f"Added {ticker} at ${buy_price}")
            self.entry_ticker.delete(0, tk.END)
            self.entry_price.delete(0, tk.END)
            self.refresh_portfolio_list()
        else:
            self.log_queue.put(f"Updated {ticker}")
            self.refresh_portfolio_list()

    def reset_portfolio(self):
        if messagebox.askyesno("Confirm Reset", "Are you sure you want to WIPE ALL portfolio data?"):
            self.portfolio_mgr.reset_portfolio()
            self.log_queue.put("Portfolio cleared.")
            self.refresh_portfolio_list()

    def delete_stock(self, ticker):
        self.portfolio_mgr.remove_stock(ticker)
        self.log_queue.put(f"Removed {ticker}")
        self.refresh_portfolio_list(fetch_prices=False)

    def refresh_portfolio_list(self, fetch_prices=True):
        for widget in self.portfolio_list_frame.winfo_children():
            widget.destroy()
        portfolio = self.portfolio_mgr.get_portfolio()
        self.portfolio_rows = {} 
        if not portfolio:
             ctk.CTkLabel(self.portfolio_list_frame, text="No holdings yet", font=UI(14), text_color=COLOR_TEXT_MUTE).pack(pady=40)
             self._update_charts({})
             return

        for ticker, data in portfolio.items():
            buy_price = data.get('buy_price', 0.0)
            row = ctk.CTkFrame(self.portfolio_list_frame, fg_color=COLOR_PANEL, corner_radius=RADIUS, border_color=COLOR_LINE, border_width=1)
            row.pack(fill='x', pady=2)
            ctk.CTkFrame(row, fg_color=COLOR_ACCENT, width=4, height=40, corner_radius=RADIUS).pack(side='left', fill='y')
            ctk.CTkLabel(row, text=ticker, width=80, font=MONO(14, "bold"), text_color=COLOR_TEXT).pack(side='left', padx=10)
            ctk.CTkLabel(row, text=f"${buy_price:.2f}", width=100, font=MONO(13), text_color=COLOR_TEXT_DIM).pack(side='left', padx=10)
            lbl_live = ctk.CTkLabel(row, text="---", width=100, font=MONO(13), text_color=COLOR_TEXT)
            lbl_live.pack(side='left', padx=10)
            lbl_pct = ctk.CTkLabel(row, text="---", width=100, font=MONO(13, "bold"))
            lbl_pct.pack(side='left', padx=10)
            lbl_gain = ctk.CTkLabel(row, text="---", width=100, font=MONO(13))
            lbl_gain.pack(side='left', padx=10)
            ctk.CTkButton(row, text="×", width=30, height=30, fg_color="transparent", text_color=COLOR_TEXT_MUTE, hover_color=COLOR_PANEL_HI, 
                         font=UI(16), command=lambda t=ticker: self.delete_stock(t)).pack(side='right', padx=10)
            self.portfolio_rows[ticker] = {'lbl_live': lbl_live, 'lbl_pct': lbl_pct, 'lbl_gain': lbl_gain, 'buy_price': buy_price}
        
        if fetch_prices:
            threading.Thread(target=self._fetch_and_update_prices, args=(list(portfolio.keys()),), daemon=True).start()
        else:
            self._update_charts(self._mock_chart_data(portfolio))

    def _mock_chart_data(self, portfolio):
        # Placeholder until a live fetch runs. Holdings with no recorded buy
        # price are left out: a pie of all-zero wedges cannot be drawn.
        return {t: {'buy': d['buy_price'], 'current': d['buy_price'], 'pl_pct': 0.0}
                for t, d in portfolio.items() if d.get('buy_price', 0) > 0}

    def _fetch_and_update_prices(self, tickers):
        data_map = {}
        try:
            if not tickers: return
            # Shared with the server/watch checker so both show the same
            # number - and, importantly, one that includes extended-hours
            # trading instead of the last regular-session close.
            data_map = price_lookup.fetch_prices(tickers)
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
                        lbl_pct.configure(text="N/A", text_color=COLOR_TEXT_MUTE)
                        lbl_gain.configure(text="Log Price", text_color=COLOR_TEXT_MUTE)
                        portfolio_data_for_charts[ticker] = {'buy': 0, 'current': live_price, 'pl_pct': 0}
                else:
                    lbl_live.configure(text="ERR", text_color=COLOR_DANGER)
        self._update_charts(portfolio_data_for_charts)

    def _update_charts(self, data):
        if not data:
            # Nothing to plot (portfolio emptied, or no prices came back):
            # blank the figures rather than leaving the previous holdings'
            # charts on screen.
            for fig, canvas in ((self.figure_pie, self.canvas_pie),
                                (self.figure_bar, self.canvas_bar)):
                if fig is not None and canvas is not None:
                    fig.clear()
                    canvas.draw()
            return
        tickers = list(data.keys())
        current_vals = [d['current'] for d in data.values()]
        pl_pcts = [d['pl_pct'] for d in data.values()]
        colors = [COLOR_SUCCESS if p >= 0 else COLOR_DANGER for p in pl_pcts]
        
        # Pie
        if self.figure_pie: self.figure_pie.clear()
        else: self.figure_pie = plt.Figure(figsize=(4, 3), dpi=80, facecolor=COLOR_PANEL)
        ax_pie = self.figure_pie.add_subplot(111)
        ax_pie.pie(current_vals, labels=tickers, autopct='%1.1f%%', startangle=90, 
                  colors=CHART_SERIES,
                  textprops={'color': COLOR_TEXT_DIM, 'fontsize': 8})
        # Honest label: the portfolio stores a buy price but no share count,
        # so this is the relative share price of each holding - NOT how much
        # money is in each. Calling it "allocation" implied otherwise.
        ax_pie.set_title("SHARE PRICE WEIGHT (not position size)", color=COLOR_TEXT_DIM, fontsize=9)
        
        if self.canvas_pie: self.canvas_pie.draw()
        else:
            self.canvas_pie = FigureCanvasTkAgg(self.figure_pie, master=self.chart_frame_left)
            self.canvas_pie.draw()
            self.canvas_pie.get_tk_widget().pack(fill='both', expand=True)

        # Bar
        if self.figure_bar: self.figure_bar.clear()
        else: self.figure_bar = plt.Figure(figsize=(4, 3), dpi=80, facecolor=COLOR_PANEL)
        ax_bar = self.figure_bar.add_subplot(111)
        ax_bar.set_facecolor(COLOR_PANEL)
        bars = ax_bar.bar(tickers, pl_pcts, color=colors)
        ax_bar.axhline(0, color=COLOR_LINE_HI, linewidth=1)
        ax_bar.set_title("PERFORMANCE (P/L %)", color=COLOR_TEXT_DIM, fontsize=9)
        ax_bar.tick_params(axis='x', colors=COLOR_TEXT_DIM, labelsize=8, length=0)
        ax_bar.tick_params(axis='y', colors=COLOR_TEXT_MUTE, labelsize=8, length=0)
        ax_bar.spines['bottom'].set_color(COLOR_LINE)
        ax_bar.spines['left'].set_color(COLOR_LINE)
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
        bar = ctk.CTkFrame(self.frame_sources, fg_color=COLOR_PANEL, corner_radius=RADIUS, border_width=1, border_color=COLOR_LINE)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        
        self.entry_source_name = ctk.CTkEntry(bar, placeholder_text="Source Name", width=200,
                                             fg_color=COLOR_BG, border_color=COLOR_LINE, text_color=COLOR_TEXT, corner_radius=RADIUS_SM, font=UI(12))
        self.entry_source_name.pack(side='left', padx=10, pady=10)
        
        self.entry_source_url = ctk.CTkEntry(bar, placeholder_text="Source URL", width=400,
                                            fg_color=COLOR_BG, border_color=COLOR_LINE, text_color=COLOR_TEXT, corner_radius=RADIUS_SM, font=UI(12))
        self.entry_source_url.pack(side='left', padx=(0, 10), pady=10)
        
        ctk.CTkButton(bar, text="Add source", command=self.add_source, width=120,
                     fg_color=COLOR_ACCENT, text_color="#FFFFFF", hover_color=COLOR_ACCENT_HOVER, corner_radius=RADIUS_SM, border_width=0, font=UI(11, "bold")).pack(side='left', padx=10)
        
        ctk.CTkButton(bar, text="Reset", command=self.reset_sources, width=140,
                     fg_color="transparent", border_width=0, text_color=COLOR_DANGER,
                     hover_color=COLOR_DANGER_SOFT, corner_radius=RADIUS_SM, font=UI(11, "bold")).pack(side='right', padx=10)
        
        # Sources List
        list_frame = ctk.CTkScrollableFrame(self.frame_sources, fg_color="transparent", corner_radius=RADIUS)
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
            ctk.CTkLabel(self.sources_list_frame, text="No sources yet", font=UI(14), text_color=COLOR_TEXT_MUTE).pack(pady=40)
            return
        
        for source in sources:
            source_id = source['id']
            name = source['name']
            url = source['url']
            enabled = source.get('enabled', True)
            
            row = ctk.CTkFrame(self.sources_list_frame, fg_color=COLOR_PANEL, corner_radius=RADIUS, border_color=COLOR_LINE, border_width=1)
            row.pack(fill='x', pady=3, padx=5)
            
            # Status indicator
            status_color = COLOR_SUCCESS if enabled else COLOR_TEXT_MUTE
            ctk.CTkFrame(row, fg_color=status_color, width=4, height=50, corner_radius=RADIUS).pack(side='left', fill='y')
            
            # Source info
            info_frame = ctk.CTkFrame(row, fg_color="transparent")
            info_frame.pack(side='left', fill='both', expand=True, padx=10, pady=5)
            
            ctk.CTkLabel(info_frame, text=name, font=UI(13, "bold"), text_color=COLOR_ACCENT, anchor="w").pack(anchor="w")
            ctk.CTkLabel(info_frame, text=url, font=MONO(10), text_color=COLOR_TEXT_DIM, anchor="w").pack(anchor="w")
            
            # Controls
            ctk.CTkButton(row, text="✓" if enabled else "○", width=40, height=40,
                         fg_color="transparent", text_color=status_color, hover_color=COLOR_PANEL_HI,
                         font=UI(18, "bold"), command=lambda sid=source_id: self.toggle_source(sid)).pack(side='right', padx=5)
            
            ctk.CTkButton(row, text="×", width=40, height=40,
                         fg_color="transparent", text_color=COLOR_TEXT_MUTE, hover_color=COLOR_DANGER_SOFT,
                         font=UI(18), command=lambda sid=source_id: self.delete_source(sid)).pack(side='right', padx=5)

    def _setup_keywords_tab(self):
        """Setup the Keywords management tab."""
        self.frame_keywords.grid_rowconfigure(1, weight=1)
        self.frame_keywords.grid_columnconfigure(0, weight=1)
        self.frame_keywords.grid_columnconfigure(1, weight=1)
        
        # Top Controls
        bar = ctk.CTkFrame(self.frame_keywords, fg_color=COLOR_PANEL, corner_radius=RADIUS, border_width=1, border_color=COLOR_LINE)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        
        ctk.CTkLabel(bar, text="Keywords", font=UI(14, "bold"), text_color=COLOR_ACCENT).pack(side='left', padx=20, pady=10)

        # These keywords only drive scoring when the LLM is switched off.
        # Without this notice, editing them while the LLM is active looks
        # like it should change the alerts, and silently does nothing. Row 1
        # is reserved for it so it can be shown/hidden as settings change.
        self.frame_keywords.grid_rowconfigure(1, weight=0)
        self.frame_keywords.grid_rowconfigure(2, weight=1)
        self._keyword_list_row = 2
        self.keywords_notice = ctk.CTkFrame(self.frame_keywords, fg_color="#2A1D05", corner_radius=RADIUS,
                                            border_width=1, border_color=COLOR_WARN)
        ctk.CTkLabel(self.keywords_notice,
                     text="⚠  INACTIVE - an AI engine is doing the analysis. "
                          "These keywords are only used when both USE_LOCAL_LLM "
                          "and USE_CLOUD_AI are False.",
                     font=UI(11), text_color=COLOR_WARN,
                     justify="left").pack(padx=16, pady=8)
        self._setup_keywords_banner()
        
        ctk.CTkButton(bar, text="Reset", command=self.reset_keywords, width=140,
                     fg_color="transparent", border_width=0, text_color=COLOR_DANGER,
                     hover_color=COLOR_DANGER_SOFT, corner_radius=RADIUS_SM, font=UI(11, "bold")).pack(side='right', padx=10)
        
        # Positive Keywords Section
        pos_frame = ctk.CTkFrame(self.frame_keywords, fg_color="transparent")
        pos_frame.grid(row=self._keyword_list_row, column=0, sticky="nsew", padx=(10, 5), pady=5)
        
        # Positive Header
        pos_header = ctk.CTkFrame(pos_frame, fg_color="#0A2010", corner_radius=RADIUS, border_width=1, border_color=COLOR_SUCCESS)
        pos_header.pack(fill='x', pady=(0, 5))
        ctk.CTkLabel(pos_header, text="Positive", font=UI(12, "bold"), text_color=COLOR_SUCCESS).pack(pady=5)
        
        # Positive Input
        pos_input = ctk.CTkFrame(pos_frame, fg_color=COLOR_PANEL, corner_radius=RADIUS)
        pos_input.pack(fill='x', pady=5)
        
        self.entry_pos_keyword = ctk.CTkEntry(pos_input, placeholder_text="Keyword", width=150,
                                              fg_color=COLOR_BG, border_color=COLOR_LINE, text_color=COLOR_SUCCESS, corner_radius=RADIUS)
        self.entry_pos_keyword.pack(side='left', padx=5, pady=5)
        
        self.entry_pos_weight = ctk.CTkEntry(pos_input, placeholder_text="Weight 1-10", width=100,
                                             fg_color=COLOR_BG, border_color=COLOR_LINE, text_color=COLOR_TEXT, corner_radius=RADIUS)
        self.entry_pos_weight.pack(side='left', padx=5, pady=5)
        
        ctk.CTkButton(pos_input, text="Add", command=lambda: self.add_keyword("positive"), width=70,
                     fg_color=COLOR_SUCCESS_SOFT, text_color=COLOR_SUCCESS, hover_color=COLOR_PANEL_HI, corner_radius=RADIUS_SM, border_width=0, font=UI(10, "bold")).pack(side='left', padx=5)
        
        # Positive List
        self.pos_keywords_list = ctk.CTkScrollableFrame(pos_frame, fg_color="transparent", corner_radius=RADIUS)
        self.pos_keywords_list.pack(fill='both', expand=True, pady=5)
        
        # Negative Keywords Section
        neg_frame = ctk.CTkFrame(self.frame_keywords, fg_color="transparent")
        neg_frame.grid(row=self._keyword_list_row, column=1, sticky="nsew", padx=(5, 10), pady=5)
        
        # Negative Header
        neg_header = ctk.CTkFrame(neg_frame, fg_color="#201010", corner_radius=RADIUS, border_width=1, border_color=COLOR_DANGER)
        neg_header.pack(fill='x', pady=(0, 5))
        ctk.CTkLabel(neg_header, text="Negative", font=UI(12, "bold"), text_color=COLOR_DANGER).pack(pady=5)
        
        # Negative Input
        neg_input = ctk.CTkFrame(neg_frame, fg_color=COLOR_PANEL, corner_radius=RADIUS)
        neg_input.pack(fill='x', pady=5)
        
        self.entry_neg_keyword = ctk.CTkEntry(neg_input, placeholder_text="Keyword", width=150,
                                              fg_color=COLOR_BG, border_color=COLOR_LINE, text_color=COLOR_DANGER, corner_radius=RADIUS)
        self.entry_neg_keyword.pack(side='left', padx=5, pady=5)
        
        self.entry_neg_weight = ctk.CTkEntry(neg_input, placeholder_text="Weight 1-10", width=100,
                                             fg_color=COLOR_BG, border_color=COLOR_LINE, text_color=COLOR_TEXT, corner_radius=RADIUS)
        self.entry_neg_weight.pack(side='left', padx=5, pady=5)
        
        ctk.CTkButton(neg_input, text="Add", command=lambda: self.add_keyword("negative"), width=70,
                     fg_color=COLOR_DANGER_SOFT, text_color=COLOR_DANGER, hover_color=COLOR_PANEL_HI, corner_radius=RADIUS_SM, border_width=0, font=UI(10, "bold")).pack(side='left', padx=5)
        
        # Negative List
        self.neg_keywords_list = ctk.CTkScrollableFrame(neg_frame, fg_color="transparent", corner_radius=RADIUS)
        self.neg_keywords_list.pack(fill='both', expand=True, pady=5)
        
        self.refresh_keywords_list()
    
    def _setup_keywords_banner(self):
        """Show the 'inactive' notice only while an AI engine is selected."""
        if config.USE_CLOUD_AI or config.USE_LOCAL_LLM:
            self.keywords_notice.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        else:
            self.keywords_notice.grid_forget()

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
            row = ctk.CTkFrame(self.pos_keywords_list, fg_color=COLOR_PANEL, corner_radius=RADIUS, border_color=COLOR_LINE, border_width=1)
            row.pack(fill='x', pady=2, padx=3)
            
            # Weight indicator (colored bar)
            bar_width = max(3, int(weight * 0.5))
            ctk.CTkFrame(row, fg_color=COLOR_SUCCESS, width=bar_width, height=30, corner_radius=RADIUS).pack(side='left', fill='y')
            
            ctk.CTkLabel(row, text=keyword, font=UI(11), text_color=COLOR_TEXT, anchor="w", width=150).pack(side='left', padx=10)
            ctk.CTkLabel(row, text=f"+{weight}", font=MONO(11, "bold"), text_color=COLOR_SUCCESS, width=50).pack(side='left')
            ctk.CTkButton(row, text="×", width=30, height=30, fg_color="transparent", text_color=COLOR_TEXT_MUTE, hover_color=COLOR_DANGER_SOFT,
                         font=UI(14), command=lambda k=keyword: self.delete_keyword(k, "positive")).pack(side='right', padx=5)
        
        if not pos_keywords:
            ctk.CTkLabel(self.pos_keywords_list, text="No positive keywords", font=UI(11), text_color=COLOR_TEXT_MUTE).pack(pady=20)
        
        # Negative keywords
        neg_keywords = self.keyword_mgr.get_negative_keywords()
        for keyword, weight in sorted(neg_keywords.items(), key=lambda x: x[1]):  # Sort by weight ascending (most negative first)
            row = ctk.CTkFrame(self.neg_keywords_list, fg_color=COLOR_PANEL, corner_radius=RADIUS, border_color=COLOR_LINE, border_width=1)
            row.pack(fill='x', pady=2, padx=3)
            
            # Weight indicator (colored bar)
            bar_width = max(3, int(abs(weight) * 0.5))
            ctk.CTkFrame(row, fg_color=COLOR_DANGER, width=bar_width, height=30, corner_radius=RADIUS).pack(side='left', fill='y')
            
            ctk.CTkLabel(row, text=keyword, font=UI(11), text_color=COLOR_TEXT, anchor="w", width=150).pack(side='left', padx=10)
            ctk.CTkLabel(row, text=f"{weight}", font=MONO(11, "bold"), text_color=COLOR_DANGER, width=50).pack(side='left')
            ctk.CTkButton(row, text="×", width=30, height=30, fg_color="transparent", text_color=COLOR_TEXT_MUTE, hover_color=COLOR_DANGER_SOFT,
                         font=UI(14), command=lambda k=keyword: self.delete_keyword(k, "negative")).pack(side='right', padx=5)
        
        if not neg_keywords:
            ctk.CTkLabel(self.neg_keywords_list, text="No negative keywords", font=UI(11), text_color=COLOR_TEXT_MUTE).pack(pady=20)

if __name__ == "__main__":
    app = StockAppGUI()
    app.mainloop()
