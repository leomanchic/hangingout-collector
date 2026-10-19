"""Fair, stateless discovery: refresh the front page and rotate deeper pages."""
from datetime import datetime, timezone
from itertools import zip_longest


def page_window(pages=4, *, hour=None, depth=10):
    if not 1 <= pages <= depth <= 10:
        raise ValueError("Invalid discovery depth")
    if pages == 1:
        return [1]
    hour = int(datetime.now(timezone.utc).timestamp() // 3600) if hour is None else hour
    count = pages - 1
    bands = (depth - 1 + count - 1) // count
    start = 2 + (hour % bands) * count
    return [1, *range(start, min(start + count, depth + 1))]


def interleave(groups, *, key=lambda value: value, limit=None):
    """Every category/page receives turns; repeated ads cannot consume the budget."""
    seen, result = set(), []
    for row in zip_longest(*groups):
        for value in row:
            if value is None or key(value) in seen:
                continue
            seen.add(key(value))
            result.append(value)
            if limit is not None and len(result) >= limit:
                return result
    return result
