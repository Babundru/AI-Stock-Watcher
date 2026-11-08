"""The weekly reviewer agent (CHANGE.md C12): once a week it reads the
closed trades, the skipped trades and the agent panel's record, and writes
proposed rule changes for a human to approve - the analysis at the top of
CHANGE.md, done automatically. It changes nothing itself.

    py weekly_review.py         write a review now (data/reviews/)

The scan loop runs it at the weekend when the last review is a week old
(config.WEEKLY_REVIEW, main._maybe_weekly_review), on the panel's worker
thread, and sends a notification when one is ready.
"""

import datetime
import os

import config
from local_time import now_local

REVIEW_DIR = "data/reviews"
REVIEW_EVERY = datetime.timedelta(days=7)

# The settings a review may propose changing, shown to it with their values.
TUNABLE = ("MIN_IMPACT", "MIN_CONFIDENCE", "ALLOW_SHORTS", "SHORT_MIN_IMPACT",
           "STOP_ATR_MULT", "STOP_LOSS_PCT", "FIRST_DAY_STOP_MULT", "LET_WINNERS_RUN",
           "TAKE_HALF_AT_TARGET", "TRAIL_AFTER_TARGET_MULT", "MIN_MOVE_ATR_MULT",
           "EXHAUSTED_ATR_MULT", "EXHAUSTED_MAX_ATR_MULT", "AGAINST_NEWS_ATR_MULT",
           "REPORTED_MOVE_MAX_PCT", "MERGER_ARB", "SKIP_INTRADAY", "ENTRY_DELAY_AFTER_OPEN_MIN",
           "ENTRY_WAIT_FOR_SESSION", "QUEUED_SKIP_EVENT_TYPES",
           "MIN_MARKET_CAP_USD", "MIN_DAILY_TURNOVER_USD", "SCREENER_MODE", "PAPER_RISK_PCT")

PROMPT = """You are reviewing a news-driven paper-trading strategy, as its weekly reviewer. Below
are its results to date. Propose rule changes a human should consider - or none, if the data does
not support any.

Rules for your review:
- Every proposal names one setting from the list, its current value, the value you propose, and
  the evidence: the groups, their trade counts and their averages.
- Below about 30 trades a group is noise. Say so, and prefer "keep collecting" to a change backed
  by a handful of trades. Never propose more than three changes.
- Skipped trades are signals a rule refused, followed as if traded: a rule whose skipped trades
  made money is keeping the strategy out of winners.
- Results are net of costs, per trade, as a % of the position.
- Judge the strategy that is running now. The pooled figures mix every strategy version since the
  start, most of whose rules are no longer in force; read "BY STRATEGY VERSION" and "LAST 14 DAYS"
  first, and say plainly when the newest version is doing worse than the pooled record.
- A pooled average carried by a few outsized winners is not an edge: compare it with the median and
  the average without the three best trades.

Write plain Markdown with three sections: "What the data says" (the few numbers that matter),
"Proposed changes" (each as: setting, from -> to, evidence, how sure), and "Watch next week".

--- CURRENT SETTINGS ---
{settings}

--- BY STRATEGY VERSION (newest first; the first one listed is the one running, or the last that closed trades) ---
{versions}

--- LAST 14 DAYS (closed in that time, any version) ---
{recent}

--- CLOSED PAPER TRADES, ALL VERSIONS POOLED ---
{paper}

--- SKIPPED TRADES, BY THE RULE THAT REFUSED THEM ---
{skipped}

--- AGENT PANEL (shadow mode) - average result when each agent said TRADE / PASS ---
{panel}
"""


def _pct(value):
    return "n/a" if value is None else f"{value * 100:+.2f}%"


def _groups(stats):
    lines = [f"{stats['trades']} closed, {stats['open']} open; expectancy {_pct(stats['expectancy'])}, "
             f"win rate {stats['win_rate'] * 100:.0f}%, avg win {_pct(stats['avg_win'])}, "
             f"avg loss {_pct(stats['avg_loss'])}"]
    for key, label in (("by_strategy", "strategy version"), ("by_mode", "mode"),
                       ("by_direction", "direction"), ("by_impact", "impact"),
                       ("by_event_type", "event type"), ("by_horizon", "horizon"),
                       ("by_reason", "exit reason"), ("by_confidence", "AI confidence"),
                       ("by_model", "model"), ("by_source", "news source"),
                       ("by_screener", "screener verdict")):
        rows = stats.get(key) or {}
        if rows:
            lines.append(f"By {label}: " + "; ".join(
                f"{name} {row['trades']} trades {_pct(row['expectancy'])} "
                f"({row['win_rate'] * 100:.0f}% won)" for name, row in rows.items()))
    return "\n".join(lines)


def _version_key(name):
    digits = ''.join(ch for ch in str(name) if ch.isdigit())
    return int(digits) if digits else -1


def _robust(rets):
    """Median and the average without the three best trades - what is left
    of an average once its outliers are taken out."""
    if not rets:
        return "no closed trades"
    srt = sorted(rets)
    mid = len(srt) // 2
    median = srt[mid] if len(srt) % 2 else (srt[mid - 1] + srt[mid]) / 2
    rest = srt[:-3]
    without = _pct(sum(rest) / len(rest)) if rest else "n/a"
    return (f"{len(rets)} trades, average {_pct(sum(rets) / len(rets))}, median {_pct(median)}, "
            f"without the 3 best {without}, {sum(1 for r in rets if r > 0) / len(rets) * 100:.0f}% won")


def _breakdown(trades):
    from paper_trader import _group
    parts = []
    for label, key in (("direction", lambda t: t['direction']),
                       ("event type", lambda t: t.get('event_type') or '?'),
                       ("exit reason", lambda t: t.get('reason') or '?')):
        rows = _group(trades, key)
        parts.append(f"  by {label}: " + "; ".join(
            f"{name} {row['trades']} {_pct(row['expectancy'])}" for name, row in rows.items()))
    return "\n".join(parts)


def version_sections(paper, shadows, versions=3):
    """The newest `versions` strategy versions with closed trades, each on
    its own: paper trades (with medians and breakdowns) and skipped trades
    by rule. Pooled numbers hide a new version doing worse than the old."""
    trades = paper.closed() if paper else []
    skipped = shadows.closed() if shadows else []
    names = sorted({t.get('strategy') or 'v1' for t in trades}
                   | {r.get('strategy') or 'v1' for r in skipped}, key=_version_key, reverse=True)
    if not names:
        return "none closed yet"
    import strategy
    out = []
    for name in names[:versions]:
        mine = [t for t in trades if (t.get('strategy') or 'v1') == name]
        running = " (running now)" if name == strategy.STRATEGY_VERSION else ""
        out.append(f"{name}{running}: " + _robust([t['net_pct'] for t in mine]))
        if mine:
            out.append(_breakdown(mine))
        refused = {}
        for r in skipped:
            if (r.get('strategy') or 'v1') == name:
                refused.setdefault(r.get('skip_rule') or '?', []).append(r['net_pct'])
        if refused:
            out.append("  skipped, by rule: " + "; ".join(
                f"{rule} {len(v)} {_pct(sum(v) / len(v))}" for rule, v in sorted(refused.items())))
    return "\n".join(out)


def recent_section(paper, now=None, days=14):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(days=days)
    recent = []
    for t in (paper.closed() if paper else []):
        try:
            closed = datetime.datetime.fromisoformat(t.get('closed_at'))
        except (TypeError, ValueError):
            continue
        if closed.tzinfo is None:
            closed = closed.replace(tzinfo=datetime.timezone.utc)
        if closed >= since:
            recent.append(t)
    if not recent:
        return "no trades closed"
    return _robust([t['net_pct'] for t in recent]) + "\n" + _breakdown(recent)


def build_prompt(paper, shadows, panel_scores):
    stats = paper.stats() if paper else None
    summary = shadows.summary() if shadows else None
    settings = "\n".join(f"{key} = {getattr(config, key, None)!r}" for key in TUNABLE)
    skipped = ("\n".join(f"{rule}: {row['trades']} trades, {_pct(row['expectancy'])} expectancy, "
                         f"{row['win_rate'] * 100:.0f}% won"
                         for rule, row in summary['by_rule'].items())
               if summary and summary['by_rule'] else "none closed yet")
    panel = ("\n".join(f"{agent}: TRADE {row['TRADE']['n']} -> {_pct(row['TRADE']['avg'])}, "
                       f"PASS {row['PASS']['n']} -> {_pct(row['PASS']['avg'])}"
                       for agent, row in panel_scores.items())
             if panel_scores else "no results yet")
    return PROMPT.format(settings=settings, paper=_groups(stats) if stats else "none closed yet",
                         versions=version_sections(paper, shadows), recent=recent_section(paper),
                         skipped=skipped, panel=panel)


def last_review_at():
    """When the newest review was written, or None."""
    try:
        files = [os.path.join(REVIEW_DIR, f) for f in os.listdir(REVIEW_DIR) if f.endswith(".md")]
    except OSError:
        return None
    if not files:
        return None
    return datetime.datetime.fromtimestamp(max(os.path.getmtime(f) for f in files),
                                           datetime.timezone.utc)


def due(now=None):
    last = last_review_at()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return last is None or now - last >= REVIEW_EVERY


def write_review(providers, paper, shadows, panel_scores, log=print):
    """Ask the models (in priority order) for a review and save it. Returns
    the file's path, or None if no model answered."""
    prompt = build_prompt(paper, shadows, panel_scores)
    for model, provider in providers:
        text = provider.complete(prompt)
        if text and text.strip():
            os.makedirs(REVIEW_DIR, exist_ok=True)
            path = os.path.join(REVIEW_DIR, f"review-{now_local():%Y-%m-%d}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# Weekly review - {now_local():%Y-%m-%d}\n\n"
                        f"_Written by {model}. Proposals only: nothing here has been applied._\n\n")
                f.write(text.strip() + "\n")
            log(f"📝 Weekly review written: {path}")
            return path
    log("📝 Weekly review: no model answered")
    return None


def outcomes(paper, shadows):
    """Article URL -> net result, from the closed trades and skipped trades
    (paper ones first) - what the panel's votes are scored against."""
    out = {}
    for r in (shadows.closed() if shadows else []):
        if r.get('url'):
            out[r['url']] = r['net_pct']
    for t in (paper.closed() if paper else []):
        if t.get('url'):
            out[t['url']] = t['net_pct']
    return out


def main():
    from cloud_analyzer import CloudAnalyzer
    from panel import Panel, agent_scores
    from paper_trader import PaperTrader
    from shadow_trades import ShadowBook

    analyzer = CloudAnalyzer()
    if not analyzer.providers:
        print("Cloud AI is not set up - the review needs it.")
        return
    paper = PaperTrader(cost_pct=config.PAPER_COST_PCT)
    shadows = ShadowBook(cost_pct=config.PAPER_COST_PCT)
    scores = agent_scores(Panel([]).load(), outcomes(paper, shadows))
    path = write_review(analyzer.providers, paper, shadows, scores)
    if path:
        print(open(path, encoding="utf-8").read())


if __name__ == "__main__":
    main()
