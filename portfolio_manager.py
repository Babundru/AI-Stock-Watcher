import json
import os

from local_time import now_local

class PortfolioManager:
    def __init__(self, filename="data/portfolio.json"):
        self.filename = filename
        self.portfolio = self._load_portfolio()

    def _load_portfolio(self):
        if not os.path.exists(self.filename):
            return {}
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Migration: List -> Dict
            if isinstance(data, list):
                new_data = {}
                for ticker in data:
                    new_data[ticker] = {"buy_price": 0.0}
                data = new_data
            if not isinstance(data, dict):
                return {}

            # Older/malformed entries may be missing shares/buy_date (added
            # for the portfolio value graph) - default them so callers never
            # have to guard for a missing key.
            clean = {}
            for ticker, entry in data.items():
                if not isinstance(entry, dict):
                    entry = {"buy_price": 0.0}
                entry.setdefault("buy_price", 0.0)
                entry.setdefault("shares", 0.0)
                entry.setdefault("buy_date", None)
                clean[str(ticker).upper().strip()] = entry

            return clean
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not read {self.filename} ({e}); starting with an empty portfolio")
            return {}

    def reload(self):
        """Re-read the file - for a manager whose file another instance
        has since written."""
        self.portfolio = self._load_portfolio()

    def save_portfolio(self):
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(self.portfolio, f, indent=4)
        except Exception as e:
            print(f"Error saving portfolio: {e}")

    def add_stock(self, ticker, buy_price=0.0, shares=0.0, buy_date=None):
        ticker = ticker.upper().strip()

        # We allow overwriting to update buy price if user adds again
        if ticker:
            self.portfolio[ticker] = {
                "buy_price": float(buy_price),
                "shares": float(shares) if shares else 0.0,
                # Defaults to today so a value/profit history can still be
                # reconstructed forward even if the caller doesn't supply one.
                "buy_date": buy_date or now_local().strftime('%Y-%m-%d'),
            }
            self.save_portfolio()
            return True
        return False

    def remove_stock(self, ticker):
        ticker = ticker.upper().strip()
        if ticker in self.portfolio:
            del self.portfolio[ticker]
            self.save_portfolio()
            return True
        return False
        
    def reset_portfolio(self):
        self.portfolio = {}
        self.save_portfolio()

    def get_portfolio(self):
        return self.portfolio

    def has_stock(self, ticker):
        return bool(ticker) and str(ticker).upper().strip() in self.portfolio
