from __future__ import annotations

from collections import Counter


def build_completeness(categories: list[str], required: list[str]) -> dict:
    counts = Counter(categories)
    items = [
        {"category": category, "count": counts.get(category, 0), "present": counts.get(category, 0) > 0}
        for category in required
    ]
    present = sum(item["present"] for item in items)
    total = len(items)
    percent = round((present / total) * 100) if total else 100
    return {
        "percent": percent,
        "present": present,
        "total": total,
        "missing": [item["category"] for item in items if not item["present"]],
        "items": items,
    }
