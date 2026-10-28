"""Parse uploaded strategy code without running it; tolerate pasted prose headers."""
from __future__ import annotations

import ast
import re
import warnings

MAX_HEADER_LINES = 30
PROSE = re.compile(r"[　-鿿＀-￯]|^(https?://|www\.)")


def parse(source):
    """Return (tree, skipped_lines) or (None, 0).

    Forum and blog copies often start with prose ("该策略由聚宽用户分享…"). When the
    file does not parse, blank leading prose lines one at a time, keeping line numbers,
    until it does. A broken line that is not prose is a real error and stops the search.
    """
    if not source or not source.strip():
        return None, 0
    lines = source.splitlines()
    for skipped in range(min(len(lines), MAX_HEADER_LINES) + 1):
        if skipped:
            line = lines[skipped - 1].strip()
            if not line:
                continue  # blanking an empty line changes nothing
            if not PROSE.search(line):
                return None, 0
        text = "\n" * skipped + "\n".join(lines[skipped:])
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        except (RecursionError, MemoryError):
            return None, 0
        return (tree, skipped) if tree.body else (None, 0)
    return None, 0
