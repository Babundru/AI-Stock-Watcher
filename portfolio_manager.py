import json
import os

class PortfolioManager:
    def __init__(self, filename="data/portfolio.json"):
        self.filename = filename
        self.portfolio = self._load_portfolio()

    def _load_portfolio(self):
        if not os.path.exists(self.filename):
            return {}
        try:
            with open(self.filename, 'r') as f:
                data = json.load(f)
                
            # Migration: List -> Dict
            if isinstance(data, list):
                new_data = {}
                for ticker in data:
                    new_data[ticker] = {"buy_price": 0.0}
                return new_data
            
            return data
        except json.JSONDecodeError:
            return {}

    def save_portfolio(self):
        try:
            with open(self.filename, 'w') as f:
                json.dump(self.portfolio, f, indent=4)
        except Exception as e:
            print(f"Error saving portfolio: {e}")

    def add_stock(self, ticker, buy_price=0.0):
        ticker = ticker.upper().strip()
        
        # We allow overwriting to update buy price if user adds again
        if ticker:
            self.portfolio[ticker] = {"buy_price": float(buy_price)}
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
        return ticker.upper().strip() in self.portfolio
