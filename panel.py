"""A panel of analysis agents, run beside the single-prompt pipeline in
shadow mode (CHANGE.md C12). It decides nothing yet: every agent's vote on
every alert is recorded (data/panel_votes.json), so once trades have closed
it can be measured which agents' votes predict results (paper_report.py),
and the panel switched live only if it beats the pipeline's expectancy.

Personas alone would not help - several personalities on the same model
make the same mistakes in different words. These agents differ by the
information they get, the job they are given, and the model they run on
(one per agent from config.CLOUD_AI_MODEL's list, where it lists several):

  catalyst    the article: what changed, and how big for this company
  skeptic     the article plus the price context the screening prompt never
              sees (move so far in daily ranges, the gap, the market's move):
              argue the move is done. May veto.
  specialist  the article with a prompt for its event type: M&A (premium,
              chance of a higher bid, deal risk), trial data (binary risk),
              results (guidance against the headline beat), share sales...
  historian   the base rates of the app's own ledger for this kind of story
              (event type and direction), and the latest such trades
  devil       the other four's views, one round: the strongest case against

Stage 3 is a rule, not an LLM judge: the panel would trade when at least
PANEL_MIN_AGREE of the four voting agents say TRADE and the skeptic doesn't
veto. A fixed rule can be measured and tuned; a judge can't.

Cost: five short calls per alert that reaches the price check - a few
hundred a month, against tens of thousands of screening calls.
"""

import json
import os
import threading

import config
from cloud_analyzer import JSON_ONLY_SYSTEM_PROMPT
from llm_prompts import parse_json_response
from local_time import now_local

VOTES_FILE = "data/panel_votes.json"
# Newest records kept. A record is ~2KB (five short reasons), so ~2MB.
MAX_RECORDS = 1000
AGENTS = ("catalyst", "skeptic", "specialist", "historian")
# How many of AGENTS must vote TRADE for the panel to trade (with no veto).
PANEL_MIN_AGREE = 3
# The latest same-kind trades the historian is shown.
HISTORY_EXAMPLES = 8
# Article text each agent reads.
TEXT_CHARS = 3000
# Models an agent tries before giving up on its vote. The panel runs on the
# scan thread, so an outage must cost a few calls, not the whole list five
# times over.
MODELS_PER_AGENT = 2

_VOTE_FORMAT = """Respond with JSON only:
{"vote": "TRADE" or "PASS", "conviction": 1-5, "reason": "one or two sentences"%s}"""

_TRADE_LINE = "The proposed trade: {side} {ticker} ({company}), because of the news below."

CATALYST = """You are a catalyst analyst. Judge only what the news changes for the company.
{trade}
Ask: what exactly changed, is it new, and how large is it relative to the company's size
(market cap {cap})? A trade is worth taking only if the change is material for THIS company.
{vote_format}

--- NEWS ---
{article}"""

SKEPTIC = """You are the "priced-in" skeptic. Your job is to argue that the move is already done,
and to veto the trade if it clearly is.
{trade}

Price context (a daily range is the stock's 14-day average true range, {atr}):
{context}

Weigh how far the stock has moved since the news against how big the news is. Moves of several
daily ranges, gaps that already match the news, or a headline that reports the move all say it is
priced in. Vote TRADE only if a meaningful part of the move is plausibly still ahead. Set "veto"
true only when you are confident the trade is chasing a finished move.
{vote_format}

--- NEWS ---
{article}"""

SPECIALIST = """You are a specialist in {kind} events.
{trade}
{focus}
{vote_format}

--- NEWS ---
{article}"""

SPECIALIST_FOCUS = {
    "M&A": ("For a takeover target: the premium, how likely a higher or rival bid is, and the risk "
            "the deal breaks (regulators, financing, shareholder votes). For an acquirer: whether "
            "the market will reward or punish the deal."),
    "CLINICAL": ("Trial data is binary: weigh how decisive the readout is, its effect size, safety, "
                 "and how much of the company's value rides on this drug."),
    "REGULATORY": ("Weigh whether the decision was expected, how much revenue it moves, and whether "
                   "it is final or can be appealed."),
    "EARNINGS": ("Results move on guidance and the quality of the beat, not the headline: weigh the "
                 "outlook, margins and any one-offs against what was expected."),
    "GUIDANCE": "Weigh the size of the change to the outlook and how credible it is.",
    "OFFERING": ("A share sale dilutes holders and is usually priced at a discount; weigh its size "
                 "against the market cap and what the money is for."),
    "CONTRACT": "Weigh the contract's value against the company's annual revenue, and its certainty.",
}
SPECIALIST_DEFAULT = ("Weigh how material and how certain this event is for the company, and how "
                      "events like it usually move a stock of this size.")

HISTORIAN = """You are the historian. You know how this app's past trades on similar stories went,
and judge the proposed trade by those base rates rather than by the story itself.
{trade}
This story: event type {event}, direction {side}.

Base rates from the app's own record (net of costs; small samples are noise):
{rates}

The latest trades on {event} stories:
{examples}

Vote TRADE only if the record for this kind of story is positive or there is a clear reason this
one is different.
{vote_format}

--- NEWS (headline) ---
{headline}"""

DEVIL = """You are the devil's advocate. Four analysts have judged a proposed trade. Make the
strongest case AGAINST it - whatever they concluded - and say whether that case should stop it.
{trade}

Their views:
{views}

Respond with JSON only:
{{"vote": "TRADE" or "PASS", "conviction": 1-5, "case_against": "the strongest case against, in two
sentences", "reason": "why it does or doesn't stop the trade"}}

--- NEWS (headline) ---
{headline}"""


def _pct(value):
    return "unknown" if value is None else f"{value * 100:+.1f}%"


def _describe_context(ctx, plan):
    """The price context as plain lines for the skeptic."""
    ctx, plan = ctx or {}, plan or {}
    atr = plan.get('atr_pct')
    lines = [
        f"- Move on the news so far, net of the market: {_pct(plan.get('already_moved_pct'))}"
        + (f" ({plan['already_moved_pct'] / atr:.1f} daily ranges)"
           if atr and plan.get('already_moved_pct') is not None else "")
        + (f", measured from the {'publish time' if plan.get('moved_from') == 'publish' else 'previous close'}"
           if plan.get('moved_from') else ""),
        f"- Change since the previous close (the gap, for news out of hours): "
        f"{_pct(ctx.get('change_since_close_pct'))}",
        f"- Change over the five sessions before: {_pct(ctx.get('change_5d_pct'))}",
        f"- The market over the same time: {_pct(plan.get('market_move_pct'))}",
        f"- The analyst's expected total move: {_pct(plan.get('expected_move_pct'))}",
    ]
    return "\n".join(lines)


def _article_text(article, analysis):
    text = (article.get('content') or article.get('description') or "")[:TEXT_CHARS]
    return (f"Headline: {article.get('title')}\nPublished: {article.get('published_ts') or 'unknown'}\n"
            f"Screening analyst's summary: {analysis.get('explanation') or ''}\n\n{text}")


def base_rates(trades, event_type, direction):
    """Expectancy and win rate of closed records - paper trades and skipped
    trades alike - for this event type, this direction, and both together.
    Returns (text, examples text)."""
    def line(label, rows):
        nets = [r['net_pct'] for r in rows if r.get('net_pct') is not None]
        if not nets:
            return f"- {label}: no closed trades yet"
        wins = sum(1 for n in nets if n > 0)
        return (f"- {label}: {len(nets)} trades, {sum(nets) / len(nets) * 100:+.1f}% average, "
                f"{wins / len(nets) * 100:.0f}% won")
    closed = [t for t in trades if t.get('status') == 'CLOSED']
    same_event = [t for t in closed if (t.get('event_type') or '') == (event_type or '')]
    same_side = [t for t in closed if t.get('direction') == direction]
    both = [t for t in same_event if t.get('direction') == direction]
    rates = "\n".join([line("All", closed), line(f"{direction}s", same_side),
                       line(f"{event_type or 'untagged'} stories", same_event),
                       line(f"{event_type or 'untagged'} {direction}s", both)])
    recent = sorted(same_event, key=lambda t: t.get('closed_at') or '')[-HISTORY_EXAMPLES:]
    examples = "\n".join(f"- {t.get('direction')} {t.get('ticker')}: {_pct(t.get('net_pct'))} "
                         f"({t.get('reason')}) - {(t.get('headline') or '')[:90]}"
                         for t in recent) or "- none yet"
    return rates, examples


def decide(votes):
    """Stage 3: the panel's decision from its votes - a rule, not a model.
    {'trade': bool, 'agree': n TRADE votes of AGENTS, 'voted': n agents that
    answered, 'vetoed': bool}."""
    answered = [votes[a] for a in AGENTS if votes.get(a)]
    agree = sum(1 for v in answered if v.get('vote') == 'TRADE')
    vetoed = bool((votes.get('skeptic') or {}).get('veto'))
    return {'trade': agree >= PANEL_MIN_AGREE and not vetoed, 'agree': agree,
            'voted': len(answered), 'vetoed': vetoed}


def _clean_vote(data, model):
    """An agent's reply, reduced to what is stored."""
    if not isinstance(data, dict):
        return None
    vote = str(data.get('vote') or '').strip().upper()
    try:
        conviction = int(round(float(data.get('conviction'))))
    except (TypeError, ValueError):
        conviction = None
    out = {'vote': vote if vote in ('TRADE', 'PASS') else None, 'conviction': conviction,
           'reason': str(data.get('reason') or '')[:400], 'model': model}
    if 'veto' in data:
        out['veto'] = data.get('veto') is True or str(data.get('veto')).lower() == 'true'
    if data.get('case_against'):
        out['case_against'] = str(data['case_against'])[:400]
    return out


class Panel:
    """Runs the agents for an alert and records their votes. Needs the cloud
    engine's providers (CloudAnalyzer.providers); with none - a local or
    keyword engine - it does nothing."""

    def __init__(self, providers, log=print, filename=VOTES_FILE):
        self.providers = list(providers or [])
        self.log = log
        self.filename = filename
        self._lock = threading.Lock()

    def _ask(self, agent_index, prompt):
        """(model, reply) from the agent's own model - the list's
        agent_index-th, so each agent runs on a different one where the list
        has several - falling back along the list, or (None, None)."""
        n = len(self.providers)
        for k in range(min(n, MODELS_PER_AGENT)):
            model, provider = self.providers[(agent_index + k) % n]
            data = parse_json_response(provider.complete(prompt, system=JSON_ONLY_SYSTEM_PROMPT))
            if data:
                return model, data
        return None, None

    def run(self, article, analysis, decision, context, plan, history):
        """Ask every agent about one alert, record the result, and return it
        - or None when there is no engine to ask."""
        if not self.providers:
            return None
        direction = decision.get('direction')
        ticker = decision.get('ticker')
        side = "LONG (buy)" if direction == 'LONG' else "SHORT (sell short)"
        trade = _TRADE_LINE.format(side=side, ticker=ticker,
                                   company=analysis.get('target_company') or ticker)
        text = _article_text(article, analysis)
        cap = decision.get('market_cap_usd')
        cap_text = f"~${cap / 1e6:,.0f}M" if cap else "unknown"
        event = analysis.get('event_type') or 'OTHER'
        rates, examples = base_rates(history, analysis.get('event_type'), direction)
        atr = (plan or {}).get('atr_pct')

        prompts = {
            'catalyst': CATALYST.format(trade=trade, cap=cap_text, article=text,
                                        vote_format=_VOTE_FORMAT % ""),
            'skeptic': SKEPTIC.format(trade=trade, article=text,
                                      atr=f"{atr * 100:.1f}%" if atr else "unknown",
                                      context=_describe_context(context, plan),
                                      vote_format=_VOTE_FORMAT % ', "veto": true or false'),
            'specialist': SPECIALIST.format(kind=event, trade=trade, article=text,
                                            focus=SPECIALIST_FOCUS.get(event, SPECIALIST_DEFAULT),
                                            vote_format=_VOTE_FORMAT % ""),
            'historian': HISTORIAN.format(trade=trade, event=event, side=direction, rates=rates,
                                          examples=examples, headline=article.get('title'),
                                          vote_format=_VOTE_FORMAT % ""),
        }
        votes = {}
        for i, agent in enumerate(AGENTS):
            try:
                model, data = self._ask(i, prompts[agent])
            except Exception as e:
                self.log(f"   (panel {agent} failed: {e})")
                model, data = None, None
            votes[agent] = _clean_vote(data, model)

        views = "\n".join(f"- {a}: {v['vote']} ({v.get('conviction')}/5) - {v['reason']}"
                          for a, v in votes.items() if v)
        try:
            model, data = self._ask(len(AGENTS), DEVIL.format(trade=trade, views=views or "- none",
                                                              headline=article.get('title')))
            votes['devil'] = _clean_vote(data, model)
        except Exception as e:
            self.log(f"   (panel devil's advocate failed: {e})")
            votes['devil'] = None

        record = {
            'time': now_local().isoformat(),
            'url': article.get('url'),
            'headline': article.get('title'),
            'ticker': ticker,
            'direction': direction,
            'event_type': analysis.get('event_type'),
            # What the pipeline itself did, to compare against.
            'pipeline_opened': bool(decision.get('opened')),
            'pipeline_reason': decision.get('reason'),
            'watch_id': (decision.get('watch') or {}).get('id'),
            'votes': votes,
            'panel': decide(votes),
        }
        self._append(record)
        p = record['panel']
        self.log(f"   🧑‍⚖️ Panel (shadow): {p['agree']}/{p['voted']} would trade"
                 f"{', skeptic vetoes' if p['vetoed'] else ''} -> "
                 f"{'TRADE' if p['trade'] else 'PASS'}")
        return record

    def load(self):
        if not os.path.exists(self.filename):
            return []
        try:
            with open(self.filename, encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (ValueError, OSError):
            return []

    def _append(self, record):
        with self._lock:
            records = self.load()
            records.append(record)
            records = records[-MAX_RECORDS:]
            tmp = self.filename + ".tmp"
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(records, f, indent=1)
                os.replace(tmp, self.filename)
            except OSError as e:
                self.log(f"   (panel votes not saved: {e})")


def agent_scores(records, outcomes):
    """Step 2 of the rollout: how each agent's votes did. `outcomes` maps an
    article URL to the net result of the trade or skipped trade on it. For
    each agent (and the panel's rule): how many votes have a result, and the
    average result when it said TRADE and when it said PASS - an agent worth
    listening to has the first well above the second."""
    scores = {}
    for rec in records:
        net = outcomes.get(rec.get('url'))
        if net is None:
            continue
        calls = {a: (v or {}).get('vote') for a, v in (rec.get('votes') or {}).items()}
        calls['panel rule'] = 'TRADE' if (rec.get('panel') or {}).get('trade') else 'PASS'
        for agent, vote in calls.items():
            if vote not in ('TRADE', 'PASS'):
                continue
            s = scores.setdefault(agent, {'TRADE': [], 'PASS': []})
            s[vote].append(net)
    return {agent: {vote: {'n': len(v), 'avg': (sum(v) / len(v)) if v else None}
                    for vote, v in s.items()}
            for agent, s in scores.items()}
