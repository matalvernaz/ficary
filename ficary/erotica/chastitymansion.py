"""The Chastity Mansion (chastitymansion.com) — member fiction.

XenForo board centred on chastity / orgasm denial / FLR; the Member
Fiction forum (node ``member-fiction.19``, ~800 threads) is the story
section. Thread = story, starter posts = chapters — all shared logic
in :class:`ficary.erotica.xenforo.XenForoStoryScraper`.

The board runs without XenForo's friendly URLs, so canonical thread
links take the ``/forums/index.php?threads/<slug>.<tid>/`` form; the
regex also accepts the rewritten ``/forums/threads/...`` shape in
case a pasted link uses it.
"""

import re

from .xenforo import XenForoStoryScraper

CM_BASE = "https://chastitymansion.com/forums"

CM_THREAD_URL_RE = re.compile(
    r"^https?://(?:www\.)?chastitymansion\.com/forums/"
    r"(?:index\.php\?)?threads/(?P<slug>[^/.]+)\.(?P<tid>\d+)",
    re.I,
)


class ChastityMansionScraper(XenForoStoryScraper):
    """Scraper for Chastity Mansion member-fiction threads."""

    site_name = "chastitymansion"
    XF_BASE = CM_BASE
    XF_THREAD_PATH = "index.php?threads/{ref}/"
    THREAD_URL_RE = CM_THREAD_URL_RE
    XF_TITLE_SUFFIX_RE = re.compile(r"\s*\|\s*The Chastity Mansion.*$", re.I)
