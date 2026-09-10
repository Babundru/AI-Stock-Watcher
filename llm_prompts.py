import datetime
import json

from reddit_source import SOURCE_PREFIX, is_reddit_article

# Shared between analyzer.py (local Ollama) and cloud_analyzer.py (Anthropic
# API) so the two engines are judged on the exact same prompt - only the
# backend that executes it differs.
#
# Two prompts:
#   build_market_prompt  - the screen, run on every article.
#   build_trade_prompt   - the trade confirmation, run only for the few
#                          articles that pass the screen and the rule checks,
#                          with live price context the screen doesn't have.


def _article_text(article, limit):
    """Scraped body when we got something substantial, else the feed
    description; trimmed to `limit` chars at a sentence boundary."""
    description = article.get('description') or ""
    content = article.get('content') or ""
    text = content if len(content) > 100 else description
    if len(text) > limit:
        text = text[:limit]
        last_period = text.rfind('.')
        if last_period > 0:
            text = text[:last_period + 1]
        text += " ... (truncated)"
    return text


def _published_line(article):
    """'2026-09-10 13:05 UTC (12 min ago)', or 'unknown'."""
    ts = article.get('published_ts')
    if not ts:
        return "unknown"
    try:
        published = datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return "unknown"
    if published.tzinfo is None:
        published = published.replace(tzinfo=datetime.timezone.utc)
    age = datetime.datetime.now(datetime.timezone.utc) - published
    minutes = max(0, int(age.total_seconds() // 60))
    return f"{published.astimezone(datetime.timezone.utc):%Y-%m-%d %H:%M} UTC ({minutes} min ago)"


def _source_note(article):
    """Framing for an article that is not news reporting (empty otherwise).
    Without it a Reddit post reads to the model like a breaking story."""
    if not is_reddit_article(article):
        return ""
    where = article['source'][len(SOURCE_PREFIX):]
    return f"""
        Source: a post on {where} (retail investors on Reddit), followed by its top comments.
        This is NOT a news report - treat every claim in it as unverified opinion. Rate it relevant only if
        it points to a concrete, checkable company event that is new (earnings, a filing, a contract, a
        lawsuit, FDA news, ...), or makes an unusually specific, well-evidenced case for a catalyst.
        Memes, hype, screenshots of personal gains or losses and "YOLO" bets are irrelevant. Use the
        comments to judge the post: pushback, corrections or "this is old news" count against it."""


def build_market_prompt(company, article, market_is_open, portfolio_tickers=None):
    """Build the single-pass conditional relevance/sentiment prompt for one
    article. Returns None if the article has no usable text to analyze."""
    title = article.get('title', 'No Title')
    article_text = _article_text(article, 5000)

    if not article_text or len(article_text.strip()) == 0:
        print(f"Skipping article '{title}' due to empty content/description.")
        return None

    market_context = "The market is currently OPEN." if market_is_open else "The market is currently CLOSED."
    portfolio_context = ""
    if portfolio_tickers:
        portfolio_str = ", ".join(portfolio_tickers)
        portfolio_context = f"\nUser Portfolio (High Priority): {portfolio_str}. IF the news affects these stocks, treat it as HIGHER RELEVANCE."

    return f"""
        You are an expert financial analyst screening news for tradable, company-specific catalysts. Analyze the following news article for potential stock market impact.

        Target Context: {company}
        {portfolio_context}
        Market Status: {market_context}
        Published: {_published_line(article)}{_source_note(article)}

        Article Title: {title}
        Article Text: {article_text}

        --- INSTRUCTIONS ---
        1. Determining RELEVANCE: Is this news likely to significantly move the stock price of {company} or a related major company?
           - Irrelevant: Routine reports, old news rehashes, minor opinion pieces, general fluff,
             "stocks to buy/watch" lists, market wraps and index recaps, price-target roundups,
             articles spread across many companies, promotional or sponsored content.
           - Relevant: Earnings, Mergers, FDA approvals, Lawsuits, Contracts, Analyst Upgrades/Downgrades, Macro events.

        2. IF IRRELEVANT: Return ONLY {{ "is_relevant": false }} and STOP.
        3. IF RELEVANT: Continue to generate the full analysis.

        Calibration - be conservative:
        - Most company news moves a large-cap stock by less than 2%. HIGH and CRITICAL are rare.
        - Size the move relative to the company: a $50M contract is huge for a small-cap and noise for a mega-cap.
        - When unsure between two impact levels, pick the lower one, and lower your confidence.

        Respond in JSON format, and with JSON only - no other text before or after it.

        Structure for RELEVANT news:
        {{
            "is_relevant": true,
            "reasoning": "Step-by-step logic. 1. Identify event. 2. Assess magnitude. 3. Determine sentiment.",
            "target_company": "Name of the main company affected",
            "ticker": "TSLA",
            "sentiment": "POSITIVE",
            "impact": "CRITICAL",
            "expected_move_pct": 12,
            "confidence": 70,
            "is_new_information": true,
            "is_company_specific": true,
            "explanation": "Concise summary of why this matters.",
            "prediction": "GAP UP",
            "horizon": "DAYS"
        }}

        Definitions:
        - Ticker: the primary US-listed symbol only (e.g. "TSLA", not "NASDAQ: TSLA"); null if the company is not listed.
        - Sentiment: POSITIVE, NEGATIVE, NEUTRAL
        - Impact:
            - CRITICAL (15%+ move, game changer)
            - HIGH (5-15% move, significant event)
            - MEDIUM (2-5% move, standard news)
            - LOW (Noise)
        - expected_move_pct: the total % move this news should cause, measured from the price before the
          news broke, as a plain positive number (6.5 means 6.5%). Must be consistent with the impact.
        - confidence: 0-100, how sure you are of BOTH the direction and roughly the size of the move.
          50 is a coin flip. Use 80+ only for unambiguous, material, company-specific news.
        - is_new_information: false if this repeats news that is already public (a follow-up,
          a recap, commentary on an earlier announcement).
        - is_company_specific: false if the story is really about the market, the economy or a whole
          sector rather than this one company.
        - Prediction: GAP UP / GAP DOWN (if closed) or RALLY / DROP (if open)
        - Horizon: how long this specific news item should keep moving the price
          before the market has fully priced it in - i.e. how long until it's
          "played out" and a holder should reassess.
            - INTRADAY (reaction fades or resolves within the current/next session,
              e.g. a single earnings beat/miss, a same-day rumor)
            - DAYS (effect plays out over the next few trading days, e.g. an
              analyst upgrade, a contract win, a product announcement)
            - WEEKS (structural news that takes longer to be fully priced in,
              e.g. M&A, major regulatory action, a large multi-year contract)
        """


def _pct(value):
    return "unknown" if value is None else f"{value * 100:+.2f}%"


def _price(value):
    return "unknown" if value is None else f"{value:.2f}"


def build_trade_prompt(article, analysis, context, direction):
    """Prompt for the trade confirmation: given what the screen concluded and
    what the price has done since, does opening a position NOW still have an
    edge? Run only for would-be trades, so it can afford to ask for both
    sides of the argument."""
    side = "LONG (buy, profit if the price rises)" if direction == "LONG" \
        else "SHORT (sell short, profit if the price falls)"
    ctx = context or {}
    atr = ctx.get('atr_pct')
    return f"""
        You are a disciplined event-driven trader. A news screen flagged the article below. Decide whether
        opening a {side} position in {analysis.get('ticker')} RIGHT NOW, at the current price, still has an edge.

        Article title: {article.get('title', '')}
        Published: {_published_line(article)}{_source_note(article)}
        Article text: {_article_text(article, 1500)}

        Screen result:
        - Sentiment: {analysis.get('sentiment')}, impact: {analysis.get('impact')}
        - Expected total move: {analysis.get('expected_move_pct')}%
        - Why: {analysis.get('explanation')}

        Market data for {analysis.get('ticker')} (prices include pre/after-hours trading):
        - Current price: {_price(ctx.get('price'))}
        - Previous regular-session close: {_price(ctx.get('ref_close'))} (move since: {_pct(ctx.get('change_since_close_pct'))})
        - Price when the article was published: {_price(ctx.get('price_at_publish'))} (move since: {_pct(ctx.get('change_since_publish_pct'))})
        - Change over the 5 sessions before: {_pct(ctx.get('change_5d_pct'))}
        - Normal daily range (14-day ATR): {"unknown" if atr is None else f"{atr * 100:.2f}%"} of the price

        Think about:
        - How much of the expected move has already happened? News that has been priced in has no edge left.
        - Is the move still ahead clearly bigger than the stock's normal daily range? If not, it is noise.
        - Sharp news spikes often partly reverse within days. Is this the kind of news that keeps going?
        - Did the price move AGAINST the news? That can mean the market reads it differently.

        Respond in JSON only - no other text before or after it:
        {{
            "bull_case": "One or two sentences: the best argument FOR taking this trade now.",
            "bear_case": "One or two sentences: the best argument AGAINST taking it now.",
            "take_trade": true,
            "confidence": 65,
            "expected_remaining_move_pct": 4.5,
            "horizon": "DAYS",
            "reason": "One sentence verdict."
        }}

        - expected_remaining_move_pct: the further move you expect FROM THE CURRENT PRICE in the trade's
          direction, as a plain positive number (4.5 means 4.5%).
        - confidence: 0-100 that this trade reaches that move before reversing. 50 is a coin flip.
        - horizon: INTRADAY, DAYS or WEEKS - how long the remaining move should take.
        """


def parse_json_response(text_response):
    """Parse JSON from an LLM response, tolerating a ```json code fence or
    stray prose around the object.

    Ollama's JSON mode guarantees a bare object, but hosted chat models
    sometimes wrap it ("Here is the analysis: {...}") despite the system
    prompt. Rather than discard the whole analysis, fall back to the
    outermost {...} in the text.
    """
    if not text_response:
        return None
    text = text_response.strip()
    if text.startswith("```"):
        # Strip the opening fence line (``` or ```json) and a closing fence.
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            print(f"Failed to parse JSON response: {text[:100]}...")
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            print(f"Failed to parse JSON response: {text[:100]}...")
            return None
    # A bare list/string is not an analysis.
    return data if isinstance(data, dict) else None
