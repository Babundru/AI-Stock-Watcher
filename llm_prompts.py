import datetime
import json

import markets
from reddit_source import SOURCE_PREFIX, is_reddit_article

# Shared between analyzer.py (local Ollama) and cloud_analyzer.py (Anthropic
# API) so the two engines are judged on the exact same prompt - only the
# backend that executes it differs.
#
# One prompt, build_market_prompt, run on every article. Whether an alert
# then becomes a trade is decided by the price rules in strategy.plan_trade,
# not by a second model call: a second read of the same text adds a veto
# that can't be tuned, and can't verify a rumour either.


# Stands in for the article text when only the headline could be got.
HEADLINE_ONLY = ("(Only the headline is available - the article itself could not be retrieved. "
                 "Judge the headline alone: rate it relevant only if it states a concrete company "
                 "event, and let the missing detail lower your confidence.)")


class AnalysisUnavailable(Exception):
    """The engine gave no verdict at all - API error, model unreachable, a
    reply that wasn't JSON. Distinct from analyze_article() returning None,
    which means the article was judged and found irrelevant: the scan loop
    retries these instead of writing the article off."""


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


# The half of the prompt that never varies, kept first and in one piece so
# it is a stable prefix across every article.
#
# That ordering is the whole point. Ollama keeps the KV cache of the longest
# prompt prefix it has already evaluated, and hosted APIs cache on an exact
# prefix match too, so a prompt whose constant half comes first is evaluated
# once and reused for every article after it. The article-specific lines
# used to sit above this block, which made almost nothing a shared prefix
# and re-evaluated the instructions from scratch every time - the single
# largest avoidable cost in a scan on a local model.
#
# Nothing article-specific may be added above or inside this string, and the
# relevance test therefore names the target company indirectly ("the company
# it concerns") rather than interpolating it.
ANALYSIS_INSTRUCTIONS = """You are an expert financial analyst screening news for tradable, company-specific catalysts. Analyze the news article given below for potential stock market impact.

--- INSTRUCTIONS ---
1. Determining RELEVANCE: Is this news likely to significantly move the stock price of the company it concerns (named under Target Context below) or a related major company?
   - Irrelevant: Routine reports, old news rehashes, minor opinion pieces, general fluff,
     "stocks to buy/watch" lists, market wraps and index recaps, price-target roundups,
     articles spread across many companies, promotional or sponsored content.
   - Relevant: Earnings, Mergers, Regulatory decisions (FDA, EMA, national regulators, EU
     antitrust), Lawsuits, Contracts, Analyst Upgrades/Downgrades, Macro events.
   - Companies listed in Europe count exactly as much as American ones: a profit warning from a
     German carmaker or an EU antitrust fine is as tradable as its US equivalent.

2. IF IRRELEVANT: Return ONLY { "is_relevant": false } and STOP.
3. IF RELEVANT: Continue to generate the full analysis.

Calibration - be conservative:
- Most company news moves a large-cap stock by less than 2%. HIGH and CRITICAL are rare.
- Size the move relative to the company: a $50M contract is huge for a small-cap and noise for a mega-cap.
- When unsure between two impact levels, pick the lower one, and lower your confidence.

Respond in JSON format, and with JSON only - no other text before or after it.

Structure for RELEVANT news:
{
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
    "value_is_capped": false,
    "explanation": "Concise summary of why this matters.",
    "prediction": "GAP UP",
    "horizon": "DAYS"
}

Definitions:
- Ticker: the symbol of the company's PRIMARY listing, written the way Yahoo Finance writes it -
  the bare symbol for a US listing ("TSLA", not "NASDAQ: TSLA"), and the symbol plus the exchange
  suffix for a listing anywhere else: BMW.DE (Frankfurt/XETRA), SAP.DE, ASML.AS (Amsterdam),
  AIR.PA (Paris), SHEL.L (London), NESN.SW (Zurich), ISP.MI (Milan), SAN.MC (Madrid),
  NOVO-B.CO (Copenhagen), ERIC-B.ST (Stockholm), EQNR.OL (Oslo), NOKIA.HE (Helsinki),
  UCB.BR (Brussels), EDP.LS (Lisbon), CEZ.PR (Prague), CDR.WA (Warsaw).
  For a European company, give the European listing, not its American depositary receipt: the
  home listing is where the news is traded and where the volume is. Use the US symbol only for a
  company whose primary listing really is in the US. null if the company is not listed at all.
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
- value_is_capped: true only when the news fixes the stock at a set price, so it jumps once and
  then sits there - the target of an agreed cash acquisition or tender offer, say. false for
  everything else, including the acquirer.
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


def build_market_prompt(company, article, open_markets, portfolio_tickers=None):
    """Build the single-pass conditional relevance/sentiment prompt for one
    article. Returns None only if there is not even a headline to analyze.

    ANALYSIS_INSTRUCTIONS first, then this article - see that constant for
    why the order matters.

    `open_markets` is the list of exchanges currently trading
    (markets.open_markets()); a plain bool is still accepted and read as the
    US market alone. Naming them matters now that the app follows more than
    one: whether this news gaps a stock at the next open or moves it right
    now depends on whether *its* exchange is trading, and at 08:00 ET a
    German stock is mid-session while a US one has hours to wait.
    """
    title = article.get('title', 'No Title')
    article_text = _article_text(article, 5000)

    if not article_text.strip():
        if not (article.get('title') or '').strip():
            print(f"Skipping article with neither a headline nor any text: {article.get('url', '')[:80]}")
            return None
        # Investing.com's and Seeking Alpha's feeds carry no summary and
        # their pages often refuse the scraper (HTTP 403), so for their
        # stories the headline is all there is. These used to be skipped -
        # and marked processed, so never looked at again - which threw away
        # wire items like "X appoints Y as CEO" unread.
        article_text = HEADLINE_ONLY

    if isinstance(open_markets, (list, tuple, set)):
        trading = sorted(open_markets)
        shut = [r for r in markets.REGIONS if r not in trading]
        market_context = (f"OPEN: {', '.join(trading)}."
                          + (f" CLOSED: {', '.join(shut)}." if shut else "")
                          if trading else "Every market is currently CLOSED.")
    else:
        market_context = "The market is currently OPEN." if open_markets else "The market is currently CLOSED."
    portfolio_context = ""
    if portfolio_tickers:
        portfolio_str = ", ".join(portfolio_tickers)
        portfolio_context = f"""User Portfolio (High Priority): {portfolio_str}. IF the news affects these stocks, treat it as HIGHER RELEVANCE.
"""

    return f"""{ANALYSIS_INSTRUCTIONS}
--- ARTICLE ---
Target Context: {company}
{portfolio_context}Market Status: {market_context}
Published: {_published_line(article)}{_source_note(article)}

Article Title: {title}
Article Text: {article_text}

Now respond with the JSON object only.
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
