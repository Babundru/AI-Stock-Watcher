import json

# Shared between analyzer.py (local Ollama) and cloud_analyzer.py (Anthropic
# API) so the two engines are judged on the exact same prompt - only the
# backend that executes it differs.


def build_market_prompt(company, article, market_is_open, portfolio_tickers=None):
    """Build the single-pass conditional relevance/sentiment prompt for one
    article. Returns None if the article has no usable text to analyze."""
    title = article.get('title', 'No Title')
    description = article.get('description', 'No Description')
    content = article.get('content')

    description = description if description else ""
    content = content if content else ""

    # Prefer the scraped body when we actually got something substantial,
    # otherwise fall back to the feed description.
    article_text = content if len(content) > 100 else description

    if not article_text or len(article_text.strip()) == 0:
        print(f"Skipping article '{title}' due to empty content/description.")
        return None

    if len(article_text) > 5000:
        article_text = article_text[:5000]
        last_period = article_text.rfind('.')
        if last_period > 0:
            article_text = article_text[:last_period + 1]
        article_text += " ... (truncated)"

    market_context = "The market is currently OPEN." if market_is_open else "The market is currently CLOSED."
    portfolio_context = ""
    if portfolio_tickers:
        portfolio_str = ", ".join(portfolio_tickers)
        portfolio_context = f"\nUser Portfolio (High Priority): {portfolio_str}. IF the news affects these stocks, treat it as HIGHER RELEVANCE."

    return f"""
        You are an expert financial analyst. Analyze the following news article for potential stock market impact.

        Target Context: {company}
        {portfolio_context}
        Market Status: {market_context}

        Article Title: {title}
        Article Text: {article_text}

        --- INSTRUCTIONS ---
        1. Determining RELEVANCE: Is this news likely to significantly move the stock price of {company} or a related major company?
           - Irrelevant: Routine reports, old news rehashes, minor opinion pieces, general fluff.
           - Relevant: Earnings, Mergers, FDA approvals, Lawsuits, Contracts, Analyst Upgrades/Downgrades, Macro events.

        2. IF IRRELEVANT: Return ONLY {{ "is_relevant": false }} and STOP.
        3. IF RELEVANT: Continue to generate the full analysis.

        Respond in JSON format, and with JSON only - no other text before or after it.

        Structure for RELEVANT news:
        {{
            "is_relevant": true,
            "reasoning": "Step-by-step logic. 1. Identify event. 2. Assess magnitude. 3. Determine sentiment.",
            "target_company": "Name of the main company affected",
            "ticker": "TSLA",
            "sentiment": "POSITIVE",
            "impact": "CRITICAL",
            "explanation": "Concise summary of why this matters.",
            "prediction": "GAP UP"
        }}

        Definitions:
        - Sentiment: POSITIVE, NEGATIVE, NEUTRAL
        - Impact:
            - CRITICAL (15%+ move, game changer)
            - HIGH (5-15% move, significant event)
            - MEDIUM (2-5% move, standard news)
            - LOW (Noise)
        - Prediction: GAP UP / GAP DOWN (if closed) or RALLY / DROP (if open)
        """


def parse_json_response(text_response):
    """Parse JSON from an LLM response, tolerating a ```json code fence."""
    try:
        text_response = text_response.strip()
        if text_response.startswith("```json"):
            text_response = text_response[7:-3].strip()
        elif text_response.startswith("```"):
            text_response = text_response[3:-3].strip()
        return json.loads(text_response)
    except json.JSONDecodeError:
        print(f"Failed to parse JSON response: {text_response[:100]}...")
        return None
