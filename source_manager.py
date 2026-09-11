import json
import os
import uuid
from typing import Dict, List, Optional

import reddit_source

# How far a source's claims can be taken at their word. "reporting" is news
# a newsroom wrote; "opinion" is someone's say-so - a post on X or Reddit -
# and a story from one only trades once the price has confirmed it
# (strategy.plan_trade). Each source can be set either way; these types
# start out as opinion.
REPORTING = "reporting"
OPINION = "opinion"
TRUST_LEVELS = (REPORTING, OPINION)
_OPINION_TYPES = ("twitter", "reddit")


def default_trust(source_type):
    return OPINION if source_type in _OPINION_TYPES else REPORTING


def source_trust(source):
    """A source's trust setting, or its type's default when it has none
    (sources saved before the setting existed)."""
    trust = (source or {}).get("trust")
    return trust if trust in TRUST_LEVELS else default_trust((source or {}).get("type"))


def article_trust(article):
    """The trust of the source an article came from. The collector tags
    custom sources' articles with it; anything untagged goes by where it
    came from - the built-in news feeds are reporting, X and Reddit posts
    opinion."""
    trust = (article or {}).get("trust")
    if trust in TRUST_LEVELS:
        return trust
    source = (article or {}).get("source") or ""
    if reddit_source.is_reddit_article(article) or source.startswith("Twitter/"):
        return OPINION
    return REPORTING


class SourceManager:
    """Manages user-configurable news sources for web scraping."""
    
    def __init__(self, sources_file='data/news_sources.json'):
        self.sources_file = sources_file
        self.sources = self._load_sources()
    
    def _load_sources(self) -> Dict:
        """Load sources from JSON file or create default."""
        if os.path.exists(self.sources_file):
            try:
                with open(self.sources_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
                    raise ValueError("sources file has no 'sources' list")
                return data
            except Exception as e:
                print(f"Error loading sources: {e}")
                return self._get_default_sources()
        else:
            # Create with defaults
            defaults = self._get_default_sources()
            self._save_sources(defaults)
            return defaults

    def reload(self):
        """Re-read the file - for a manager whose file another instance
        has since written."""
        self.sources = self._load_sources()
    
    def _get_default_sources(self) -> Dict:
        """Get default news sources."""
        return {
            "sources": [
                {
                    "id": str(uuid.uuid4()),
                    "name": "Reuters Business",
                    "url": "https://www.reuters.com/business/",
                    "enabled": True,
                    "type": "webpage"
                },
                {
                    "id": str(uuid.uuid4()),
                    "name": "Bloomberg Markets",
                    "url": "https://www.bloomberg.com/markets",
                    "enabled": True,
                    "type": "webpage"
                },
                {
                    "id": str(uuid.uuid4()),
                    "name": "CNBC News",
                    "url": "https://www.cnbc.com/world/?region=world",
                    "enabled": True,
                    "type": "webpage"
                }
            ]
        }
    
    def _save_sources(self, sources: Dict = None):
        """Save sources to JSON file."""
        try:
            data = sources if sources is not None else self.sources
            with open(self.sources_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving sources: {e}")
    
    def add_source(self, name: str, url: str, source_type: str = "webpage",
                   trust: Optional[str] = None) -> str:
        """
        Add a new news source.

        Args:
            name: Display name for the source
            url: URL of the news source (can be a Twitter/X URL, or a
                 subreddit - "r/stocks" or any reddit.com link into one)
            source_type: Type of source ('webpage', 'rss', 'twitter', 'reddit')
            trust: 'reporting' or 'opinion'; None for the type's default

        Returns:
            Source ID if successful

        Raises:
            ValueError: If URL or trust is invalid
        """
        if trust is not None and trust not in TRUST_LEVELS:
            raise ValueError(f"trust must be one of {', '.join(TRUST_LEVELS)}")
        # Validate and normalize URL
        url = url.strip()

        # Check if it's a Twitter/X URL and convert to Nitter
        if self._is_twitter_url(url):
            original_url = url
            url = self._convert_to_nitter(url)
            source_type = "twitter"
            print(f"Detected Twitter URL, converted to Nitter: {url}")
        elif source_type == "reddit" or reddit_source.is_reddit_url(url):
            subreddit = self._reddit_subreddit(url)
            url = reddit_source.subreddit_url(subreddit)
            source_type = "reddit"
            name = (name or "").strip() or f"r/{subreddit}"

        if not self._validate_url(url):
            raise ValueError(f"Invalid URL: {url}")
        
        source_id = str(uuid.uuid4())
        new_source = {
            "id": source_id,
            "name": name,
            "url": url,
            "enabled": True,  # New sources are enabled by default
            "type": source_type,
            "trust": trust or default_trust(source_type),
        }
        
        self.sources["sources"].append(new_source)
        self._save_sources()
        print(f"Added source: {name} ({url})")
        return source_id
    
    def remove_source(self, source_id: str) -> bool:
        """
        Remove a source by ID.
        
        Args:
            source_id: ID of source to remove
            
        Returns:
            True if removed, False if not found
        """
        initial_count = len(self.sources["sources"])
        self.sources["sources"] = [s for s in self.sources["sources"] if s["id"] != source_id]
        
        if len(self.sources["sources"]) < initial_count:
            self._save_sources()
            print(f"Removed source: {source_id}")
            return True
        return False
    
    def get_sources(self, enabled_only: bool = False) -> List[Dict]:
        """
        Get all sources or only enabled ones.
        
        Args:
            enabled_only: If True, return only enabled sources
            
        Returns:
            List of source dictionaries
        """
        sources = self.sources.get("sources", [])
        if enabled_only:
            return [s for s in sources if s.get("enabled", True)]
        return sources
    
    def update_source(self, source_id: str, **kwargs) -> bool:
        """
        Update source properties.
        
        Args:
            source_id: ID of source to update
            **kwargs: Properties to update (name, url, enabled, type, trust)

        Returns:
            True if updated, False if not found
        """
        for source in self.sources["sources"]:
            if source["id"] == source_id:
                for key, value in kwargs.items():
                    if key in ["name", "url", "enabled", "type", "trust"]:
                        source[key] = value
                self._save_sources()
                print(f"Updated source: {source_id}")
                return True
        return False
    
    def toggle_source(self, source_id: str) -> bool:
        """
        Toggle enabled/disabled status of a source.
        
        Args:
            source_id: ID of source to toggle
            
        Returns:
            New enabled status
        """
        for source in self.sources["sources"]:
            if source["id"] == source_id:
                source["enabled"] = not source.get("enabled", True)
                self._save_sources()
                return source["enabled"]
        return False

    def set_trust(self, source_id: str, trust: str) -> bool:
        """Mark a source as 'reporting' or 'opinion' (see TRUST_LEVELS).
        Raises ValueError for anything else; False if there's no such source."""
        if trust not in TRUST_LEVELS:
            raise ValueError(f"trust must be one of {', '.join(TRUST_LEVELS)}")
        return self.update_source(source_id, trust=trust)

    def _reddit_subreddit(self, url: str) -> str:
        """The subreddit a Reddit source points at, refusing links that are not
        to a subreddit and one that is already a source - two copies would
        split one request budget between them for nothing."""
        subreddit = reddit_source.parse_subreddit(url, bare_ok=True)
        if not subreddit:
            raise ValueError("Reddit sources must be a subreddit, e.g. r/wallstreetbets")
        for source in self.sources.get("sources", []):
            existing = reddit_source.parse_subreddit(source.get("url"), bare_ok=True)
            if source.get("type") == "reddit" and (existing or "").lower() == subreddit.lower():
                raise ValueError(f"r/{subreddit} is already a source")
        return subreddit

    def _is_twitter_url(self, url: str) -> bool:
        """Check if URL is a Twitter/X URL."""
        url_lower = url.lower()
        return ('twitter.com/' in url_lower or 'x.com/' in url_lower) and 'nitter' not in url_lower
    
    def _convert_to_nitter(self, twitter_url: str) -> str:
        """Convert Twitter/X URL to Nitter URL."""
        # List of Nitter instances (in order of preference)
        nitter_instances = [
            'nitter.poast.org',
            'nitter.privacydev.net',
            'nitter.net',
            'nitter.lunar.icu'
        ]
        
        # Extract username from Twitter URL
        # Handles: twitter.com/username, x.com/username, twitter.com/@username
        url = twitter_url.replace('https://', '').replace('http://', '')
        url = url.replace('twitter.com', '').replace('x.com', '')
        url = url.strip('/')
        
        # Remove @ if present
        if url.startswith('@'):
            url = url[1:]
        
        # Extract just the username (before any / or ?)
        username = url.split('/')[0].split('?')[0]
        
        # Use first Nitter instance (user can manually change if needed)
        nitter_url = f"https://{nitter_instances[0]}/{username}"
        
        return nitter_url
    
    def _validate_url(self, url: str) -> bool:
        """Validate URL format."""
        if not url:
            return False
        return url.startswith(('http://', 'https://'))
    
    def reset_to_defaults(self):
        """Reset sources to default set."""
        self.sources = self._get_default_sources()
        self._save_sources()
        print("Reset sources to defaults")
