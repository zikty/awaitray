"""Text from GitHub and other services, made safe to show.

Titles, logins, check and incident names are written by whoever posts them.
`clean` drops control and format characters (bidi overrides, zero-width
marks), which could reorder or hide what a menu line or a notification
says, and optionally caps the length.
"""

from __future__ import annotations

import unicodedata

_DROP = {"Cc", "Cf", "Zl", "Zp"}


def clean(text: object, limit: int = 0) -> str:
    """`text` as one plain line: whitespace controls become spaces, other
    control and format characters go. With `limit`, at most that many
    characters, the last one an ellipsis."""
    out = "".join(
        " " if ch in "\t\n\r" else ch
        for ch in str(text)
        if ch in "\t\n\r" or unicodedata.category(ch) not in _DROP
    ).strip()
    if limit and len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out
