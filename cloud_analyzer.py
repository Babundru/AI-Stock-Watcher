import config
from cloud_providers import PROVIDERS
from llm_prompts import AnalysisUnavailable, build_market_prompt, parse_json_response

# Ollama gets a JSON-mode flag to force this; hosted chat APIs don't have an
# equivalent, so the constraint is stated in a system prompt instead.
JSON_ONLY_SYSTEM_PROMPT = (
    "Respond with a single JSON object only - no prose, no markdown code "
    "fences, no text before or after it."
)

# Calls a model gets before the request moves on to the next model. Hosted
# APIs fail transiently - a 500, a dropped connection, a momentary rate
# limit - and a second attempt usually goes through, which is cheaper than
# handing every article to a pricier fallback. Only a failed call is worth
# repeating: a call that returned something is never sent twice.
MODEL_ATTEMPTS = 2


class CloudAnalyzer:
    """Analyzes articles via a hosted AI API instead of a local model.

    Same prompt and JSON contract as the local (Ollama) analyzer - only the
    backend that executes it differs - so switching engines in Settings
    doesn't change what "relevant" or "CRITICAL" means. Which vendor
    actually runs the prompt is decided by CLOUD_AI_PROVIDER; see
    cloud_providers.py to add one beyond Anthropic/OpenAI.

    CLOUD_AI_MODEL may list several models, most preferred first: each
    request goes to the first one that gives a usable reply (see _ask).
    """

    def __init__(self, ai_log_callback=None):
        self.ai_log_callback = ai_log_callback
        # (model, provider) pairs, most preferred first.
        self.providers = []

        # Read at construction (not import) so a settings change followed
        # by StockAppBackend.apply_settings() builds a fresh, current client.
        if not config.CLOUD_AI_API_KEY:
            print("Warning: Cloud AI is selected but no API key is configured. "
                  "Set one in Settings.")
            return

        provider_cls = PROVIDERS.get(config.CLOUD_AI_PROVIDER)
        if not provider_cls:
            print(f"Warning: unknown Cloud AI provider '{config.CLOUD_AI_PROVIDER}'. "
                  f"Available: {', '.join(PROVIDERS)}")
            return

        models = config.cloud_models()
        try:
            self.providers = [
                (model, provider_cls(
                    api_key=config.CLOUD_AI_API_KEY,
                    model=model,
                    base_url=config.CLOUD_AI_BASE_URL,
                    log_callback=ai_log_callback,
                ))
                for model in models
            ]
            print(f"Analyzer initialized in CLOUD MODE using {config.CLOUD_AI_PROVIDER}: "
                  f"{' -> '.join(models)}")
        except ImportError as e:
            print(f"Warning: {e}")

    def _log(self, msg):
        (self.ai_log_callback or print)(msg)

    def _ask(self, prompt):
        """The parsed JSON reply of the first model, in priority order, that
        gives one - or None if none did.

        No model is ever taken out of the queue or held back for later
        requests: every request starts again at the preferred model, so a
        single passing error never pushes a whole scan onto a pricier
        fallback. A model whose call fails outright (the provider has
        logged why) is simply retried, up to MODEL_ATTEMPTS calls, before
        the request moves on. A call that came back - even with a reply
        that wasn't JSON - is already paid for, so it is not repeated;
        that model just yields this one request to the next.
        """
        for i, (model, provider) in enumerate(self.providers):
            for attempt in range(1, MODEL_ATTEMPTS + 1):
                text = provider.complete(prompt, system=JSON_ONLY_SYSTEM_PROMPT)
                if text is not None:
                    break
                if attempt < MODEL_ATTEMPTS:
                    self._log(f"   ↪ {model} failed - retrying it "
                              f"(attempt {attempt + 1} of {MODEL_ATTEMPTS})")
            data = parse_json_response(text)
            if data:
                return data
            if i + 1 < len(self.providers):
                nxt = self.providers[i + 1][0]
                reason = ("failed every attempt" if text is None
                          else "gave no usable reply")
                self._log(f"   ↪ {model} {reason} - trying {nxt}")
        return None

    def analyze_article(self, company, article, open_markets, portfolio_tickers=None):
        """The analysis dict, or None if the article isn't relevant (or has
        no text to judge). Raises AnalysisUnavailable when no model gave an
        answer at all, so the article is retried rather than written off."""
        if not self.providers:
            raise AnalysisUnavailable("cloud AI is not set up - check the API key and provider")

        prompt = build_market_prompt(company, article, open_markets, portfolio_tickers)
        if not prompt:
            return None

        data = self._ask(prompt)
        if not data:
            tried = f" (tried {len(self.providers)} models)" if len(self.providers) > 1 else ""
            raise AnalysisUnavailable(f"no usable reply from cloud AI{tried}")
        if not data.get('is_relevant', False):
            return None

        return data
