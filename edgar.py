"""SEC EDGAR 8-K filings as a news source.

An 8-K is the company's own announcement of a material event, filed within
minutes of the press release - the primary source the newswires report on,
and usually ahead of them. EDGAR publishes the newest filings as an Atom
feed (FEED_URL). Each entry names the filer and lists the 8-K's items
("Item 2.02: Results of Operations ..."), which is enough to drop the
routine ones - shareholder-vote results, bylaw changes, an exhibit-only
filing - before they cost a model call.

The text the model reads is the filing's press release (exhibit 99.x) when
it has one, else the 8-K itself: the index page the feed links to is only
a table of documents.

SEC refuses requests without a User-Agent naming who is asking
(https://www.sec.gov/os/accessing-edgar-data) - a browser's gets HTTP 403 -
so the source is only polled when config.SEC_USER_AGENT is set, and every
sec.gov request is sent with it. Their limit is 10 requests a second; this
makes one feed request a scan and two per filing analysed.
"""

import re

from bs4 import BeautifulSoup

FEED_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K"
            "&company=&dateb=&owner=include&start=0&count=40&output=atom")
SOURCE_NAME = "SEC 8-K"

# Items that can move a stock on their own. Left out: 3.02 (unregistered
# share sales), 5.03 (bylaws), 5.05 (code of ethics), 5.07 (vote results),
# 9.01 (exhibits) and the asset-backed 6.x items - nearly always routine.
# 7.01 and 8.01 are catch-alls, but they are where most press releases are
# furnished, so they stay.
MATERIAL_ITEMS = {
    "1.01": "Material agreement",
    "1.02": "Termination of a material agreement",
    "1.03": "Bankruptcy or receivership",
    "1.05": "Cybersecurity incident",
    "2.01": "Completed acquisition or disposal",
    "2.02": "Results of operations",
    "2.05": "Exit or restructuring costs",
    "2.06": "Material impairment",
    "3.01": "Delisting notice",
    "4.01": "Change of auditor",
    "4.02": "Prior financial statements can no longer be relied on",
    "5.01": "Change in control",
    "5.02": "Departure or appointment of directors or officers",
    "7.01": "Regulation FD disclosure",
    "8.01": "Other events",
}

_ITEM = re.compile(r"Item\s+(\d\.\d\d)", re.I)
# "8-K - UFP INDUSTRIES INC (0000912767) (Filer)" -> "UFP INDUSTRIES INC"
_FILER = re.compile(r"^\s*8-K(?:/A)?\s*-\s*(.*?)\s*\(\d{10}\)", re.I)


def is_edgar_url(url):
    return "sec.gov" in (url or "").lower()


def items(summary):
    """The 8-K item numbers listed in a feed entry's summary, in order."""
    found = []
    for item in _ITEM.findall(summary or ""):
        if item not in found:
            found.append(item)
    return found


def is_material(item_list):
    return any(item in MATERIAL_ITEMS for item in item_list)


def company(title):
    """The filer's name from a feed entry's title, in title case when EDGAR
    shouted it, or None."""
    m = _FILER.match(title or "")
    if not m:
        return None
    name = m.group(1).strip()
    return name.title() if name.isupper() else name


def headline(title, item_list):
    """A headline the model and the dashboard can read: "UFP Industries Inc
    files 8-K: Material agreement; Regulation FD disclosure"."""
    name = company(title) or (title or "").strip()
    what = "; ".join(MATERIAL_ITEMS[i] for i in item_list if i in MATERIAL_ITEMS)
    return f"{name} files 8-K: {what}" if what else f"{name} files 8-K"


def document_url(index_html, base="https://www.sec.gov"):
    """The document worth reading in a filing index page: the first press
    release exhibit (EX-99.x), else the 8-K itself. None if neither is
    listed."""
    soup = BeautifulSoup(index_html, "html.parser")
    try:
        main = None
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            link = cells[2].find("a")
            kind = cells[3].get_text(strip=True).upper()
            if not link or not link.get("href"):
                continue
            # The 8-K itself is linked through the inline-XBRL viewer
            # ("/ix?doc=/Archives/..."); the document is the part after doc=.
            href = link["href"].split("doc=", 1)[-1]
            url = href if href.startswith("http") else base + href
            if kind.startswith("EX-99"):
                return url
            if main is None and kind.startswith("8-K"):
                main = url
        return main
    finally:
        soup.decompose()


def document_text(html, limit):
    """Plain text of a filing document, at most `limit` characters. Filings
    are laid out in <div>s and <font>s as often as in <p>s, so this reads
    the whole body rather than paragraphs only."""
    soup = BeautifulSoup(html, "html.parser")
    try:
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ").split())
        return text[:limit] or None
    finally:
        soup.decompose()
