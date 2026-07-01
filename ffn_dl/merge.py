"""Combine a series of works into one Story for single-file export.

Split out from ``cli.py`` so both the CLI (``--merge-series``) and the
GUI series-merge path share one implementation — the GUI no longer has
to import the argparse-heavy ``cli`` module just to merge a series.
"""

from html import escape
from urllib.parse import urlsplit

from .models import Chapter, Story

# Human display names for the sites we merge, matched against a work's
# URL host. Labelling per-work links and the merged category from the
# actual source keeps a merged Literotica (or Royal Road, …) book from
# claiming every part is "Original on AO3". Unmapped hosts fall back to
# the second-level domain label, title-cased, so an unknown source still
# reads sensibly instead of leaking a bare hostname.
_HOST_DISPLAY = (
    ("archiveofourown.org", "AO3"),
    ("literotica.com", "Literotica"),
    ("royalroad.com", "Royal Road"),
    ("fanfiction.net", "FanFiction.net"),
    ("wattpad.com", "Wattpad"),
    ("webnovel.com", "Webnovel"),
)


def source_display_name(url: str) -> str:
    """Human label for the site ``url`` belongs to, for merged-book text."""
    host = urlsplit(url or "").netloc.lower()
    for fragment, name in _HOST_DISPLAY:
        if fragment in host:
            return name
    if host.startswith("www."):
        host = host[4:]
    label = host.split(".")[0]
    return label.title() if label else "the original source"


def merge_stories(series_name: str, series_url: str, stories: list) -> Story:
    """Combine a series of Story objects into one Story for single-file export.

    The merged Story gets a computed title (the series name), a
    combined author (single author if all works share one, otherwise
    comma-joined), and a per-work summary block. Each source work
    becomes a title chapter followed by its own chapters, preserving
    chapter numbering across the merged document so exporters can
    render a proper table of contents.

    Per-work "Original on <site>" links and the merged work's category
    are labelled from each work's own URL host, so the merged book names
    the real source instead of a hardcoded site.
    """
    authors = []
    for s in stories:
        if s.author and s.author not in authors:
            authors.append(s.author)
    combined_author = authors[0] if len(authors) == 1 else ", ".join(authors)

    summaries = []
    for s in stories:
        if s.summary:
            summaries.append(
                f"<strong>{escape(s.title)}</strong>: {escape(s.summary)}"
            )
    combined_summary = "\n".join(summaries) or "A series of works."

    total_words = 0
    for s in stories:
        w = s.metadata.get("words", "").replace(",", "").strip()
        if w.isdigit():
            total_words += int(w)

    all_complete = all(
        s.metadata.get("status", "").lower() == "complete" for s in stories
    )

    # Series parts always share one host, so the first work's URL names
    # the merged work's source; fall back to the series URL if empty.
    merged_label = source_display_name(stories[0].url if stories else series_url)

    merged = Story(
        id=0,
        title=series_name,
        author=combined_author or "Various",
        summary=combined_summary,
        url=series_url,
    )
    if total_words:
        merged.metadata["words"] = f"{total_words:,}"
    merged.metadata["status"] = "Complete" if all_complete else "In-Progress"
    merged.metadata["category"] = f"{merged_label} series"

    ch_num = 1
    for s in stories:
        header_html = (
            f"<h1>{escape(s.title)}</h1>"
            f"<p><em>by {escape(s.author)}</em></p>"
        )
        if s.summary:
            header_html += f"<blockquote>{escape(s.summary)}</blockquote>"
        if s.url:
            link_label = source_display_name(s.url)
            header_html += (
                f'<p><a href="{escape(s.url)}">'
                f"Original on {escape(link_label)}</a></p>"
            )
        merged.chapters.append(
            Chapter(number=ch_num, title=s.title, html=header_html)
        )
        ch_num += 1
        for ch in s.chapters:
            merged.chapters.append(
                Chapter(number=ch_num, title=ch.title, html=ch.html)
            )
            ch_num += 1

    return merged
