# SPDX-License-Identifier: MIT
"""Split messages without dropping Unicode text or exceeding encoded payload limits."""
import json


def split_text(text, payload, limit):
    if not isinstance(text, str) or not text.strip() or not callable(payload) or type(limit) is not int:
        raise ValueError("invalid batch configuration")
    def size(value):
        return len(json.dumps(payload(value), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size(text) <= limit:
        return (text,)
    # Reserve enough space for the largest permitted part label on every part.
    prefix = "[part 999/999]\n"
    parts = []
    start = 0
    while start < len(text):
        low, high = start, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if size(prefix + text[start:mid]) <= limit:
                low = mid
            else:
                high = mid - 1
        if low == start or len(parts) >= 999:
            raise ValueError("message cannot be split")
        parts.append(text[start:low])
        start = low
    return tuple(f"[part {index}/{len(parts)}]\n{part}" for index, part in enumerate(parts, 1))
