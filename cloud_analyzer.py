import config
from cloud_providers import PROVIDERS
from llm_prompts import build_market_prompt, parse_json_response

# Ollama gets a JSON-mode flag to force this; hosted chat APIs don't have an
# equivalent, so the constraint is stated in a system prompt instead.
JSON_ONLY_SYSTEM_PROMPT = (
    "Respond with a single JSON object only - no prose, no markdown code "
    "fences, no text before or after it."
)


class CloudAnalyzer:
    """Analyzes articles via a hosted AI API instead of a local model.

    Same prompt and JSON contract as the local (Ollama) analyzer - only the
    backend that executes it differs - so switching engines in Settings
    doesn't change what "relevant" or "CRITICAL" means. Which vendor
    actually runs the prompt is decided by CLOUD_AI_PROVIDER; see
    cloud_providers.py to add one beyond Anthropic/OpenAI.
    """

    def __init__(self, ai_log_callback=None):
        self.ai_log_callback = ai_log_callback
        self.provider = None

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

        try:
            self.provider = provider_cls(
                api_key=config.CLOUD_AI_API_KEY,
                model=config.CLOUD_AI_MODEL,
                base_url=config.CLOUD_AI_BASE_URL,
                log_callback=ai_log_callback,
            )
            print(f"Analyzer initialized in CLOUD MODE using {config.CLOUD_AI_PROVIDER}/{config.CLOUD_AI_MODEL}")
        except ImportError as e:
            print(f"Warning: {e}")

    def analyze_article(self, company, article, market_is_open, portfolio_tickers=None):
        if not self.provider:
            return None

        prompt = build_market_prompt(company, article, market_is_open, portfolio_tickers)
        if not prompt:
            return None

        response_text = self.provider.complete(prompt, system=JSON_ONLY_SYSTEM_PROMPT)
        if not response_text:
            return None

        data = parse_json_response(response_text)
        if not data or not data.get('is_relevant', False):
            return None

        return data
