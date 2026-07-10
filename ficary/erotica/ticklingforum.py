"""TicklingForum / TMF (ticklingforum.com) — tickling-flavoured
feet stories.

XenForo board; the story sections are ``tickling-stories.12`` (main)
plus per-author archive subforums. Thread = story, starter posts =
chapters — all shared logic in
:class:`ficary.erotica.xenforo.XenForoStoryScraper`.
"""

import re

from .xenforo import XenForoStoryScraper

TMF_BASE = "https://www.ticklingforum.com"

TMF_THREAD_URL_RE = re.compile(
    r"^https?://(?:www\.)?ticklingforum\.com/"
    r"(?:index\.php\?)?threads/(?P<slug>[^/.]+)\.(?P<tid>\d+)",
    re.I,
)


class TicklingForumScraper(XenForoStoryScraper):
    """Scraper for TicklingForum story threads."""

    site_name = "ticklingforum"
    XF_BASE = TMF_BASE
    THREAD_URL_RE = TMF_THREAD_URL_RE
    XF_TITLE_SUFFIX_RE = re.compile(
        r"\s*\|\s*(?:The\s+)?(?:TMF|Tickl).*$", re.I,
    )
