import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox
import threading
import queue
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

        # Redirect Console
        sys.stdout = ConsoleRedirector(self.log_queue)
        sys.stderr = ConsoleRedirector(self.log_queue, "[ERROR] ")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- Sidebar (HUD Panel) ---
        self.sidebar_frame = ctk.CTkFrame(self, width=220, corner_radius=0, fg_color=COLOR_SIDEBAR)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(6, weight=1)

        # Logo
        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="STOCKS WATCHER\n[OWL_SYSTEM]", 
                                      font=("Segoe UI", 20, "bold"), text_color=COLOR_ACCENT)
        self.logo_label.grid(row=0, column=0, padx=20, pady=(30, 20))

        # Status Indicator
        self.status_label = ctk.CTkLabel(self.sidebar_frame, text="STATUS: OFFLINE", text_color=COLOR_DANGER, font=("Consolas", 12, "bold"))
        self.status_label.grid(row=1, column=0, padx=20, pady=(0, 20))

        # Controls
        self._create_sidebar_btn("INITIALIZE WATCHER", self.start_backend, COLOR_SUCCESS, 2)
        self._create_sidebar_btn("TERMINATE PROCESS", self.stop_backend, COLOR_DANGER, 3, state="disabled")
        self._create_sidebar_btn("RELOAD SETTINGS", self.reload_settings, "#FFA500", 4)
        self._create_sidebar_btn("SYSTEM OPTIONS", self.open_settings, COLOR_ACCENT, 5)
        self._create_sidebar_btn("SYSTEM_DIAGNOSTIC", self.send_test_alert, COLOR_TEXT, 6)

        # --- Main View ---
        self.main_area = ctk.CTkFrame(self, fg_color="transparent")
        self.main_area.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.main_area.grid_rowconfigure(1, weight=1)
        self.main_area.grid_columnconfigure(0, weight=1)

        # Styled Tabs
        self.tab_var =  ctk.StringVar(value="LOGS")
        self.seg_button = ctk.CTkSegmentedButton(self.main_area, values=["LOGS", "PORTFOLIO", "SOURCES", "KEYWORDS"], 
                                                command=self.switch_tab,
                                                selected_color=COLOR_SIDEBAR,
                                                selected_hover_color=COLOR_SIDEBAR,
                                                unselected_color=COLOR_BG,
                                                unselected_hover_color="#1a1a1a",
                                                text_color=COLOR_ACCENT,
                                                corner_radius=0,
                                                border_width=1,
                                                font=("Consolas", 12, "bold"))
        self.seg_button.set("LOGS")
        self.seg_button.grid(row=0, column=0, sticky="ew", pady=(0, 15))

        # -- Views --
        self.frame_logs = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.frame_portfolio = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.frame_sources = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.frame_keywords = ctk.CTkFrame(self.main_area, fg_color="transparent")
        
        self.frame_logs.grid(row=1, column=0, sticky="nsew") # Default

        # Tab 1: Terminal Logs
        self.log_area = ctk.CTkTextbox(self.frame_logs, state='disabled', 
                                      font=("Consolas", 11), 
                                      fg_color="#000000", 
                                      text_color=COLOR_SUCCESS,
                                      border_color=COLOR_SIDEBAR, border_width=2, corner_radius=0)
        self.log_area.pack(expand=True, fill='both')

        # Tab 2: Portfolio Charts Init
        self.figure_pie = None
        self.figure_bar = None
        self.canvas_pie = None
        self.canvas_bar = None
        
        self._setup_portfolio_tab()
        self._setup_sources_tab()
        self._setup_keywords_tab()

        self.after(100, self.process_log_queue)
        self.refresh_portfolio_list()

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
        win.geometry("500x300")
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
        except:
            current_topic = "stocks_ai_secret"
        
        e_topic = add_input("NTFY NOTIFICATION TOPIC:", current_topic)
        
        ctk.CTkLabel(scroll, text="ℹ️ Change this to a unique value for privacy", 
                    text_color="#666", font=("Consolas", 9)).pack(pady=(5, 20))
        
        def save():
            import json
            new_settings = {
                "NTFY_TOPIC": e_topic.get().strip()
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
        self.frame_logs.grid_forget()
        self.frame_portfolio.grid_forget()
        self.frame_sources.grid_forget()
        self.frame_keywords.grid_forget()

        if value == "LOGS":
            self.frame_logs.grid(row=1, column=0, sticky="nsew")
        elif value == "PORTFOLIO":
            self.frame_portfolio.grid(row=1, column=0, sticky="nsew")
        elif value == "SOURCES":
            self.frame_sources.grid(row=1, column=0, sticky="nsew")
        elif value == "KEYWORDS":
            self.frame_keywords.grid(row=1, column=0, sticky="nsew")

    def log_callback(self, msg):
        self.log_queue.put(msg)

    # The watcher is meant to run for days; without a cap the log textbox
    # grows until it exhausts memory.
    MAX_LOG_LINES = 2000

    def process_log_queue(self):
        drained = False
        while not self.log_queue.empty():
            msg = self.log_queue.get()
            self.log_area.configure(state='normal')
            self.log_area.insert(tk.END, f"> {msg}\n")
            drained = True

        if drained:
            line_count = int(self.log_area.index("end-1c").split('.')[0])
            if line_count > self.MAX_LOG_LINES:
                trim_to = line_count - self.MAX_LOG_LINES
                self.log_area.delete("1.0", f"{trim_to}.0")
            self.log_area.see(tk.END)
            self.log_area.configure(state='disabled')

        self.after(100, self.process_log_queue)

    def start_backend(self):
        if self.backend and self.backend.running: return 
        self.log_queue.put("Initializing System Core...")
        self.backend = StockAppBackend(log_callback=self.log_callback)
        self.backend.start()
        self.btn_2.configure(state='disabled', border_color="#333", text_color="#333")
        self.btn_3.configure(state='normal', border_color=COLOR_DANGER, text_color=COLOR_DANGER)
        self.status_label.configure(text="STATUS: ONLINE", text_color=COLOR_SUCCESS)

    def stop_backend(self):
        if self.backend:
            self.log_queue.put("Terminating System Core...")
            self.backend.stop()
            self.backend = None
        self.btn_2.configure(state='normal', border_color=COLOR_SUCCESS, text_color=COLOR_SUCCESS)
        self.btn_3.configure(state='disabled', border_color="#333", text_color="#333")
        self.status_label.configure(text="STATUS: OFFLINE", text_color=COLOR_DANGER)

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
            if self.backend and hasattr(self.backend, 'analyzer'):
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
        self.after(0, lambda: self._update_ui_prices(data_map))

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
        ax_pie.set_title("ALLOCATION (Estimated)", color="white", fontsize=10)
        
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
        
        ctk.CTkButton(bar, text="RESET TO DEFAULTS", command=self.reset_keywords, width=140,
                     fg_color="transparent", border_width=1, border_color=COLOR_DANGER, text_color=COLOR_DANGER,
                     hover_color="#220000", corner_radius=0, font=("Consolas", 11, "bold")).pack(side='right', padx=10)
        
        # Positive Keywords Section
        pos_frame = ctk.CTkFrame(self.frame_keywords, fg_color="transparent")
        pos_frame.grid(row=1, column=0, sticky="nsew", padx=(10, 5), pady=5)
        
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
        neg_frame.grid(row=1, column=1, sticky="nsew", padx=(5, 10), pady=5)
        
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
            if self.backend and hasattr(self.backend, 'analyzer'):
                self.backend.analyzer.reload_keywords()
    
    def delete_keyword(self, keyword, keyword_type):
        """Delete a keyword."""
        if self.keyword_mgr.remove_keyword(keyword, keyword_type):
            self.log_queue.put(f"Removed {keyword_type} keyword: {keyword}")
            self.refresh_keywords_list()
            # Reload keywords in analyzer
            if self.backend and hasattr(self.backend, 'analyzer'):
                self.backend.analyzer.reload_keywords()
    
    def reset_keywords(self):
        """Reset keywords to defaults."""
        if messagebox.askyesno("Confirm Reset", "Reset to default keywords?"):
            self.keyword_mgr.reset_to_defaults()
            self.log_queue.put("Keywords reset to defaults")
            self.refresh_keywords_list()
            # Reload keywords in analyzer
            if self.backend and hasattr(self.backend, 'analyzer'):
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
