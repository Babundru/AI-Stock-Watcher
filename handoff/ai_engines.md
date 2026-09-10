# AI / Analysis Engines

Three interchangeable engines analyze each article. `main.py:
StockAppBackend.__init__` picks exactly one at startup, in this priority
order (see `config.py`):

```
USE_CLOUD_AI = True   -> CloudAnalyzer  (cloud_analyzer.py)
USE_LOCAL_LLM = True  -> MarketAnalyzer (analyzer.py, talks to Ollama)
(both False)           -> KeywordAnalyzer (keyword_analyzer.py, offline)
```

All three implement the same interface -
`analyze_article(company, article, market_is_open, portfolio_tickers) ->
dict | None` - and are judged against the identical output contract, so
swapping engines never changes what counts as "relevant" or "CRITICAL"
purely because of a different code path.

## The shared prompt contract (`llm_prompts.py`)

Both LLM-based engines (local and cloud) send the exact same prompt,
built by `build_market_prompt()`. It's a single-pass conditional prompt:
the model returns `{"is_relevant": false}` and stops immediately for
noise, or a full structured object for real news:

```json
{
  "is_relevant": true,
  "reasoning": "step-by-step logic",
  "target_company": "...", "ticker": "...",
  "sentiment": "POSITIVE|NEGATIVE|NEUTRAL",
  "impact": "LOW|MEDIUM|HIGH|CRITICAL",
  "expected_move_pct": 6.5,
  "confidence": 0-100,
  "is_new_information": true,
  "is_company_specific": true,
  "explanation": "...",
  "prediction": "GAP UP|GAP DOWN|RALLY|DROP|FLAT",
  "horizon": "INTRADAY|DAYS|WEEKS"
}
```

`confidence`, `is_new_information` and `is_company_specific` gate the
alert in `main.py:_alert_skip_reasons` (below `MIN_CONFIDENCE`, or either
flag explicitly `false`, drops it). A field the engine leaves out never
counts against an alert - the keyword engine has none of them.
`expected_move_pct` feeds `strategy.plan_trade` (the priced-in check and
the target). The prompt also gets the article's publish time and a
calibration paragraph ("most news moves a large-cap < 2%").

### The trade confirmation (`build_trade_prompt`)

A second call, made only for alerts that are about to become a trade
(after the rule checks in `strategy.plan_trade` pass, so a handful a day),
via `confirm_trade()` on both LLM engines. It sends the article again with
the live price context from `price_lookup.fetch_context` - move since the
previous close and since publication, 5-day change, 14-day ATR - and asks
for a bull case, a bear case, `take_trade`, `confidence` and
`expected_remaining_move_pct`. A pass, or confidence below
`MIN_CONFIDENCE`, cancels the trade; the remaining move sets the target.
If the call fails the trade is decided on the rules alone (logged).
`AI_TRADE_CONFIRM` turns it off. The keyword engine has no
`confirm_trade`, so it always trades on the rules.

`horizon` feeds `watch_manager.py`'s sell-signal expiry window - see
`portfolio_and_notifications.md`. `parse_json_response()` tolerates a
``` ```json ``` fence some models wrap responses in.

**KeywordAnalyzer does NOT use this prompt** - it's a from-scratch
salience-weighted regex scorer (see `data_sources.md` for its algorithm),
returning the same shape but computed from `data/keywords.json` weights
instead of model reasoning.

## Engine 1: Local (`analyzer.py` + `ollama_manager.py`)

Talks to a local Ollama server (`OLLAMA_URL`, default
`http://localhost:11434/api/generate`, model `LOCAL_MODEL_NAME`).
`ollama_manager.py` auto-starts `ollama serve` if not already running -
**this uses a Windows `tasklist` check, so it's desktop-only**. Never
worked, and was never meant to, on the Linux VM - that's exactly why
`USE_CLOUD_AI` exists as the server-appropriate path.

## Engine 2: Cloud (`cloud_analyzer.py` + `cloud_providers.py`)

`CloudAnalyzer.__init__` builds a `BaseCloudProvider` subclass from the
`PROVIDERS` registry in `cloud_providers.py`, keyed by `CLOUD_AI_PROVIDER`:

| Key | Class | Notes |
|---|---|---|
| `anthropic` | `AnthropicProvider` | Official `anthropic` SDK, direct |
| `openai` | `OpenAIProvider` | Official `openai` SDK; also covers any OpenAI-compatible host via `CLOUD_AI_BASE_URL` |
| `openrouter` | `OpenRouterProvider` | `OpenAIProvider` with a defaulted base URL |
| `routera` | `RouteraProvider` | See below - not a simple `OpenAIProvider` subclass |

If `CLOUD_AI_API_KEY` is empty, `CloudAnalyzer.provider` stays `None` and
`analyze_article` always returns `None` (logged once as a warning, not per
article).

### Routera specifics - **read this before changing the model**

Routera (`https://www.routera.one`) is a one-key-many-models router:
`CLOUD_AI_MODEL` is `"<vendor>/<model>"`, e.g. `"openai/gpt-5.6-luna"` or
`"anthropic/claude-opus-5"`.

**Routera exposes two different wire formats depending on the vendor
prefix**, confirmed against their docs and hit as a real bug during setup:

- Everything except `anthropic/*` -> OpenAI-compatible `chat.completions`
  at `https://api.routera.one/v1`
- `anthropic/*` models -> Anthropic's native Messages format at
  `https://api.routera.one` (root - the `anthropic` SDK appends
  `/v1/messages` itself)

Sending an `anthropic/*` model to the OpenAI-style endpoint fails with
`400 anthropic_requires_messages`. `RouteraProvider.complete()` dispatches
on the model prefix and builds either an internal `AnthropicProvider` or
`OpenAIProvider` lazily to handle this - if you ever see that 400, this is
why, and it means the dispatch logic broke, not that Routera changed
anything.

**Current model: `openai/gpt-5.6-luna`.** Chosen deliberately - it's the
cheapest model in Routera's catalog (0.04x credit multiplier, i.e. real
tokens consumed are ~25x the credits drawn), which matters because this
app calls the model on every scanned article continuously. Verified live
against the app's actual prompt: clean JSON every time, sound
proportionate reasoning (e.g. correctly picked HIGH not CRITICAL for a
Tesla guidance cut). 400K context, native JSON mode, supports reasoning.

Check remaining Routera balance (credits, not raw tokens):
```bash
curl "https://api.routera.one/v1/balance" -H "Authorization: Bearer $ROUTERA_KEY"
```

**A model that looked free but wasn't**: `google/gemma-4-31b-it` (the
original choice) sits behind Routera's "Free plan," which is disabled
service-wide due to abuse and returns `403 insufficient_plan` regardless
of purchased token balance. That's Routera account-side, not this app's
bug - don't spend time debugging the app if you see that error, just pick
a different (non-free-plan) model.

## Engine 3: Keyword (offline, `keyword_analyzer.py`)

No model, no API key, no network call - a salience-weighted regex scorer
over `data/keywords.json` (~230 phrases with hand-tuned weights, e.g.
`"bankruptcy": -10`, `"fda approval": 9`). Full algorithm detail (field
weights, negation handling, diminishing returns on repeats, relevance
gating, ticker/company-name extraction heuristics) is documented in the
module's own docstrings/comments - it's dense but self-contained, read the
file directly if you're tuning scoring rather than duplicating it here.
Key numbers worth knowing without opening the file: `SENTIMENT_THRESHOLD
= 8.0`, `IMPACT_HIGH = 18.0`, `IMPACT_CRITICAL = 32.0`, headline matches
weighted 3x vs. 1x for body text.

This is the fallback with zero cost and zero external dependency - useful
for testing the pipeline, or as a last resort if both AI paths are
unavailable/unaffordable.

## Live testing note

There's no automated test suite for prompt/engine changes. The pattern
used during setup: instantiate the provider directly, call `.complete()`
with a realistic prompt built via `build_market_prompt()` using a real or
representative article, and inspect the raw response before trusting
`parse_json_response()`'s output. Worth doing after any model or prompt
change - a syntactically valid but semantically wrong response won't throw
anywhere, it'll just silently produce a bad or missing alert.
