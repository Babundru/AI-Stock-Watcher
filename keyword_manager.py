import json
import os
from typing import Dict

class KeywordManager:
    """Manages user-configurable keywords for sentiment analysis."""
    
    def __init__(self, keywords_file='data/keywords.json'):
        self.keywords_file = keywords_file
        self.keywords = self._load_keywords()
    
    def _load_keywords(self) -> Dict:
        """Load keywords from JSON file or create defaults."""
        if os.path.exists(self.keywords_file):
            try:
                with open(self.keywords_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading keywords: {e}")
                return self._get_default_keywords()
        else:
            # Create with defaults
            defaults = self._get_default_keywords()
            self._save_keywords(defaults)
            return defaults
    
    def _get_default_keywords(self) -> Dict:
        """Get default keyword sets with weights."""
        return {
            "positive_keywords": {
                # Mergers & Acquisitions (High Impact)
                "acquisition": 8,
                "merger": 8,
                "buyout": 7,
                "takeover": 7,
                "acquired": 8,
                "acquiring": 7,
                
                # Growth & Performance (Very High Impact)
                "breakthrough": 9,
                "record revenue": 9,
                "record profit": 9,
                "record earnings": 9,
                "profit surge": 8,
                "beats expectations": 8,
                "exceeds forecast": 7,
                "exceeds estimates": 7,
                "growth": 6,
                "expansion": 6,
                "revenue increase": 7,
                "double-digit growth": 8,
                "triple": 8,
                "soars": 7,
                "skyrockets": 8,
                
                # Product & Innovation
                "innovation": 7,
                "patent granted": 8,
                "patent approved": 8,
                "fda approval": 9,
                "fda cleared": 9,
                "approval": 7,
                "cleared": 7,
                "launched": 6,
                "new product": 6,
                "product release": 6,
                "game-changer": 8,
                "revolutionary": 8,
                "cutting-edge": 7,
                
                # Partnerships & Contracts (Medium-High Impact)
                "partnership": 6,
                "strategic alliance": 7,
                "collaboration": 6,
                "contract": 6,
                "deal": 5,
                "agreement": 5,
                "signed": 5,
                "wins contract": 7,
                "major deal": 7,
                "exclusive": 6,
                
                # Stock Movement & Ratings
                "shares jump": 7,
                "shares surge": 8,
                "stock surges": 8,
                "stock soars": 8,
                "rally": 6,
                "bullish": 6,
                "upgraded": 7,
                "buy rating": 7,
                "overweight": 7,
                "price target raised": 7,
                "analyst upgrade": 7,
                "institutional buying": 7,
                
                # Clinical/Research (Biotech/Pharma Specific)
                "positive results": 8,
                "clinical trial success": 9,
                "breakthrough therapy": 9,
                "accelerated approval": 9,
                "orphan drug": 7,
                "phase 3 success": 9,
                "positive data": 8,
                "efficacy": 7,
                "promising": 6,
                
                # Financial Events
                "dividend increase": 7,
                "dividend raised": 7,
                "share buyback": 7,
                "stock split": 6,
                "cash infusion": 7,
                "funding secured": 7,
                "investment": 6,
                "capital raise": 6,
                
                # Market Position
                "market leader": 7,
                "dominant": 7,
                "competitive advantage": 7,
                "outperform": 7,
                "market share gain": 7,
                "wins bid": 7,
                
                # General Positive
                "success": 5,
                "successful": 6,
                "strong": 5,
                "robust": 6,
                "boosts": 6,
                "momentum": 6,
                "optimistic": 6,
                "confident": 5,
                "turnaround": 7,
                "recovery": 6,
                "rebound": 6
            },
            "negative_keywords": {
                # Financial Distress (Critical)
                "bankruptcy": -10,
                "bankrupt": -10,
                "chapter 11": -10,
                "insolvent": -9,
                "insolvency": -9,
                "debt crisis": -8,
                "financial trouble": -7,
                "cash crunch": -7,
                "liquidity crisis": -8,
                "default": -9,
                "covenant breach": -7,
                "going concern": -9,
                
                # Losses & Declines
                "loss": -6,
                "losses": -6,
                "decline": -5,
                "drop": -5,
                "falls": -5,
                "misses estimates": -7,
                "revenue decline": -6,
                "profit decline": -6,
                "sales decline": -6,
                
                # Legal Issues (High Severity)
                "lawsuit": -7,
                "sued": -7,
                "litigation": -6,
                "investigation": -6,
                "probe": -6,
                "fraud": -9,
                "scandal": -8,
                "bribery": -8,
                "corruption": -8,
                "penalty": -6,
                "fine": -6,
                "settlement": -5,
                "guilty": -8,
                "charged": -7,
                "indicted": -8,
                "sec investigation": -8,
                "doj probe": -8,
                
                # Product Issues
                "recall": -8,
                "defect": -7,
                "defective": -7,
                "failure": -6,
                "rejected": -7,
                "denied": -6,
                "fda rejection": -9,
                "safety issue": -8,
                "contamination": -8,
                "side effects": -7,
                
                # Workforce & Operations
                "layoffs": -6,
                "cutting jobs": -6,
                "job cuts": -6,
                "restructuring": -5,
                "downsizing": -6,
                "plant closure": -7,
                "shutdown": -7,
                "suspended": -7,
                "halt": -6,
                
                # Stock Movement & Ratings
                "plunge": -7,
                "crash": -8,
                "tumble": -7,
                "plummets": -8,
                "tanks": -7,
                "downgrade": -7,
                "downgraded": -7,
                "sell rating": -7,
                "bearish": -6,
                "price target cut": -7,
                "analyst downgrade": -7,
                "short interest": -5,
                "insider selling": -6,
                
                # Performance (Underperformance)
                "misses expectations": -7,
                "falls short": -6,
                "disappointing": -6,
                "disappoints": -6,
                "weak": -5,
                "underperform": -6,
                "underperforms": -6,
                "slump": -6,
                "sluggish": -5,
                "stagnant": -5,
                
                # Clinical/Research (Pharma/Biotech)
                "trial failed": -9,
                "trial failure": -9,
                "negative results": -8,
                "failed endpoint": -8,
                "safety concerns": -8,
                "adverse events": -7,
                "discontinued": -7,
                "terminated": -7,
                
                # Market & Competition
                "market share loss": -7,
                "losing ground": -6,
                "competition": -4,
                "threat": -5,
                "challenged": -5,
                "pressure": -4,
                
                # Regulatory
                "regulatory setback": -8,
                "compliance issue": -7,
                "violation": -7,
                "warning letter": -7,
                "audit": -5,
                
                # General Negative
                "warning": -5,
                "concern": -4,
                "concerns": -4,
                "risk": -4,
                "risks": -4,
                "struggle": -5,
                "struggling": -5,
                "uncertainty": -5,
                "delay": -5,
                "delayed": -5,
                "setback": -6,
                "volatile": -5,
                "volatility": -5
            }
        }
    
    def _save_keywords(self, keywords: Dict = None):
        """Save keywords to JSON file."""
        try:
            data = keywords if keywords is not None else self.keywords
            with open(self.keywords_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving keywords: {e}")
    
    def add_keyword(self, keyword: str, weight: int, keyword_type: str = "positive") -> bool:
        """
        Add a new keyword.
        
        Args:
            keyword: The keyword to add
            weight: Weight/score for the keyword (positive for positive keywords, negative for negative)
            keyword_type: "positive" or "negative"
            
        Returns:
            True if added successfully
        """
        keyword = keyword.lower().strip()
        if not keyword:
            return False
        
        if keyword_type == "positive":
            self.keywords["positive_keywords"][keyword] = abs(weight)
        elif keyword_type == "negative":
            self.keywords["negative_keywords"][keyword] = -abs(weight)
        else:
            return False
        
        self._save_keywords()
        print(f"Added {keyword_type} keyword: {keyword} (weight: {weight})")
        return True
    
    def remove_keyword(self, keyword: str, keyword_type: str = None) -> bool:
        """
        Remove a keyword.
        
        Args:
            keyword: The keyword to remove
            keyword_type: "positive" or "negative", or None to check both
            
        Returns:
            True if removed
        """
        keyword = keyword.lower().strip()
        removed = False
        
        if keyword_type in ["positive", None]:
            if keyword in self.keywords["positive_keywords"]:
                del self.keywords["positive_keywords"][keyword]
                removed = True
        
        if keyword_type in ["negative", None]:
            if keyword in self.keywords["negative_keywords"]:
                del self.keywords["negative_keywords"][keyword]
                removed = True
        
        if removed:
            self._save_keywords()
            print(f"Removed keyword: {keyword}")
        
        return removed
    
    def update_keyword_weight(self, keyword: str, new_weight: int, keyword_type: str) -> bool:
        """
        Update the weight of an existing keyword.
        
        Args:
            keyword: The keyword to update
            new_weight: New weight value
            keyword_type: "positive" or "negative"
            
        Returns:
            True if updated
        """
        keyword = keyword.lower().strip()
        
        if keyword_type == "positive" and keyword in self.keywords["positive_keywords"]:
            self.keywords["positive_keywords"][keyword] = abs(new_weight)
            self._save_keywords()
            return True
        elif keyword_type == "negative" and keyword in self.keywords["negative_keywords"]:
            self.keywords["negative_keywords"][keyword] = -abs(new_weight)
            self._save_keywords()
            return True
        
        return False
    
    def get_positive_keywords(self) -> Dict[str, int]:
        """Get all positive keywords with their weights."""
        return self.keywords.get("positive_keywords", {})
    
    def get_negative_keywords(self) -> Dict[str, int]:
        """Get all negative keywords with their weights."""
        return self.keywords.get("negative_keywords", {})
    
    def get_all_keywords(self) -> Dict:
        """Get all keywords."""
        return self.keywords
    
    def reset_to_defaults(self):
        """Reset keywords to default set."""
        self.keywords = self._get_default_keywords()
        self._save_keywords()
        print("Reset keywords to defaults")
