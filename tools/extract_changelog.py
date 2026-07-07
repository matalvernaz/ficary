#!/usr/bin/env python3
"""Extract a single version's notes from CHANGELOG.md.

The release workflow runs this to fill the GitHub release *body* from the
curated CHANGELOG. That body is what the in-app "changes since your
version" changelog shows: ``self_update.fetch_changelog_since`` aggregates
release bodies, so a release created with an empty body renders blank in
the update dialog. Emitting the matching section here keeps the two in
sync without hand-copying notes at tag time.

Usage:
    extract_changelog.py <changelog_path> <version> [output_path]

``version`` accepts the release-tag form (``v2.9.0``) or the bare number
(``2.9.0``). Writes the section to ``output_path`` as UTF-8 when given,
else prints it to stdout. Exits 1 when no section matches, so a release
never silently ships empty notes.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# CHANGELOG headings are ``## X.Y.Z — <date>``. Anchor on the H2 and a
# 3-part version; the lookahead stops ``2.9.0`` matching ``2.9.0.1`` or a
# longer prefix.
_VERSION_HEADING = re.compile(r"^##\s+(\d+\.\d+\.\d+)(?=\s|$)")

_USAGE = "usage: extract_changelog.py <changelog_path> <version> [output_path]"


def extract_section(changelog: str, version: str) -> str | None:
    """Return the notes under the heading for ``version``, or None.

    The heading line itself is dropped (the release page already shows the
    tag) and surrounding blank lines are trimmed. The section runs until
    the next version heading or end of file.
    """
    version = version.lstrip("v")
    lines = changelog.splitlines()
    start = None
    for i, line in enumerate(lines):
        m = _VERSION_HEADING.match(line)
        if m and m.group(1) == version:
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if _VERSION_HEADING.match(lines[j]):
            end = j
            break
    return "\n".join(lines[start:end]).strip() or None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not 2 <= len(argv) <= 3:
        print(_USAGE, file=sys.stderr)
        return 2
    changelog_path, version = argv[0], argv[1]
    output_path = argv[2] if len(argv) == 3 else None

    section = extract_section(
        Path(changelog_path).read_text(encoding="utf-8"), version
    )
    if not section:
        print(f"No CHANGELOG section found for version {version!r}", file=sys.stderr)
        return 1

    if output_path:
        Path(output_path).write_text(section + "\n", encoding="utf-8")
    else:
        sys.stdout.write(section + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
