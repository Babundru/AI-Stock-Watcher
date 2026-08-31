import re
from keyword_manager import KeywordManager
from typing import Dict, List, Optional

# --- Scoring tunables -------------------------------------------------------
# Where a keyword appears matters more than how often. A phrase in the
# headline is the story; the same phrase in paragraph 30 is usually incidental.
FIELD_WEIGHTS = {'title': 3.0, 'description': 1.5, 'body': 1.0}

# Only the top of the article body is considered. Scraped pages carry nav,
# footers, "related stories" and comments, all of which used to be scored.
BODY_CHAR_LIMIT = 2500

# Only the strongest N signals count. Without this, score grows with article
# length: a long page matching 30 weak keywords would outrank a headline
# saying "FDA approval". This is what decouples impact from page length.
MAX_SIGNALS = 10

# Repeated occurrences add progressively less (1x, 1.4x, 1.8x, then flat).
REPEAT_BONUS = 0.4
MAX_REPEATS = 2

# Words before a match that are scanned for a negation ("no breakthrough").
NEGATION_WINDOW = 4
NEGATION_FACTOR = -0.5

# Classification thresholds, calibrated against the field weights above:
# one strong (>=7) keyword in a headline scores >=21 and reaches HIGH.
SENTIMENT_THRESHOLD = 8.0
IMPACT_HIGH = 18.0
IMPACT_CRITICAL = 32.0

# A keyword this strong is a standalone reason to consider an article.
STRONG_KEYWORD = 7

NEGATORS = {
    "no", "not", "never", "none", "nor", "without", "lacks", "lacking",
    "fails", "failed", "fail", "denies", "denied", "deny", "dismissed",
    "dismisses", "unlikely", "doubt", "doubts", "avoids", "avoided",
    "isn't", "aren't", "wasn't", "weren't", "doesn't", "didn't", "don't",
    "won't", "cannot", "can't", "hasn't", "haven't", "despite", "absent",
}

# An article must look like it is about a traded company, not general
# interest. Filters out personal-finance and lifestyle pieces that happen
# to contain scoring words.
MARKET_CONTEXT = re.compile(
    r'\b(shares?|stocks?|nasdaq|nyse|earnings|revenue|profits?|quarterly|'
    r'investors?|shareholders?|ticker|market cap|ipo|guidance|analysts?|'
    r'inc\.?|corp\.?|corporation|plc|ltd\.?|holdings)\b',
    re.IGNORECASE,
)

# All-caps tokens that look like tickers but are not.
TICKER_STOPLIST = {
    "CEO", "CFO", "COO", "CTO", "USA", "US", "UK", "EU", "GDP", "AI", "IPO",
    "ETF", "FDA", "SEC", "DOJ", "FTC", "IRS", "NYSE", "AP", "PM", "AM", "EST",
    "EDT", "PDT", "PST", "UTC", "GMT", "Q1", "Q2", "Q3", "Q4", "THE", "AND",
    "FOR", "NEW", "NOW", "OUT", "TOP", "CNBC", "BBC", "CNN", "WSJ", "FT",
    "OPEC", "NATO", "UN", "IMF", "ECB", "FED", "M&A", "R&D", "EPS", "YOY",
}


class Signal:
    """One scored keyword occurrence."""

    __slots__ = ("keyword", "weight", "field", "count", "negated", "contribution")

    def __init__(self, keyword, weight, field, count, negated, contribution):
        self.keyword = keyword
        self.weight = weight
        self.field = field
        self.count = count
        self.negated = negated
        self.contribution = contribution


class KeywordAnalyzer:
    """Weighted keyword sentiment analysis for news articles.

    Scoring is salience-based rather than cumulative: a keyword's contribution
    depends on which field it appears in, whether it is negated, and whether a
    longer phrase already covers it. Only the strongest signals count toward
    the final score, so a long article cannot out-score a decisive headline.
    """

    def __init__(self):
        self.keyword_mgr = KeywordManager()
        self.reload_keywords()

    def reload_keywords(self):
        """Reload keywords from manager and recompile match patterns."""
        self.positive_keywords = self.keyword_mgr.get_positive_keywords()
        self.negative_keywords = self.keyword_mgr.get_negative_keywords()
        self._weights = {}
        self._weights.update(self.positive_keywords)
        self._weights.update(self.negative_keywords)
        # One combined pattern instead of ~230 separate ones: scanning a field
        # used to mean 230 independent regex passes over the same text
        # (finditer per keyword). A single alternation does one pass instead,
        # and finditer's non-overlapping matches give us "longest phrase wins"
        # for free as long as longer keywords are listed first - no separate
        # overlap-resolution pass needed afterwards.
        by_length = sorted(self._weights, key=len, reverse=True)
        self._combined_pattern = re.compile(
            r'\b(?:' + '|'.join(re.escape(kw.lower()) for kw in by_length) + r')\b'
        )

    def analyze_article(self, company, article, market_is_open, portfolio_tickers=None):
        """
        Analyze a news article using the weighted keyword algorithm.

        Args:
            company: Company name or ticker hint for this article
            article: Article dictionary with title, description, content
            market_is_open: Boolean indicating if market is open
            portfolio_tickers: List of tickers in user's portfolio

        Returns:
            Analysis dictionary, or None if the article is not relevant
        """
        title = article.get('title') or ''
        description = article.get('description') or ''
        content = article.get('content') or ''

        fields = {
            'title': title,
            'description': description,
            # Only the lead of the body; the tail is boilerplate.
            'body': content[:BODY_CHAR_LIMIT],
        }

        if len(f"{title} {description} {content}".strip()) < 20:
            return None

        signals = []
        for field_name, text in fields.items():
            signals.extend(self._score_field(text, field_name))

        if not signals:
            return None

        # Keep only the strongest signals - this is what stops long articles
        # from accumulating a high score out of many weak matches.
        signals.sort(key=lambda s: abs(s.contribution), reverse=True)
        kept = signals[:MAX_SIGNALS]

        ticker = self._extract_ticker(f"{title}\n{description}\n{fields['body']}", company)

        if not self._is_relevant(kept, fields, ticker):
            return None

        total_score = sum(s.contribution for s in kept)

        sentiment = self._classify_sentiment(total_score)
        impact = self._classify_impact(total_score)
        prediction = self._generate_prediction(sentiment, market_is_open)
        horizon = self._generate_horizon(impact)

        found_positive = sorted(
            {s.keyword for s in kept if s.contribution > 0},
            key=lambda k: -abs(self._weights.get(k, 0)),
        )
        found_negative = sorted(
            {s.keyword for s in kept if s.contribution < 0},
            key=lambda k: -abs(self._weights.get(k, 0)),
        )

        company_name = self._extract_company_name(article, company, ticker)

        return {
            "is_relevant": True,
            "target_company": company_name,
            "ticker": ticker,
            "sentiment": sentiment,
            "impact": impact,
            "explanation": self._generate_explanation(kept, sentiment, impact, total_score),
            "prediction": prediction,
            "horizon": horizon,
            "keywords_found": {
                "positive": found_positive,
                "negative": found_negative,
            },
            "score": round(total_score, 2),
            "confidence": self._confidence(kept),
            "signals": [
                {
                    "keyword": s.keyword,
                    "field": s.field,
                    "weight": s.weight,
                    "count": s.count,
                    "negated": s.negated,
                    "contribution": round(s.contribution, 2),
                }
                for s in kept
            ],
        }

    # --- Scoring ------------------------------------------------------------

    def _score_field(self, text, field_name):
        """Score every non-overlapping keyword occurrence within one field."""
        if not text or not text.strip():
            return []

        lowered = text.lower()
        field_weight = FIELD_WEIGHTS[field_name]

        accepted = {}
        for m in self._combined_pattern.finditer(lowered):
            keyword = m.group(0)
            data = accepted.setdefault(keyword, {'count': 0, 'negated': 0})
            data['count'] += 1
            if self._is_negated(lowered, m.start()):
                data['negated'] += 1

        signals = []
        for keyword, data in accepted.items():
            count = data['count']
            weight = self._weights[keyword]
            # Diminishing returns on repetition.
            multiplier = 1.0 + REPEAT_BONUS * min(count - 1, MAX_REPEATS)
            contribution = weight * field_weight * multiplier

            # If most occurrences were negated, invert and damp the signal.
            fully_negated = data['negated'] > count / 2
            if fully_negated:
                contribution *= NEGATION_FACTOR

            signals.append(
                Signal(keyword, weight, field_name, count, fully_negated, contribution)
            )
        return signals

    def _is_negated(self, lowered_text, match_start):
        """Check whether a negation word appears just before a match."""
        preceding = lowered_text[max(0, match_start - 60):match_start]
        words = re.findall(r"[a-z']+", preceding)[-NEGATION_WINDOW:]
        return any(w in NEGATORS for w in words)

    def _is_relevant(self, signals, fields, ticker=None):
        """Gate out articles that are not really market news."""
        # Needs either one decisive keyword or several corroborating ones.
        has_strong = any(abs(s.weight) >= STRONG_KEYWORD for s in signals)
        distinct = len({s.keyword for s in signals})
        if not has_strong and distinct < 3:
            return False

        # An explicitly quoted ticker - "(INTC)", "NASDAQ:AAPL" - is itself
        # proof the article is about a traded company, even when the headline
        # uses no finance vocabulary. A strong keyword ("fda approval",
        # "bankruptcy", "acquisition"...) is the same kind of proof - these
        # were previously still dropped by the context gate below whenever
        # the article named no ticker and no corp suffix, which mainly hit
        # biotech/pharma headlines ("XYZ Pharma wins FDA approval") that say
        # nothing like "shares" or "Inc.".
        if ticker or has_strong:
            return True

        # Otherwise it has to read like it concerns a traded company.
        context = f"{fields['title']} {fields['description']} {fields['body'][:600]}"
        return bool(MARKET_CONTEXT.search(context))

    def _classify_sentiment(self, score: float) -> str:
        """Classify sentiment based on score."""
        if score > SENTIMENT_THRESHOLD:
            return "POSITIVE"
        elif score < -SENTIMENT_THRESHOLD:
            return "NEGATIVE"
        return "NEUTRAL"

    def _classify_impact(self, score: float) -> str:
        """Classify impact from score magnitude alone.

        Keyword *count* is deliberately not part of this any more - it was the
        main reason long articles were reported as CRITICAL.
        """
        magnitude = abs(score)
        if magnitude >= IMPACT_CRITICAL:
            return "CRITICAL"
        elif magnitude >= IMPACT_HIGH:
            return "HIGH"
        elif magnitude >= SENTIMENT_THRESHOLD:
            return "MEDIUM"
        return "LOW"

    def _confidence(self, signals) -> str:
        """How much corroboration is behind the verdict."""
        headline_signals = sum(1 for s in signals if s.field == 'title')
        strong = sum(1 for s in signals if abs(s.weight) >= STRONG_KEYWORD)
        if headline_signals and strong >= 2:
            return "HIGH"
        if headline_signals or strong >= 2:
            return "MEDIUM"
        return "LOW"

    def _generate_prediction(self, sentiment: str, market_is_open: bool) -> str:
        """Generate price prediction based on sentiment and market status."""
        if sentiment == "POSITIVE":
            return "RALLY" if market_is_open else "GAP UP"
        elif sentiment == "NEGATIVE":
            return "DROP" if market_is_open else "GAP DOWN"
        return "FLAT"

    def _generate_horizon(self, impact: str) -> str:
        """How long this news should keep moving the price, from impact alone
        (no LLM reasoning available here). A bigger move takes longer to be
        fully priced in than a routine one."""
        if impact in ("CRITICAL", "HIGH"):
            return "DAYS"
        return "WEEKS"

    def _generate_explanation(self, signals, sentiment, impact, score) -> str:
        """Explain the verdict in terms of the signals that actually drove it."""
        parts = [
            f"{sentiment.title()} sentiment, {impact.lower()} impact "
            f"(score: {score:.1f})."
        ]

        field_label = {'title': 'headline', 'description': 'summary', 'body': 'article'}
        drivers = []
        for s in signals[:4]:
            tag = f"'{s.keyword}' in {field_label[s.field]}"
            if s.negated:
                tag += " (negated)"
            if s.count > 1:
                tag += f" x{s.count}"
            drivers.append(f"{tag} [{s.contribution:+.0f}]")

        if drivers:
            parts.append("Driven by " + ", ".join(drivers) + ".")

        headline_driven = any(s.field == 'title' for s in signals[:4])
        if headline_driven:
            parts.append("Signal appears in the headline.")
        else:
            parts.append("Signal is body-only - treat with caution.")

        return " ".join(parts)

    # --- Entity extraction --------------------------------------------------

    def _extract_ticker(self, text: str, company: str) -> Optional[str]:
        """
        Extract a stock ticker from the article.

        Patterns are matched against the original-case text: upper-casing the
        whole document first made "(the)" and "(ceo)" look like tickers.
        """
        # Most explicit forms first.
        explicit = [
            r'\b(?:NASDAQ|NYSE|AMEX|OTC)\s*:\s*([A-Z]{1,5})\b',
            r'\$([A-Z]{1,5})\b',
            r'\(\s*(?:NASDAQ|NYSE|AMEX|OTC)\s*:\s*([A-Z]{1,5})\s*\)',
        ]
        for pattern in explicit:
            m = re.search(pattern, text)
            if m and m.group(1) not in TICKER_STOPLIST:
                return m.group(1)

        # Bare "(TSLA)" - only trust it if it is not a common acronym.
        for m in re.finditer(r'\(([A-Z]{2,5})\)', text):
            candidate = m.group(1)
            if candidate not in TICKER_STOPLIST:
                return candidate

        # Fall back to the hint only when it already looks like a ticker.
        if re.fullmatch(r'[A-Z]{1,5}', company or '') and company not in TICKER_STOPLIST:
            return company.upper()

        return None

    def _extract_company_name(self, article: dict, company_hint: str, ticker=None) -> str:
        """Extract a company name from the article, preferring the headline."""
        title = article.get('title') or ''
        description = article.get('description') or ''
        content = article.get('content') or ''
        # Title counted twice so headline mentions outrank body mentions.
        search_text = f"{title} {title} {description[:200]} {content[:400]}"

        patterns = [
            # "Apple Inc.", "Rivian Automotive Inc"
            (r'\b([A-Z][A-Za-z&.\-]*(?:\s+[A-Z][A-Za-z&.\-]*){0,3}\s+'
             r'(?:Inc|Corp|Corporation|Ltd|Limited|LLC|Company|Group|Holdings|Plc|AG|SE|NV))\b', 10),
            # "Amazon (AMZN)"
            (r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s+\((?:NASDAQ|NYSE)?:?\s*[A-Z]{2,5}\)', 9),
            # "Tesla shares", "Apple stock"
            (r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:shares|stock|stocks|equity|earnings)\b', 7),
            # "Tesla's" - feeds use both straight and curly apostrophes
            (r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)['’]s\b", 5),
        ]

        false_positives = {
            'The', 'This', 'That', 'What', 'When', 'Where', 'Who', 'How', 'Why',
            'United States', 'Wall Street', 'New York', 'Federal Reserve', 'White House',
            'Here', 'There', 'These', 'Those', 'Monday', 'Tuesday', 'Wednesday',
            'Thursday', 'Friday', 'Saturday', 'Sunday', 'Reuters', 'Bloomberg',
            'Analysts', 'Investors', 'Shares', 'Stocks', 'Markets', 'Treasury',
        }

        candidates = []
        for pattern, base_score in patterns:
            for match in re.findall(pattern, search_text):
                name = match.strip(' .')
                if len(name) < 2 or name in false_positives:
                    continue
                score = base_score
                if name in title:
                    score += 5
                if len(name) > 10:
                    score += 2
                candidates.append((name, score))

        if candidates:
            candidates.sort(key=lambda x: x[1], reverse=True)
            return candidates[0][0]

        if ticker:
            return ticker

        # Falling back to a generic hint like "General Market News" makes for a
        # useless alert title - the headline is far more informative.
        if company_hint and not company_hint.endswith("News"):
            return company_hint
        return (title[:60] + "...") if len(title) > 60 else (title or company_hint)
