import config
from llm_prompts import AnalysisUnavailable, build_market_prompt, parse_json_response
import requests

class MarketAnalyzer:
    """Analyses articles with a local Ollama model.

    Model name, thread count and endpoint are read from `config` at request
    time rather than captured at import, so the dashboard's Settings tab can
    change them on a running server without a restart.
    """

    def __init__(self, ai_log_callback=None):
        self.ai_log_callback = ai_log_callback
        self.use_local = config.USE_LOCAL_LLM
        if self.use_local:
            print(f"Analyzer initialized in LOCAL MODE using {config.LOCAL_MODEL_NAME}")
        else:
            print("Warning: Local LLM is disabled in config, but Gemini support has been removed.")


    def analyze_article(self, company, article, market_is_open, portfolio_tickers=None):
        """
        Analyzes a single news article for sentiment and market impact using a single-pass conditional prompt.
        Returns None for an irrelevant article; raises AnalysisUnavailable
        when the model gave no usable answer.
        """
        if not self.use_local:
            raise AnalysisUnavailable("local LLM is turned off in settings")

        prompt = build_market_prompt(company, article, market_is_open, portfolio_tickers)
        if not prompt:
            return None

        try:
            data = parse_json_response(self._analyze_local(prompt))
        except Exception as e:
            print(f"Error analyzing article for {company}: {e}")
            data = None
        if not data:
            # Ollama unreachable or erroring (_analyze_local has logged it),
            # or a reply that wasn't JSON - either way, no verdict yet.
            raise AnalysisUnavailable("no usable reply from the local LLM")

        if not data.get('is_relevant', False):
            # Model decided this is noise - drop it before it reaches
            # the notifier.
            return None

        return data

    def _analyze_local(self, prompt):
        """
        Sends the prompt to the local Ollama instance.
        """
        try:

            if self.ai_log_callback:
                self.ai_log_callback(f"--> SENT TO AI:\n{prompt[:500]}..." if len(prompt) > 500 else f"--> SENT TO AI:\n{prompt}")

            payload = {
                "model": config.LOCAL_MODEL_NAME,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {
                    "num_thread": config.OLLAMA_NUM_THREADS
                }
            }

            response = requests.post(config.OLLAMA_URL, json=payload, timeout=300)
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
