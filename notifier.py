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
        from config import NTFY_TOPIC, NOTIFY_OWNERSHIP, NOTIFICATIONS_ENABLED
        self.ntfy_topic = NTFY_TOPIC
        self.notify_ownership = NOTIFY_OWNERSHIP
        # Master mute. Distinct from having no topic: the topic stays
        # configured, so unmuting needs no retyping. See set_enabled.
        self.enabled = NOTIFICATIONS_ENABLED
        self._warned_no_topic = False

    def apply_settings(self):
        """Pick up the current config values (topic, ownership tag, mute)
        after the dashboard saved new ones, without rebuilding anything."""
        import config
        if config.NTFY_TOPIC != self.ntfy_topic:
            self._warned_no_topic = False
        self.ntfy_topic = config.NTFY_TOPIC
        self.notify_ownership = config.NOTIFY_OWNERSHIP
        self.enabled = config.NOTIFICATIONS_ENABLED

    def set_enabled(self, enabled, persist=True):
        """Turn phone notifications on or off, effective immediately.

        Writes the choice to data/settings.json by default so it survives a
        restart. Everything else about the app carries on unchanged while
        muted - scanning, analysis, watches and the paper-trade record are
        all untouched; only the push to ntfy.sh is suppressed.
        """
        self.enabled = bool(enabled)
        if persist:
            import config
            config.save_setting("NOTIFICATIONS_ENABLED", self.enabled)
        return self.enabled

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
        Sends a system-level notification (e.g. App Start/Stop). Returns
        whether ntfy actually accepted it, so a caller like the dashboard's
        test-notification button can tell the user it worked.
        """
        print("\n" + "="*50)
        print(f"🤖 SYSTEM: {title}")
        print(f"📝 Message: {message}")
        print("="*50 + "\n")

        return self._send_ntfy(title, message, priority='default')

    def notify(self, company, article, analysis, is_owned=False):
        """
        Sends a notification (Prints to console for MVP).

        Args:
            company: Company name
            article: Article dict
            analysis: Analysis dict
            is_owned: Whether the stock is in user's portfolio

        Returns whether ntfy accepted it, so a caller like the desktop test
        button can tell "delivered" apart from "muted" or "no topic set"
        instead of reporting success either way.
        """
        # `or`, not a .get() default: the LLM can return the key with a null
        # value, and None.upper() would drop the alert with a traceback.
        impact = (analysis.get('impact') or 'UNKNOWN').upper()
        sentiment = (analysis.get('sentiment') or 'UNKNOWN').upper()

        # Same threshold main.py filters on, read live from config rather
        # than cached here, so the sensitivity slider applies immediately and
        # the two cannot drift apart.
        import config
        if not config.impact_passes(impact):
            return False

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

        # Spell out the trade the alert implies, so the entry notification
        # is as actionable as the exit one that follows it. Only sentiments
        # that open a watch get an action line.
        action = ""
        if sentiment == "POSITIVE":
            action = "\nAction: BUY (open a long CFD) - a SELL alert follows when to close."
        elif sentiment == "NEGATIVE":
            action = "\nAction: SHORT (open a short CFD) - a BUY BACK alert follows when to close."

        title = f"{company} ({analysis.get('ticker') or '???'}){ownership_tag}"
        # Add emoji to message body instead to avoid Header encoding issues
        message = (
            f"{emoji} {analysis.get('explanation')}\n\n"
            f"Price Prediction: {analysis.get('prediction')}{action}"
        )
        
        # Console Output
        print("\n" + "="*50)
        print(f"TITLE: {title} {emoji}")

        print("="*50)
        print(f"📰 Article: {article.get('title')}")
        print(f"💥 Impact: {impact}")
        print(f"📝 Message: {message}")
        print("="*50 + "\n")
        
        # Send Mobile Notification
        return self._send_ntfy(title, message, priority='high' if impact == 'CRITICAL' else 'default', url=article.get('url'))

    def notify_sell(self, ticker, company, reason, entry_price, current_price,
                    target_price, article_url=None, direction="LONG"):
        """
        Sends an exit-signal notification for a watch closed by
        _check_watches (main.py): either the alerted-on move played out, or
        the expected window passed without it.

        Args:
            ticker: Stock ticker
            company: Company name
            reason: "target_hit", "stop_loss", or "horizon_expired"
            entry_price: Price when the original alert fired
            current_price: Price now
            target_price: Price that would have counted as the move "playing out"
            article_url: Link back to the article that opened the watch
            direction: "LONG" (close by selling) or "SHORT" (close by buying back)
        """
        is_short = (direction or "LONG").upper() == "SHORT"
        price_change = ((current_price - entry_price) / entry_price * 100) if entry_price else 0.0
        # A short earns when the price falls, so report the move from the
        # position's point of view, not the stock's.
        position_pct = -price_change if is_short else price_change

        if reason == "target_hit":
            emoji = "💰"
            reason_text = f"Target of {target_price:.2f} reached - the alerted-on move played out."
        elif reason == "stop_loss":
            emoji = "🛑"
            reason_text = "Stop-loss reached - closing now to cap the loss."
        else:
            emoji = "⏰"
            reason_text = "Expected time window passed without the alerted-on move happening - reassess the position."

        # "Sell" means nothing for a short - that position is closed by
        # buying the CFD back.
        action = "BUY BACK (close the short CFD)" if is_short else "SELL (close the long CFD)"
        title = f"{company} ({ticker}) - {'COVER SHORT' if is_short else 'SELL'} SIGNAL"
        message = (
            f"{emoji} {reason_text}\n\n"
            f"Action: {action}\n"
            f"Entry: {entry_price:.2f} -> Now: {current_price:.2f} "
            f"({price_change:+.1f}% price, {position_pct:+.1f}% on the position)"
        )

        print("\n" + "="*50)
        print(f"TITLE: {title} {emoji}")
        print("="*50)
        print(f"📝 Message: {message}")
        print("="*50 + "\n")

        # An exit signal is rarer and always actionable, unlike a news alert,
        # so it always goes out at high priority - no impact-based gating.
        return self._send_ntfy(title, message, priority='high', url=article_url)

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
        """Returns True if ntfy accepted the notification, False otherwise
        (muted, no topic configured, a non-2xx response, or a request error).

        The mute check comes first and prints nothing: being muted is a
        deliberate state the user can see in the UI, so repeating it on every
        alert would only bury the log.
        """
        if not self.enabled:
            return False

        if not self.ntfy_topic:
            # Warn once rather than silently dropping every alert - otherwise
            # a new user sees the app working but never gets a phone alert.
            if not self._warned_no_topic:
                print("No notification topic set - phone alerts are disabled. "
                      "Set one in Settings to enable them.")
                self._warned_no_topic = True
            return False

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
            resp = requests.post(
                f"https://ntfy.sh/{self.ntfy_topic}",
                data=message.encode('utf-8'),
                headers=headers,
                timeout=10
            )
            return resp.ok
        except Exception as e:
            print(f"Error sending mobile notification: {e}")
            return False
