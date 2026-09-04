"""Line-level diffing for the code-comparison view (Phase 9a)."""
from __future__ import annotations

import difflib


def line_diff(original: str, migrated: str) -> "tuple[list[dict], dict]":
    """Aligned line-level diff between two sources.

    Returns ``(rows, summary)``. Each row is one aligned line pair:

    * ``same``    -- identical text on both sides (old/new line numbers set)
    * ``changed`` -- a replaced line, paired row-wise within the hunk
    * ``removed`` -- present only in the original (``new`` is None)
    * ``added``   -- present only in the migrated version (``old`` is None)

    Pairing inside a ``replace`` hunk is row-wise (first-with-first): a
    fully faithful alignment would need token-level diffing; row-wise
    keeps the view honest and simple (documented approximation).
    """
    old_lines = original.splitlines()
    new_lines = migrated.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    rows: "list[dict]" = []
    counts = {"same": 0, "changed": 0, "added": 0, "removed": 0}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append({
                    "type": "same",
                    "old": i1 + offset + 1,
                    "new": j1 + offset + 1,
                    "old_text": old_lines[i1 + offset],
                    "new_text": new_lines[j1 + offset],
                })
                counts["same"] += 1
        elif tag == "replace":
            paired = min(i2 - i1, j2 - j1)
            for offset in range(paired):
                rows.append({
                    "type": "changed",
                    "old": i1 + offset + 1,
                    "new": j1 + offset + 1,
                    "old_text": old_lines[i1 + offset],
                    "new_text": new_lines[j1 + offset],
                })
                counts["changed"] += 1
            for offset in range(paired, i2 - i1):
                rows.append({
                    "type": "removed",
                    "old": i1 + offset + 1,
                    "new": None,
                    "old_text": old_lines[i1 + offset],
                    "new_text": None,
                })
                counts["removed"] += 1
            for offset in range(paired, j2 - j1):
                rows.append({
                    "type": "added",
                    "old": None,
                    "new": j1 + offset + 1,
                    "old_text": None,
                    "new_text": new_lines[j1 + offset],
                })
                counts["added"] += 1
        elif tag == "delete":
            for offset in range(i1, i2):
                rows.append({
                    "type": "removed",
                    "old": offset + 1,
                    "new": None,
                    "old_text": old_lines[offset],
                    "new_text": None,
                })
                counts["removed"] += 1
        elif tag == "insert":
            for offset in range(j1, j2):
                rows.append({
                    "type": "added",
                    "old": None,
                    "new": offset + 1,
                    "old_text": None,
                    "new_text": new_lines[offset],
                })
                counts["added"] += 1
    summary = dict(counts)
    summary["old_lines"] = len(old_lines)
    summary["new_lines"] = len(new_lines)
    return rows, summary