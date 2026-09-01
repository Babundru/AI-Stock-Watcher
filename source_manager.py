import json
import os
import uuid
from typing import Dict, List, Optional

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
    
    def add_source(self, name: str, url: str, source_type: str = "webpage") -> str:
        """
        Add a new news source.
        
        Args:
            name: Display name for the source
            url: URL of the news source (can be Twitter/X URL)
            source_type: Type of source ('webpage', 'rss', 'twitter')
            
        Returns:
            Source ID if successful
            
        Raises:
            ValueError: If URL is invalid
        """
        # Validate and normalize URL
        url = url.strip()
        
        # Check if it's a Twitter/X URL and convert to Nitter
        if self._is_twitter_url(url):
            original_url = url
            url = self._convert_to_nitter(url)
            source_type = "twitter"
            print(f"Detected Twitter URL, converted to Nitter: {url}")
        
        if not self._validate_url(url):
            raise ValueError(f"Invalid URL: {url}")
        
        source_id = str(uuid.uuid4())
        new_source = {
            "id": source_id,
            "name": name,
            "url": url,
            "enabled": True,  # New sources are enabled by default
            "type": source_type
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
            **kwargs: Properties to update (name, url, enabled, type)
            
        Returns:
            True if updated, False if not found
        """
        for source in self.sources["sources"]:
            if source["id"] == source_id:
                for key, value in kwargs.items():
                    if key in ["name", "url", "enabled", "type"]:
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
