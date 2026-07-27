import datetime
import pytz
import sys
import unicodedata

# Windows console encoding fix
try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

class Notifier:
    def __init__(self):
        self.timezone = pytz.timezone('US/Eastern')
        # Import config here to avoid circular imports at top level if any
        from config import NTFY_TOPIC, NOTIFY_OWNERSHIP
        self.ntfy_topic = NTFY_TOPIC
        self.notify_ownership = NOTIFY_OWNERSHIP
        self._warned_no_topic = False

    def is_market_open(self):
        """
        Checks if the US stock market (NYSE/Nasdaq) is currently open.
        """
        now = datetime.datetime.now(self.timezone)
        
        # 0 = Monday, 4 = Friday
        if now.weekday() > 4:
            return False
            
        # Market hours: 9:30 AM - 4:00 PM ET
        start_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
        end_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
        
        return start_time <= now <= end_time

    def notify_system(self, title, message):
        """
        Sends a system-level notification (e.g. App Start/Stop).
        """
        print("\n" + "="*50)
        print(f"🤖 SYSTEM: {title}")
        print(f"📝 Message: {message}")
        print("="*50 + "\n")
        
        self._send_ntfy(title, message, priority='default')

    def notify(self, company, article, analysis, is_owned=False):
        """
        Sends a notification (Prints to console for MVP).
        
        Args:
            company: Company name
            article: Article dict
            analysis: Analysis dict
            is_owned: Whether the stock is in user's portfolio
        """
        impact = analysis.get('impact', 'UNKNOWN').upper()
        sentiment = analysis.get('sentiment', 'UNKNOWN').upper()
        
        # We only notify for High or Critical
        if impact not in ['CRITICAL', 'HIGH']:
            return

        # Prepare formatting
        emoji = "🚨" if impact == "CRITICAL" else "📢"
        if sentiment == "POSITIVE":
            emoji = "🚀"
        elif sentiment == "NEGATIVE":
            emoji = "📉"

        # Ownership is deliberately NOT broadcast by default: the ntfy topic
        # is a public channel, and this tag would disclose which stocks are
        # actually held. The Alerts tab shows it locally instead.
        ownership_tag = ""
        if sentiment == "NEGATIVE" and is_owned and self.notify_ownership:
            ownership_tag = " [OWNED]"

        title = f"{company} ({analysis.get('ticker', '???')}){ownership_tag}"
        # Add emoji to message body instead to avoid Header encoding issues
        message = f"{emoji} {analysis.get('explanation')}\n\nPrice Prediction: {analysis.get('prediction')}"
        
        # Console Output
        print("\n" + "="*50)
        print(f"TITLE: {title} {emoji}")

        print("="*50)
        print(f"📰 Article: {article.get('title')}")
        print(f"💥 Impact: {impact}")
        print(f"📝 Message: {message}")
        print("="*50 + "\n")
        
        # Send Mobile Notification
        self._send_ntfy(title, message, priority='high' if impact == 'CRITICAL' else 'default', url=article.get('url'))

    @staticmethod
    def _header_safe(value, max_length=180):
        """Make a string safe to send as an HTTP header value.

        Header values are encoded as latin-1 at send time, so a company name
        scraped from a feed containing a curly apostrophe or CJK characters
        raises UnicodeEncodeError and the alert is lost. Transliterate to
        ASCII and strip anything that could break the header framing. The
        full-fidelity text still goes out in the UTF-8 body.
        """
        if not value:
            return ""
        folded = unicodedata.normalize('NFKD', str(value))
        ascii_only = folded.encode('ascii', 'ignore').decode('ascii')
        # Collapse newlines/control chars rather than letting them through.
        cleaned = ' '.join(ascii_only.split())
        return cleaned[:max_length]

    def _send_ntfy(self, title, message, priority='default', url=None):
        if not self.ntfy_topic:
            # Warn once rather than silently dropping every alert - otherwise
            # a new user sees the app working but never gets a phone alert.
            if not self._warned_no_topic:
                print("No notification topic set - phone alerts are disabled. "
                      "Set one in Settings to enable them.")
                self._warned_no_topic = True
            return

        try:
            import requests
            safe_title = self._header_safe(title) or "Stocks Watcher"
            headers = {
                "Title": safe_title,
                "Priority": priority,
                "Tags": "chart_with_upwards_trend,moneybag"
            }
            # Only pass through a plain http(s) link; anything else is dropped.
            if url and str(url).lower().startswith(('http://', 'https://')):
                safe_url = self._header_safe(url, max_length=500)
                if safe_url:
                    headers["Click"] = safe_url

            # Without a timeout this call can hang the backend thread forever.
            requests.post(
                f"https://ntfy.sh/{self.ntfy_topic}",
                data=message.encode('utf-8'),
                headers=headers,
                timeout=10
            )
        except Exception as e:
            print(f"Error sending mobile notification: {e}")
