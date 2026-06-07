"""
Add a search to the database.

Usage:
    python scripts/add_search.py

Edit the search parameters below, then run the script.
You do NOT need main.py running first.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.database import init_db, create_search, get_all_searches


# ---------------------------------------------------------------------------
# ✏️  Edit your search here
# ---------------------------------------------------------------------------

SEARCH = dict(
    name        = "Road Bikes",
    keywords    = "bike",
    zip_code    = "27506",
    neg_keywords= "exercise,stationary,peloton,kids,tricycle",
    price_min   = 50,
    price_max   = 400,
    distance_mi = 30,
    condition   = "any",   # any | new | used | used_good | used_like_new | used_fair
)

# ---------------------------------------------------------------------------

async def main() -> None:
    # Ensure tables exist
    await init_db()

    search_id = await create_search(**SEARCH)
    print(f"\n  ✅ Search created with ID: {search_id}")
    print(f"     Name     : {SEARCH['name']}")
    print(f"     Keywords : {SEARCH['keywords']}")
    print(f"     Zip      : {SEARCH['zip_code']}")
    print(f"     Price    : ${SEARCH['price_min']}–${SEARCH['price_max']}")
    print(f"     Distance : {SEARCH['distance_mi']} miles")
    print(f"     Exclude  : {SEARCH['neg_keywords']}")

    all_searches = await get_all_searches()
    print(f"\n  Total searches in DB: {len(all_searches)}")
    for s in all_searches:
        icon = "✅" if s["enabled"] else "❌"
        print(f"  {icon} [{s['id']}] {s['name']} — {s['keywords']}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
