import json
import os
from config import USE_LOCAL_LLM, OLLAMA_URL, LOCAL_MODEL_NAME, OLLAMA_NUM_THREADS
import requests

class MarketAnalyzer:
    def __init__(self, ai_log_callback=None):
        self.ai_log_callback = ai_log_callback
        self.use_local = USE_LOCAL_LLM
        if self.use_local:
            print(f"Analyzer initialized in LOCAL MODE using {LOCAL_MODEL_NAME}")
        else:
            print("Warning: Local LLM is disabled in config, but Gemini support has been removed.")


    def analyze_article(self, company, article, market_is_open, portfolio_tickers=None):
        """
        Analyzes a single news article for sentiment and market impact using a single-pass conditional prompt.
        """
        if not self.use_local:
            return None

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

        prompt = f"""
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
        
        Respond in JSON format.
        
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

        try:
            response_text = self._analyze_local(prompt)
            if not response_text: return None

            data = self._parse_json_response(response_text)
            if not data: return None

            if not data.get('is_relevant', False):
                # Model decided this is noise - drop it before it reaches
                # the notifier.
                return None

            return data

        except Exception as e:
            print(f"Error analyzing article for {company}: {e}")
            return None

    def _parse_json_response(self, text_response):
        """
        Helper to parse JSON from LLM response, handling code blocks.
        """
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

    def _analyze_local(self, prompt):
        """
        Sends the prompt to the local Ollama instance.
        """
        try:

            if self.ai_log_callback:
                self.ai_log_callback(f"--> SENT TO AI:\n{prompt[:500]}..." if len(prompt) > 500 else f"--> SENT TO AI:\n{prompt}")

            payload = {
                "model": LOCAL_MODEL_NAME,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {
                    "num_thread": OLLAMA_NUM_THREADS
                }
            }

            response = requests.post(OLLAMA_URL, json=payload, timeout=300)
            response.raise_for_status()

            result = response.json()
            response_text = result.get('response', '')


            if self.ai_log_callback:
                self.ai_log_callback(f"<-- AI RESPONSE:\n{response_text}")

            return response_text

        except Exception as e:
            err_msg = f"Local LLM Error: {e}"
            print(err_msg)
            if self.ai_log_callback:
                self.ai_log_callback(f"!! ERROR: {err_msg}")
            return None
