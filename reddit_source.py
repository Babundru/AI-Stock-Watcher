"""Reddit subreddits as a news source: each post, analysed together with its
top comments.

Why feeds, not Reddit's API
---------------------------
Reddit's unauthenticated .json endpoints have returned 403 since late May
2026, and new OAuth apps need Reddit's manual approval. The public Atom feeds
still work -

    /r/<sub>/new/.rss        the newest posts, self-text included
    <post permalink>/.rss    the post, followed by its comments

- but only about ONE request a minute is allowed per IP, shared by every
feed: asking for a different subreddit two seconds after a good request gets
a 429 (measured, September 2026). Everything below follows from that budget:

  * One RedditPoller serves every Reddit source and makes at most one
    request per poll(), i.e. per scan cycle. It never sleeps - the scan loop
    must not stall for a minute waiting on Reddit's budget.
  * A post is analysed exactly once, REDDIT_COMMENT_DELAY_MIN after it was
    posted, together with its top comments. The comments are what tell a
    lone pump apart from something traders are reacting to, and each post's
    comment feed costs a whole minute of budget, so it is fetched only once.
  * What has been seen, and what is still waiting for its comments, is kept
    in data/reddit_state.json, so a restart neither repeats nor forgets.
"""

import datetime
import json
import os
import re
import threading
import time
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

import config

# Every article built here has a source starting with this - it is how the
# rest of the app recognises a Reddit post (prompt framing, REDDIT_CAN_TRADE).
SOURCE_PREFIX = "Reddit/"

# Reddit asks automated clients to identify themselves; a browser disguise is
# what gets an IP flagged.
USER_AGENT = "python:stocks-ai-watcher:v1.0 (personal news monitor)"
FEED_HOST = "https://www.reddit.com"
FEED_ACCEPT = "application/atom+xml, application/xml;q=0.9, */*;q=0.8"

# Seconds between requests. Reddit's window is a minute; a little over it
# keeps a request from landing just before the reset and eating a 429.
MIN_REQUEST_GAP = 62
REQUEST_TIMEOUT = 15
MAX_FEED_BYTES = 2 * 1024 * 1024

LISTING_LIMIT = 25
# A post whose comment feed fails this many times is analysed without its
# comments rather than lost.
MAX_ATTEMPTS = 3
RETRY_DELAY = 5 * 60
# Posts waiting for their comments. Busy subreddits can produce posts faster
# than one request a minute drains them; past this the oldest are dropped.
MAX_PENDING = 60
# Post ids remembered as already seen.
MAX_SEEN = 3000

# Fits the prompt's 5000-character article budget with room to spare.
CONTENT_LIMIT = 4500
POST_BODY_LIMIT = 2500
COMMENT_CHAR_LIMIT = 400
MAX_COMMENTS = 25
MAX_DELAY_MIN = 240

BOT_AUTHORS = {"automoderator", "visualmod"}
# Pinned megathreads ("Daily Discussion Thread for ...", "Weekly Earnings
# Thread", "What Are Your Moves Tomorrow") are thousands of unrelated
# comments rather than a story, and a feed returns only a handful of them.
MEGATHREAD = re.compile(
    r"\b(?:daily|weekly|weekend)\b.{0,30}\b(?:discussion|thread|earnings)\b|what are your moves",
    re.I)
GONE = {"[deleted]", "[removed]"}

_SUB_NAME = re.compile(r"[A-Za-z0-9_]{2,21}")
_SUB_IN_PATH = re.compile(r"(?:^|/)r/([A-Za-z0-9_]{2,21})(?=/|$)", re.I)
_THING_ID = re.compile(r"(t[13])_([a-z0-9]+)", re.I)
_MEDIA_URL = re.compile(r"https?://(?:preview|i|v)\.redd\.it/\S+")
_SUBMITTED_BY = re.compile(r"\s*submitted by\s+/u/\S+\s*\[link\]\s*\[comments\]\s*$", re.I)


# --- links -------------------------------------------------------------------

def is_reddit_url(url):
    """Whether a URL - or a bare "r/name" - points at Reddit."""
    u = (url or "").strip().lower()
    if u.startswith(("r/", "/r/")):
        return True
    host = urlparse(u if "://" in u else "https://" + u).hostname or ""
    return host == "reddit.com" or host.endswith(".reddit.com")


def parse_subreddit(url, bare_ok=False):
    """The subreddit a link refers to: "r/stocks", "reddit.com/r/stocks",
    "https://old.reddit.com/r/stocks/new/" and a post inside r/stocks all
    give "stocks". With bare_ok, a plain "stocks" does too. None when there
    is no subreddit (a user profile, the front page, not Reddit at all)."""
    u = (url or "").strip()
    if bare_ok and _SUB_NAME.fullmatch(u.strip("/")):
        return u.strip("/")
    if not is_reddit_url(u):
        return None
    path = urlparse(u).path if "://" in u else "/" + u.lstrip("/")
    m = _SUB_IN_PATH.search(path)
    return m.group(1) if m else None


def subreddit_url(name):
    return f"{FEED_HOST}/r/{name}/"


def is_reddit_article(article):
    return ((article or {}).get("source") or "").startswith(SOURCE_PREFIX)


# --- feed parsing ------------------------------------------------------------

def _html_text(html):
    """Visible text of a feed entry's HTML, minus Reddit's boilerplate (the
    "submitted by /u/x [link] [comments]" footer, image URLs)."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    try:
        text = soup.get_text(" ")
    finally:
        soup.decompose()
    text = _MEDIA_URL.sub("", " ".join(text.split()))
    return _SUBMITTED_BY.sub("", text).strip()


def _entry_html(entry):
    content = entry.get("content")
    if content:
        return content[0].get("value") or ""
    return entry.get("summary") or ""


def _author(entry):
    name = (entry.get("author") or "").strip()
    return name[3:] if name.startswith("/u/") else name


def _thing_id(entry):
    """("t3", id) for a post, ("t1", id) for a comment, ("", "") otherwise.
    Reddit's feeds use "t3_abc123"-style ids; the link is the fallback -
    /r/x/comments/<post>/<slug>/ is a post, one segment more a comment."""
    m = _THING_ID.fullmatch((entry.get("id") or "").strip())
    if m:
        return m.group(1).lower(), m.group(2).lower()
    parts = [p for p in urlparse(entry.get("link") or "").path.split("/") if p]
    if "comments" in parts:
        rest = parts[parts.index("comments") + 1:]
        if len(rest) >= 3:
            return "t1", rest[2].lower()
        if rest:
            return "t3", rest[0].lower()
    return "", ""


def _entry_time(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime.datetime(*parsed[:6], tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def _canonical(link):
    """https://www.reddit.com/r/x/comments/id/slug/ - no query, one host, so
    the same post always dedups to the same URL."""
    path = urlparse(link).path.rstrip("/")
    return f"{FEED_HOST}{path}/"


def _clip(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def parse_listing(body, subreddit):
    """The posts in a subreddit feed, newest first, as plain dicts."""
    posts = []
    for entry in feedparser.parse(body).entries:
        kind, ident = _thing_id(entry)
        link = (entry.get("link") or "").strip()
        if kind != "t3" or not link:
            continue
        posted = _entry_time(entry)
        posts.append({
            "id": ident,
            "subreddit": subreddit,
            "title": " ".join((entry.get("title") or "").split()),
            "link": _canonical(link),
            "author": _author(entry),
            "posted_ts": posted.isoformat() if posted else None,
            "body": _clip(_html_text(_entry_html(entry)), POST_BODY_LIMIT),
        })
    return posts


def parse_thread(body, limit):
    """A post's comment feed -> (the post's current text or None, comments).

    The post's text is re-read because it may have changed while the post
    waited: "[removed]" means the moderators took it down. Comments are
    [(author, text)] in feed order, without deleted ones and bots.
    """
    post_text = None
    comments = []
    for entry in feedparser.parse(body).entries:
        kind, _ = _thing_id(entry)
        text = _html_text(_entry_html(entry))
        if kind == "t3":
            post_text = text
            continue
        if kind != "t1" or len(comments) >= limit:
            continue
        author = _author(entry)
        if not text or text in GONE or author.lower() in BOT_AUTHORS:
            continue
        comments.append((author or "?", _clip(text, COMMENT_CHAR_LIMIT)))
    return post_text, comments


def build_article(post, comments):
    """One analysable article: the post, then as many of its top comments as
    fit the prompt's budget."""
    body = post.get("body") or ""
    head = body or "(no text - a link or image post)"
    used = len(head)
    lines = []
    for author, text in comments:
        line = f"- u/{author}: {text}"
        if used + len(line) + 40 > CONTENT_LIMIT:
            break
        lines.append(line)
        used += len(line) + 1
    content = head
    if lines:
        content += f"\n\nTop comments ({len(lines)}):\n" + "\n".join(lines)
    return {
        "title": post["title"],
        # A link post has no text of its own; its title is the whole story.
        "description": _clip(body, 300) if body else post["title"],
        "url": post["link"],
        "publishedAt": post.get("posted_ts") or "",
        "published_ts": post.get("posted_ts"),
        "source": f"{SOURCE_PREFIX}r/{post['subreddit']}",
        "content": content,
    }


def _epoch(iso):
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def _header(headers, name):
    value = (headers or {}).get(name)
    if value is None and hasattr(headers, "items"):
        value = next((v for k, v in headers.items() if k.lower() == name), None)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_noise(post):
    return post["author"].lower() in BOT_AUTHORS or bool(MEGATHREAD.search(post["title"]))


# --- the poller --------------------------------------------------------------

class RedditPoller:
    """Shares Reddit's one-request-a-minute budget between every Reddit
    source. The scan loop calls poll() once per cycle."""

    def __init__(self, state_file="data/reddit_state.json", fetch=None,
                 clock=time.time, log=print):
        self.state_file = state_file
        self._fetch = fetch or self._http_get
        self._clock = clock
        self._log = log
        # poll() runs on the scan thread; pending_counts() on the dashboard's.
        self._lock = threading.Lock()
        self._session = None
        self._next_request_at = 0.0
        self._load()

    # --- settings, read live so a change in Settings applies next cycle ---

    @staticmethod
    def _comments_wanted():
        return max(0, min(MAX_COMMENTS, int(config.REDDIT_COMMENTS_PER_POST or 0)))

    @staticmethod
    def _delay():
        return max(0, min(MAX_DELAY_MIN, int(config.REDDIT_COMMENT_DELAY_MIN or 0))) * 60

    @staticmethod
    def _listing_interval(n_subs):
        # Listings may use at most half the budget, so comment fetches are
        # never starved however many subreddits are added.
        return max(config.REDDIT_POLL_MINUTES, 2 * n_subs) * 60

    # --- state -------------------------------------------------------------

    def _load(self):
        data = {}
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                self._log(f"Reddit: couldn't read {self.state_file} ({e}) - starting fresh")
            if not isinstance(data, dict):
                data = {}
        self.seen = [s for s in (data.get("seen") or []) if isinstance(s, str)][-MAX_SEEN:]
        self._seen_set = set(self.seen)
        self.pending = [p for p in (data.get("pending") or [])
                        if isinstance(p, dict) and p.get("id") and p.get("link")]
        self.next_listing = {}
        for key, value in (data.get("next_listing") or {}).items():
            try:
                self.next_listing[key] = float(value)
            except (TypeError, ValueError):
                pass

    def _save(self):
        data = {"seen": self.seen, "pending": self.pending, "next_listing": self.next_listing}
        tmp = self.state_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_file)
        except OSError as e:
            self._log(f"Reddit: couldn't save {self.state_file}: {e}")

    def _mark_seen(self, post_id):
        if post_id in self._seen_set:
            return
        self.seen.append(post_id)
        self._seen_set.add(post_id)
        if len(self.seen) > MAX_SEEN:
            self.seen = self.seen[-MAX_SEEN:]
            self._seen_set = set(self.seen)

    def pending_counts(self):
        """{subreddit (lower case): posts waiting for their comments}."""
        with self._lock:
            counts = {}
            for p in self.pending:
                key = p["subreddit"].lower()
                counts[key] = counts.get(key, 0) + 1
            return counts

    # --- one cycle -----------------------------------------------------------

    def poll(self, sources, is_seen=None):
        """Advance by at most one feed request and return the articles that
        became ready. `sources` are the enabled Reddit sources; `is_seen`
        (url -> bool) skips posts the app has already analysed."""
        subs = {}
        for source in sources:
            name = parse_subreddit(source.get("url"), bare_ok=True)
            if name:
                subs.setdefault(name.lower(), name)

        with self._lock:
            now = self._clock()
            changed = self._prune(subs, now)
            job = self._next_job(subs, now) if now >= self._next_request_at else None
            if job is None:
                if changed:
                    self._save()
                return []
            kind, target = job
            if kind == "listing":
                url = f"{FEED_HOST}/r/{subs[target]}/new/.rss?limit={LISTING_LIMIT}"
            else:
                url = f"{target['link']}.rss?sort=top&limit={min(3 * self._comments_wanted() + 1, 100)}"
            # Reserve the slot before letting go of the lock.
            self._next_request_at = now + MIN_REQUEST_GAP

        # The request runs outside the lock, so the dashboard asking for the
        # queue size never waits on Reddit.
        try:
            status, body, headers = self._fetch(url)
        except requests.RequestException as e:
            status, body, headers = None, b"", {}
            self._log(f"Reddit: request failed ({type(e).__name__}): {url[:90]}")

        with self._lock:
            now = self._clock()
            self._next_request_at = now + self._gap(status, headers)
            if kind == "listing":
                articles = self._on_listing(subs, target, status, body, now, is_seen)
            else:
                articles = self._on_thread(target, status, body, now)
            self._save()
            return articles

    def _prune(self, subs, now):
        """Forget waiting posts from subreddits no longer polled, posts too old
        to matter any more, and the oldest ones past MAX_PENDING."""
        before = len(self.pending)
        max_age = config.REDDIT_MAX_POST_AGE_HOURS * 3600 + self._delay()
        keep = []
        for p in self.pending:
            posted = _epoch(p.get("posted_ts"))
            if p["subreddit"].lower() not in subs:
                continue
            if posted is not None and now - posted > max_age:
                continue
            keep.append(p)
        if len(keep) > MAX_PENDING:
            keep.sort(key=lambda p: _epoch(p.get("posted_ts")) or 0, reverse=True)
            keep = keep[:MAX_PENDING]
        self.pending = keep
        for key in [k for k in self.next_listing if k not in subs]:
            del self.next_listing[key]
        return len(self.pending) != before

    def _next_job(self, subs, now):
        due = [k for k in subs if self.next_listing.get(k, 0) <= now]
        if due:
            return "listing", min(due, key=lambda k: self.next_listing.get(k, 0))
        ready = [p for p in self.pending if p.get("due_at", 0) <= now]
        if ready:
            return "thread", min(ready, key=lambda p: p["due_at"])
        return None

    @staticmethod
    def _gap(status, headers):
        """Seconds until the next request: Reddit's own reset time when it
        says the window is spent, never less than MIN_REQUEST_GAP."""
        reset = _header(headers, "x-ratelimit-reset")
        remaining = _header(headers, "x-ratelimit-remaining")
        if reset is not None and (status == 429 or (remaining is not None and remaining < 1)):
            return max(MIN_REQUEST_GAP, reset + 2)
        return MIN_REQUEST_GAP

    def _on_listing(self, subs, key, status, body, now, is_seen):
        name = subs[key]
        if status == 429:
            self._log(f"Reddit: r/{name} rate limited - retrying next minute")
            return []
        self.next_listing[key] = now + self._listing_interval(len(subs))
        if status != 200:
            hint = {403: " (private or quarantined?)", 404: " (no such subreddit?)"}.get(status, "")
            self._log(f"Reddit: r/{name} feed returned {status}{hint}")
            return []

        max_age = config.REDDIT_MAX_POST_AGE_HOURS * 3600
        wanted = self._comments_wanted()
        waiting = {p["id"] for p in self.pending}
        fresh = []
        for post in parse_listing(body, name):
            if post["id"] in self._seen_set or post["id"] in waiting:
                continue
            self._mark_seen(post["id"])
            posted = _epoch(post["posted_ts"])
            if (_is_noise(post) or posted is None or now - posted > max_age
                    or (is_seen and is_seen(post["link"]))):
                continue
            fresh.append(post)

        if not fresh:
            return []
        if not wanted:
            self._log(f"Reddit: r/{name} - {len(fresh)} new post(s)")
            return [build_article(post, []) for post in fresh]
        for post in fresh:
            post["due_at"] = max(now, _epoch(post["posted_ts"]) + self._delay())
            post["attempts"] = 0
            self.pending.append(post)
        self._log(f"Reddit: r/{name} - {len(fresh)} new post(s), each analysed once its "
                  f"comments have had {self._delay() // 60} min to arrive")
        return []

    def _on_thread(self, post, status, body, now):
        if status == 429:
            return []
        if status == 200:
            post_text, comments = parse_thread(body, self._comments_wanted())
            self._drop(post)
            if post_text in GONE:
                self._log(f"Reddit: skipped a removed post - {post['title'][:60]}")
                return []
            self._log(f"Reddit: {len(comments)} comment(s) for {post['title'][:60]}")
            return [build_article(post, comments)]
        if status == 404:
            self._drop(post)
            return []
        post["attempts"] = post.get("attempts", 0) + 1
        if post["attempts"] >= MAX_ATTEMPTS:
            self._drop(post)
            self._log(f"Reddit: comments unavailable ({status}) - analysing the post alone: "
                      f"{post['title'][:60]}")
            return [build_article(post, [])]
        post["due_at"] = now + RETRY_DELAY
        return []

    def _drop(self, post):
        self.pending = [p for p in self.pending if p["id"] != post["id"]]

    def _http_get(self, url):
        """GET a feed with Reddit's own rate-limit headers intact, reading at
        most MAX_FEED_BYTES."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": USER_AGENT, "Accept": FEED_ACCEPT})
        with self._session.get(url, timeout=REQUEST_TIMEOUT, stream=True) as response:
            chunks, total = [], 0
            for chunk in response.iter_content(8192):
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_FEED_BYTES:
                    break
            return response.status_code, b"".join(chunks), response.headers
