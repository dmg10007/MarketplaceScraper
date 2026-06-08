"""
Reset seen listings for one or all searches.

Usage:
  python scripts/reset_seen.py          # list searches
  python scripts/reset_seen.py 3        # reset seen for search ID 3
  python scripts/reset_seen.py all      # reset seen for ALL searches

After running, the next scan will treat every listing as new and send alerts.
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import init_db, get_all_searches, reset_seen_for_search


async def main() -> None:
    await init_db()
    searches = await get_all_searches()

    if len(sys.argv) < 2:
        print("\nConfigured searches:")
        for s in searches:
            print(f"  [{s['id']}] {s['name']}  ({s['keywords']})")
        print("\nUsage:")
        print("  python scripts/reset_seen.py <id>   # reset one search")
        print("  python scripts/reset_seen.py all    # reset all searches")
        return

    arg = sys.argv[1].lower()

    if arg == "all":
        total = 0
        for s in searches:
            deleted = await reset_seen_for_search(s["id"])
            print(f"  [{s['id']}] {s['name']} — cleared {deleted} seen entries")
            total += deleted
        print(f"\nDone. Cleared {total} total seen entries across all searches.")
    elif arg.isdigit():
        search_id = int(arg)
        match = next((s for s in searches if s["id"] == search_id), None)
        if not match:
            print(f"No search found with ID {search_id}.")
            sys.exit(1)
        deleted = await reset_seen_for_search(search_id)
        print(f"Cleared {deleted} seen entries for [{search_id}] '{match['name']}'.")
        print("Next scan will alert on all current listings for this search.")
    else:
        print(f"Unknown argument: {arg}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
