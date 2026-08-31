"""
Cloud AI providers for CloudAnalyzer (see cloud_analyzer.py).

CloudAnalyzer only ever talks to BaseCloudProvider.complete() - it has no
idea which vendor it's calling. That's what makes this pluggable: to wire up
a new provider (Google Gemini, Mistral, Cohere, a local server, anything
with a Python SDK or an HTTP endpoint) -

  1. Subclass BaseCloudProvider below and implement complete().
  2. Add it to the PROVIDERS registry at the bottom of this file.
  3. Set CLOUD_AI_PROVIDER to that key (in Settings, or data/settings.json).

No other file needs to change. Every provider is constructed with the same
three settings (api_key, model, base_url) even if it ignores base_url, and
must never raise out of complete() - on failure it logs and returns None so
one bad call doesn't take down the scan loop.
"""


class BaseCloudProvider:
    """Common constructor + logging for every provider.

    __init__ is where an SDK client should be built - do it there rather
    than lazily in complete(), so a missing package or a malformed base_url
    surfaces immediately (CloudAnalyzer treats ImportError from here as
    "provider unavailable" and logs it once, instead of per-article).
    """

    def __init__(self, api_key, model, base_url=None, log_callback=None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url or None
        self.log_callback = log_callback

    def _log(self, msg):
        print(msg)
        if self.log_callback:
            self.log_callback(msg)

    def complete(self, prompt, system=None):
        """Send `prompt` (with an optional `system` instruction) and return
        the model's text response, or None on failure. Must not raise."""
        raise NotImplementedError


class AnthropicProvider(BaseCloudProvider):
    """Anthropic Claude, via the official `anthropic` SDK."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            import anthropic
        except ImportError:
            raise ImportError("The 'anthropic' package is not installed. Run: pip install anthropic")
        self._sdk = anthropic
        self._client = anthropic.Anthropic(api_key=self.api_key, base_url=self.base_url)

    def complete(self, prompt, system=None):
        anthropic = self._sdk
        preview = prompt[:500] + "..." if len(prompt) > 500 else prompt
        self._log(f"--> SENT TO {self.model}:\n{preview}")
        try:
            kwargs = {
                "model": self.model,
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                kwargs["system"] = system
            response = self._client.messages.create(**kwargs)
            text = next((b.text for b in response.content if b.type == "text"), "")
            self._log(f"<-- RESPONSE:\n{text}")
            return text
        except anthropic.AuthenticationError:
            self._log("Cloud AI Error: invalid API key.")
        except anthropic.RateLimitError as e:
            self._log(f"Cloud AI Error: rate limited ({e}).")
        except anthropic.APIStatusError as e:
            self._log(f"Cloud AI Error ({e.status_code}): {e.message}")
        except anthropic.APIConnectionError as e:
            self._log(f"Cloud AI Error: connection failed ({e}).")
        return None


class OpenAIProvider(BaseCloudProvider):
    """OpenAI, and any OpenAI-compatible third-party API - Groq, Together,
    DeepSeek, Fireworks, OpenRouter, a local vLLM/llama.cpp server, etc. -
    by pointing `base_url` at that provider's endpoint. The wire format
    (chat.completions) is the same either way; only the key and host differ.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            import openai
        except ImportError:
            raise ImportError("The 'openai' package is not installed. Run: pip install openai")
        self._sdk = openai
        self._client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)

    def complete(self, prompt, system=None):
        openai = self._sdk
        preview = prompt[:500] + "..." if len(prompt) > 500 else prompt
        self._log(f"--> SENT TO {self.model}:\n{preview}")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            response = self._client.chat.completions.create(
                model=self.model, max_tokens=1024, messages=messages,
            )
            text = response.choices[0].message.content or ""
            self._log(f"<-- RESPONSE:\n{text}")
            return text
        except openai.AuthenticationError:
            self._log("Cloud AI Error: invalid API key.")
        except openai.RateLimitError as e:
            self._log(f"Cloud AI Error: rate limited ({e}).")
        except openai.APIStatusError as e:
            self._log(f"Cloud AI Error ({e.status_code}): {e.message}")
        except openai.APIConnectionError as e:
            self._log(f"Cloud AI Error: connection failed ({e}).")
        return None


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter - one API key routing to many hosted models (Anthropic,
    OpenAI, Google, Meta, ...) behind an OpenAI-compatible endpoint. This is
    exactly OpenAIProvider with the endpoint defaulted, so a base_url
    doesn't need to be typed in Settings; an explicit CLOUD_AI_BASE_URL
    still overrides it. Model names are provider-namespaced, e.g.
    "anthropic/claude-opus-5" or "openai/gpt-4o".
    """

    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(self, *args, **kwargs):
        kwargs["base_url"] = kwargs.get("base_url") or self.DEFAULT_BASE_URL
        super().__init__(*args, **kwargs)


class RouteraProvider(BaseCloudProvider):
    """Routera (https://www.routera.one) - a one-key-many-models router,
    key prefix "rta_...". Model names are provider-namespaced, e.g.
    "anthropic/claude-opus-5" or "openai/gpt-5.5" - but unlike OpenRouter,
    Routera exposes two different wire formats depending on that prefix:
    OpenAI-style chat.completions at "https://api.routera.one/v1" for
    everything else, and Anthropic's native Messages format at
    "https://api.routera.one" (root - the Anthropic SDK appends
    /v1/messages itself) for "anthropic/..." models. Sending an
    Anthropic-prefixed model to the OpenAI-style endpoint fails with
    "400 anthropic_requires_messages", so this dispatches to whichever
    underlying provider matches the model prefix instead of always using
    one SDK.
    """

    OPENAI_BASE_URL = "https://api.routera.one/v1"
    ANTHROPIC_BASE_URL = "https://api.routera.one"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._delegate = None

    def _build_delegate(self):
        if self.model.startswith("anthropic/"):
            return AnthropicProvider(
                api_key=self.api_key, model=self.model,
                base_url=self.ANTHROPIC_BASE_URL, log_callback=self.log_callback,
            )
        return OpenAIProvider(
            api_key=self.api_key, model=self.model,
            base_url=self.base_url or self.OPENAI_BASE_URL, log_callback=self.log_callback,
        )

    def complete(self, prompt, system=None):
        # Built lazily (not in __init__) so a missing SDK for whichever
        # branch this model doesn't need is never imported at all.
        if self._delegate is None:
            self._delegate = self._build_delegate()
        return self._delegate.complete(prompt, system=system)


# Registry consulted by CloudAnalyzer. Add new providers here.
PROVIDERS = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "openrouter": OpenRouterProvider,
    "routera": RouteraProvider,
}
