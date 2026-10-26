# Proposed changes

From a review on 2026-10-24 of the code and of the live data on the VM
(`paper_trades.json`, `shadow_trades.json`, `settings.json`, the journal).
Implemented on branch `change-md-strategy-review` (Oct 26) except where marked;
strategy v9 / prompt p2.

Status: **proposed** / **in progress** / **done** / **rejected** (with the reason).

> The sample is small: 49 closed paper trades (Sep 17 - Oct 20) and 65 closed
> skipped trades, pooled across strategy v3-v7. Below ~30 trades per group the
> numbers are noise. Treat every finding as a lead to verify, not a result.

---

## What the data says so far

| Group | Trades | Avg net / trade | Win rate |
|---|---|---|---|
| All | 49 | +2.16% | 45% |
| All except the 4 biggest winners | 45 | -1.58% | - |
| M&A / takeover headlines | 14 | +12.1% | - |
| Headline already reports the move ("jumps 30%") | 28 | -1.60% | 39% |
| Headline does not report a move | 21 | +7.17% | 52% |
| Longs | 29 | +5.0% | 45% |
| Shorts | 20 | -2.0% | 45% |
| HIGH impact | 42 | +4.1% | 48% |
| CRITICAL impact | 7 | -9.7% | 29% |
| Confidence 60-69 | 19 | +7.7% | 58% |
| Confidence 80+ | 8 | -10.4% | 13% |
| Exit: stop loss | 19 | -8.6% (-163% total) | 0% |
| Exit: trailing stop | 15 | +8.95% | 80% |
| Exit: horizon expired | 14 | +6.1% | 64% |
| Horizon: INTRADAY | 6 | -0.4% | 33% |

Skipped trades (`shadow_trades.py`), by the rule that refused them:

| Rule | Trades | Avg net / trade |
|---|---|---|
| `capped` | 7 | **+9.0%** |
| `reward_risk` | 11 | +6.0% |
| `exhausted` | 11 | +2.8% |
| `cash` | 7 | +0.7% |
| `stale` | 27 | +0.2% |
| `against` | 2 | -11.0% |

Takeaways:

- **All of the profit comes from 4 trades** (BMBL +58%, LFCR +50%, AT.L +42%,
  SECZ +28%), three of them takeovers.
- **Self-reported confidence is inversely related to results.** It currently
  gates trades (`MIN_CONFIDENCE`) without predicting anything.
- **CRITICAL impact and "already moved" headlines lose.** By the time an RSS
  headline says "surges 22%", the move is done (QURE short -55%, NUAI -10%,
  ACN -6%).
- **Trades give back most of their gains.** Average best point (MFE) +8.6%,
  average close +2.2%. 5 stop-outs happened within 6 hours, on news day.
- **The `capped` rule refuses a winning category** - cash takeover targets
  that then got a raised or rival bid.

---

## C1. Fix the dead feeds - done

> **Done** (branch `change-md-strategy-review`): Yahoo US replaced by Seeking Alpha market news, Yahoo UK moved to `rss/topstories` (the only Yahoo URL still answering), Investing.com dropped from the built-in feeds. A feed with no entries for `FEED_DEAD_AFTER_SCANS` (30) scans is reported once to the journal and the dashboard log, and its per-scan error lines are muted. The VM's `data/news_sources.json` still lists Yahoo and Investing.com as custom sources - remove them in the dashboard.

`news_collector.py: US_RSS_FEEDS / EU_RSS_FEEDS`

`https://finance.yahoo.com/news/rssindex` and
`https://uk.finance.yahoo.com/news/rssindex` have returned HTTP 404 since
2026-10-06 (1,716 failures in the last 7 days of the journal). Two of the eight
built-in feeds are gone. Replace them, and log a warning when a feed has
returned nothing for N scans in a row so this is noticed in hours, not weeks.

Investing.com article pages return 403, so its stories are always judged on
the headline alone; consider dropping it or replacing it.

## C2. Add primary news sources - done (except RNS)

> **Done (except RNS)** (branch `change-md-strategy-review`): PR Newswire, GlobeNewswire (`rss.` host - the main one 403s the VM) and Business Wire (`SCAN_PRESS_RELEASES`), and SEC 8-Ks via `edgar.py` once `SEC_USER_AGENT` is set (routine items dropped by number, the model reads the EX-99 press release). Law-firm class-action spam is a noise headline. LSE RNS: no free feed was found that answers - not done. More articles mean more model calls; see C10.

`news_collector.py`, `source_manager.py`

The current feeds (CNBC, FT, Yahoo, City AM) are secondary reporting and often
publish after the price has moved. The free, real-time primary sources are not
used:

- **SEC EDGAR** 8-K filings (current-events Atom feed)
- **Press-release wires**: PR Newswire, GlobeNewswire, Business Wire RSS
- **LSE RNS** regulatory announcements for UK listings

Speed is the edge for a news strategy. This is likely worth more than any
prompt change.

## C3. Record the model and the publish time on every trade - done

> **Done** (branch `change-md-strategy-review`): `model`, `prompt_version`, `news_source`, `published_at`, `analysed_at` (plus `event_type`, `reported_move_pct`, `screened_out`) on every watch, paper trade and skipped trade (`paper_trader.SIGNAL_FIELDS`); `paper_report.py` splits by model, prompt, event type, source and prints the median detection latency.

`paper_trader.py`, `shadow_trades.py`, `watch_manager.py`

- `CLOUD_AI_MODEL` on the VM is a 4-model fallback list
  (`gpt-6-luna, gpt-5.6-luna, grok-4.6, claude-haiku-4.5`), and no trade
  records which one judged it - results from different models are pooled.
- No trade records the article's publish time, so detection latency
  (published -> analysed -> opened) can't be measured. It is probably the
  single most important number for this strategy.

Also record a prompt version next to `STRATEGY_VERSION`.

## C4. Stop chasing moves the headline already reports - done (price context to the model moved to C12)

> **Done (price context to the model moved to C12)** (branch `change-md-strategy-review`): Rule `reported` (headline move >= `REPORTED_MOVE_MAX_PCT` 5%, not for M&A; checked against the ledger: such trades averaged -2.2% vs +7.6%), `EXHAUSTED_MAX_ATR_MULT` 6 -> 4, CRITICAL sizes x0.85 instead of x1.15, prompt p2 (event_type, reported_move_pct, CRITICAL warning; verified live). The screening prompt still can't see the price - the ticker isn't known before it runs - so the price context goes to the panel's skeptic (C12) instead.

`llm_prompts.py`, `strategy.py: plan_trade`, `config.py`

- Give the model the price context it currently never sees: move so far in
  ATR units, the gap, volume vs. average, pre-market move. Today it estimates
  `expected_move_pct` blind and only the deterministic rules see the price.
- Refuse a trade whose headline reports a large % move, unless it is a
  continuation setup such as M&A.
- Lower `EXHAUSTED_MAX_ATR_MULT` (currently 6.0).
- Treat CRITICAL with suspicion rather than as the strongest signal.

## C5. Lean into M&A - done

> **Done** (branch `change-md-strategy-review`): `MERGER_ARB` (on): a capped long is held over WEEKS with a 5% stop and 5% target (then trailing), exiting on `against` or deal-break news. Note the +9% for `capped` skips is mostly one trade (SGR-UN.TO +63%).

`strategy.py: plan_trade` (`capped` rule)

M&A is the one category with a clear edge (+12.1% avg). The `capped` skips
would have averaged +9.0%. Replace the blanket refusal with a small
merger-arbitrage mode: hold the target for a raised or rival bid, with a
wider horizon, and exit if the deal breaks.

## C6. Cut shorts back - pending your OK

> **Pending your OK** (branch `change-md-strategy-review`): Code default is already `CRITICAL`; the VM's `settings.json` overrides it to `HIGH`. Needs the VM setting removed (or `ALLOW_SHORTS` false) - not changed without approval.

`data/settings.json` on the VM

The VM overrides `SHORT_MIN_IMPACT` to `HIGH` (default `CRITICAL`), and shorts
are the losing side (-2.0% avg). Restore `CRITICAL`, or set
`ALLOW_SHORTS = False`, until shorts show positive results on their own.

## C7. Change entries and exits - done

> **Done** (branch `change-md-strategy-review`): Entries wait `ENTRY_DELAY_AFTER_OPEN_MIN` (20) into the session; out-of-hours news now waits for the first session after it (`ENTRY_WAIT_FOR_SESSION`) instead of never trading. First-day stop `FIRST_DAY_STOP_MULT` (1.5x, sized on it), settling after the first session. `TAKE_HALF_AT_TARGET` sells half at the target. `SKIP_INTRADAY` refuses INTRADAY stories (followed as skipped). Each has its own setting so it can be judged or reverted separately.

`strategy.py`, `main.py: _decide_trade`, `config.py`

- Wait 15-30 minutes after the open on news day before entering, so event-day
  swings settle (5 stop-outs within 6 hours).
- Use a wider stop on the first day, tightening afterwards.
- At the target, sell half and trail the rest, to keep more of the MFE.
- Drop INTRADAY-horizon trades (-0.4% avg).

## C8. Filter the universe - done

> **Done** (branch `change-md-strategy-review`): Only exchanges in `markets.py` (`ALLOWED_MARKETS` narrows them, `TRADE_OTHER_EXCHANGES` off), `MIN_MARKET_CAP_USD` $150M and `MIN_DAILY_TURNOVER_USD` $1M (rule `illiquid`, followed as skipped). Set `ALLOWED_MARKETS` to what the broker offers.

`main.py: _decide_trade`, `price_lookup.py`, `markets.py`

Trades went into AIM small-caps (W7L.L, CDGP.L, OMI.L, TUN.L) and into Korea,
India, Japan and Sydney listings - no modelled session, FX fees, and likely not
offered as CFDs by the broker. Add:

- a minimum average daily dollar volume and market cap;
- an allow-list of exchanges the broker supports.

## C9. Replace self-reported confidence - partly done - waiting for data

> **Partly done - waiting for data** (branch `change-md-strategy-review`): Every alert is tagged with `event_type` (prompt p2) and results split by it; confidence no longer sizes positions. Sizing by category expectancy needs ~30 trades per type first.

`main.py: _alert_skip_reasons`, `paper_trader.py`

Tag every alert with an event type (M&A, earnings, FDA, guidance, offering,
lawsuit, management change, ...) and score it by that category's actual
results instead of the model's confidence. Once there is enough data, size
positions by category expectancy rather than a flat `PAPER_RISK_PCT`.

## C10. Cheap screener before the full prompt - in progress - shadow mode

> **In progress - shadow mode** (branch `change-md-strategy-review`): The keyword engine runs as a screener in `SCREENER_MODE` `shadow`: verdicts counted in `stats.json` and recorded per trade, nothing skipped. On past headlines alone it would have dropped 38 of 52 trades, so `on` needs its shadow record first. A small-model screener is not built.

`main.py: _process_article`, `cloud_analyzer.py`

33,969 articles scanned produced 244 alerts (0.7%), each costing a full LLM
call. Put a cheap first pass (a small model asked only "is this a company
event?", or the keyword scorer) in front of the full analysis.

## C11. Small cleanups - partly done

> **Partly done** (branch `change-md-strategy-review`): Call transcripts (VGZ, TLX) are caught by `is_noise_headline` (tested). `AI_TRADE_CONFIRM: true` is still in the VM's `settings.json` - ignored by the code, removal pending your OK.

- `AI_TRADE_CONFIRM: true` is still in the VM's `settings.json` but is ignored
  - remove it.
- Older trades include call transcripts (VGZ, TLX.AX "M&A Call Transcript");
  check `is_noise_headline` catches them now.

---

## C12. Multi-agent analysis panel - in progress - stage 1 (shadow mode)

> **In progress - stage 1 (shadow mode)** (branch `change-md-strategy-review`): `panel.py`: catalyst, skeptic (with price context, can veto), event specialist, historian (ledger base rates), devil's advocate, each on its own model; rule 3-of-4 with no veto. Runs in the background on every alert that reaches the price check, writes `data/panel_votes.json`, decides nothing; `paper_report.py` scores each agent (step 2). `weekly_review.py` is the weekly reviewer (writes `data/reviews/`, never applies). Verified live (~70s an alert). Steps 2-3 need closed trades.

`llm_prompts.py`, `cloud_analyzer.py`, `main.py`, a new `panel.py`

Personas alone won't help: several personalities on the **same model** make
the **same mistakes** in different words. What helps is agents with different
**information**, a different **job**, or a different **model**.

### Stage 1 - screener

The cheap pass from C10. Drops ~95% of articles.

### Stage 2 - panel, for the ~1% that pass

Each agent on a different model where possible (Routera already offers GPT,
Grok and Claude on one key):

| Agent | Input | Job |
|---|---|---|
| Catalyst analyst | The article | What changed, and how big it is relative to the company's size |
| "Priced-in" skeptic | Article + price context (move so far, gap, ATR, volume, pre-market) | Argue the move is already done |
| Event specialist | Article + an event-type prompt | M&A: premium, chance of a higher bid, regulatory risk. Biotech: binary readout risk. Earnings: guidance vs. the headline beat |
| Historian | Similar past trades and skipped trades from the ledger | Base rates: "the last 6 'jumps X% on earnings' trades lost on average" |
| Devil's advocate | The other agents' views, one round only | Strongest case against the trade |

### Stage 3 - a decision rule, not an LLM judge

E.g. trade only if at least 3 of 4 agree and the skeptic doesn't veto; size by
how strongly they agree. Disagreement is itself a signal. A fixed rule can be
measured and tuned; an LLM judge can't.

### Rollout

1. Run the panel in **shadow mode** beside the current single prompt. Record
   every agent's vote on every alert; the panel decides nothing yet.
2. After 30-50 trades, measure which agents' votes predict results. Weight
   them by that (a simple logistic regression is enough) and drop the rest.
3. Switch the panel live only once it beats the current pipeline's
   expectancy.

Cost: about 4-5 calls on roughly 250 alerts a month - small next to the ~34k
screening calls.

### Weekly reviewer agent

Reads the closed trades and skipped trades once a week and proposes rule
changes for a human to approve - the analysis at the top of this file, done
automatically.

---

## Suggested order

1. C1, C3, C6, C11 - cheap, and C3 makes everything after it measurable.
2. C2, C4 - news speed and the "already priced in" problem.
3. C5, C7, C8 - strategy changes, each judged as its own strategy version.
4. C12 in shadow mode, then C9 and C10 once there is data to fit them.
