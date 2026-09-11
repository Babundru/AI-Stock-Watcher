import time

import config
from cloud_providers import PROVIDERS
from llm_prompts import AnalysisUnavailable, build_market_prompt, parse_json_response

# Ollama gets a JSON-mode flag to force this; hosted chat APIs don't have an
# equivalent, so the constraint is stated in a system prompt instead.
JSON_ONLY_SYSTEM_PROMPT = (
    "Respond with a single JSON object only - no prose, no markdown code "
    "fences, no text before or after it."
)

# Seconds a model whose API call failed spends at the back of the queue.
# Long enough that an outage costs one wasted call every few minutes rather
# than one per article; short enough that a passing error doesn't leave a
# pricier fallback model running for long.
MODEL_COOLDOWN = 300


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
        # model -> time.monotonic() until which it waits at the back of the queue.
        self._benched = {}

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

        A model whose call fails outright (the provider has logged why) is
        benched for MODEL_COOLDOWN: moved to the back of the queue rather
        than out of it, so with every model failing each is still tried,
        and once the cooldown ends the preferred model is first again. A
        reply that merely wasn't JSON moves only this one request along.
        """
        now = time.monotonic()
        ready = [p for p in self.providers if self._benched.get(p[0], 0) <= now]
        order = ready + [p for p in self.providers if p not in ready]
        for i, (model, provider) in enumerate(order):
            text = provider.complete(prompt, system=JSON_ONLY_SYSTEM_PROMPT)
            data = parse_json_response(text)
            if data:
                self._benched.pop(model, None)
                return data
            failed = text is None
            if failed:
                self._benched[model] = time.monotonic() + MODEL_COOLDOWN
            if i + 1 < len(order):
                nxt = order[i + 1][0]
                if failed:
                    self._log(f"   ↪ {model} failed - trying {nxt}; {model} goes to the back "
                              f"of the queue for {MODEL_COOLDOWN // 60} min")
                else:
                    self._log(f"   ↪ {model} gave no usable reply - trying {nxt}")
        return None

    def analyze_article(self, company, article, market_is_open, portfolio_tickers=None):
        """The analysis dict, or None if the article isn't relevant (or has
        no text to judge). Raises AnalysisUnavailable when no model gave an
        answer at all, so the article is retried rather than written off."""
        if not self.providers:
            raise AnalysisUnavailable("cloud AI is not set up - check the API key and provider")

        prompt = build_market_prompt(company, article, market_is_open, portfolio_tickers)
        if not prompt:
            return None

        data = self._ask(prompt)
        if not data:
            tried = f" (tried {len(self.providers)} models)" if len(self.providers) > 1 else ""
            raise AnalysisUnavailable(f"no usable reply from cloud AI{tried}")
        if not data.get('is_relevant', False):
            return None

        return data
