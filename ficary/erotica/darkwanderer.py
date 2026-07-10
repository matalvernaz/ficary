"""Dark Wanderer (darkwanderer.net) scraper.

Dark Wanderer is a XenForo-based cuckold community. Stories are
forum threads at ``/threads/<slug>.<tid>/`` — the thread starter's
posts are the chapters; replies by other members are dropped. The
walk/filter logic lives in :class:`ficary.erotica.xenforo.
XenForoStoryScraper` (shared with Chastity Mansion and
TicklingForum); only the board constants live here.

Tagging the cuckold kink with a dedicated site gives it the same
two-archive footing as TG (Fictionmania + TGStorytime) and adds a
community-specific voice — the stories on Dark Wanderer are written
in a different register than the tagged Lushstories-and-SOL kind.
"""

import re

from .xenforo import XenForoStoryScraper

DW_BASE = "https://darkwanderer.net"

DW_THREAD_URL_RE = re.compile(
    r"^https?://(?:www\.)?darkwanderer\.net/threads/(?P<slug>[^/.]+)\.(?P<tid>\d+)",
    re.I,
)


class DarkWandererScraper(XenForoStoryScraper):
    """Scraper for darkwanderer.net forum threads treated as stories."""

    site_name = "darkwanderer"
    XF_BASE = DW_BASE
    THREAD_URL_RE = DW_THREAD_URL_RE
    XF_TITLE_SUFFIX_RE = re.compile(r"\s*\|\s*Darkwanderer.*$")
