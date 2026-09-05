from collections import Counter


def build_completeness(categories: list[str], required: list[str]) -> dict:
    """Presence of categories only; never certifies document completeness or construction progress."""
    counts = Counter(categories)
    items = [{"category": c, "count": counts[c], "present": counts[c] > 0} for c in dict.fromkeys(required)]
    present = sum(i["present"] for i in items)
    total = len(items)
    return {"percent": round(present / total * 100) if total else 0,
            "present": present, "total": total,
            "missing": [i["category"] for i in items if not i["present"]],
            "items": items, "metric": "category_presence_not_document_completeness"}
