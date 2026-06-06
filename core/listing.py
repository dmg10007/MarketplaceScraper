"""
Listing dataclass — structured representation of a single FB Marketplace item.
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class Listing:
    id: str                          # FB listing ID extracted from URL
    title: str
    listing_url: str
    search_id: int
    price: Optional[float] = None
    location: Optional[str] = None
    image_url: Optional[str] = None
    condition: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def price_display(self) -> str:
        if self.price is None:
            return "Price not listed"
        return f"${self.price:,.0f}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "listing_url": self.listing_url,
            "search_id": self.search_id,
            "price": self.price,
            "location": self.location,
            "image_url": self.image_url,
            "condition": self.condition,
            "timestamp": self.timestamp.isoformat(),
        }
