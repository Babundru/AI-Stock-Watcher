import os
from config import USE_LOCAL_LLM, OLLAMA_URL, LOCAL_MODEL_NAME, OLLAMA_NUM_THREADS
from llm_prompts import build_market_prompt, parse_json_response
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

        prompt = build_market_prompt(company, article, market_is_open, portfolio_tickers)
        if not prompt:
            return None

        try:
            response_text = self._analyze_local(prompt)
            if not response_text: return None

            data = parse_json_response(response_text)
            if not data: return None

            if not data.get('is_relevant', False):
                # Model decided this is noise - drop it before it reaches
                # the notifier.
                return None

            return data

        except Exception as e:
            print(f"Error analyzing article for {company}: {e}")
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
