"""Erotica-focused scrapers.

Kept in a subpackage — instead of alongside the general-purpose site
modules at ``ficary/*`` — so the erotica surface is a single visible
bucket in both the file tree and the import graph. Each module inside
follows the same interface as any other ``BaseScraper`` subclass and
is wired into :mod:`ficary.sites` the same way.

Listing one site per module (not a monolithic ``erotica.py``) matches
the existing convention — ``ao3.py``, ``literotica.py``, ``royalroad.py``
— and keeps per-site selectors/docs readable.

Sites covered: AFF (Adult-FanFiction.org), StoriesOnline (SOL), Nifty,
SexStories (xnxx), MCStories, Lushstories, Fictionmania, TGStorytime,
Chyoa, Dark Wanderer, GreatFeet, BDSM Library, The Mousepad (a
Tapatalk-hosted phpBB story forum, reached through its mobile XML-RPC
API — see :mod:`ficary.erotica.tapatalk`), ReadOnlyMind, Giantess
World (eFiction), Chastity Mansion, and TicklingForum (both XenForo —
see :mod:`ficary.erotica.xenforo` for the shared thread-as-story
engine). AO3 is folded into
the unified Erotic Story Search window's fan-out via an explicit-only
adapter in :mod:`ficary.erotica.search`; the underlying scraper is
:class:`ficary.ao3.AO3Scraper`. The unified Erotic Story Search window
(:mod:`ficary.gui_search`) fans out across all of them.

Sites considered and not included in this release:

* ASSTR / Kristen Archives — DNS offline as of 2024 takedown, no
  alternate host found.
* BigCloset TopShelf — Drupal install with TG-only focus; would
  duplicate Fictionmania + TGStorytime coverage.
* Standalone foot-fetish portals (feetstories.com, crazyfoot.com,
  thefoothunter.com) — all serve stub bodies (~114 bytes) or are DNS-
  dead; the live archive coverage for feet is Literotica /
  Lushstories / SOL / GreatFeet / BDSM Library.
"""

from .aff import AFFScraper
from .bdsmlibrary import BDSMLibraryScraper
from .chastitymansion import ChastityMansionScraper
from .chyoa import ChyoaScraper
from .darkwanderer import DarkWandererScraper
from .fictionmania import FictionmaniaScraper
from .giantessworld import GiantessWorldScraper
from .greatfeet import GreatFeetScraper
from .literotica import LiteroticaScraper
from .lushstories import LushStoriesScraper
from .mcstories import MCStoriesScraper
from .mousepad import MousepadScraper
from .nifty import NiftyScraper
from .readonlymind import ReadOnlyMindScraper
from .sexstories import SexStoriesScraper
from .storiesonline import StoriesOnlineScraper
from .tgstorytime import TGStorytimeScraper
from .ticklingforum import TicklingForumScraper

__all__ = [
    "AFFScraper",
    "BDSMLibraryScraper",
    "ChastityMansionScraper",
    "ChyoaScraper",
    "DarkWandererScraper",
    "FictionmaniaScraper",
    "GiantessWorldScraper",
    "GreatFeetScraper",
    "LiteroticaScraper",
    "LushStoriesScraper",
    "MCStoriesScraper",
    "MousepadScraper",
    "NiftyScraper",
    "ReadOnlyMindScraper",
    "SexStoriesScraper",
    "StoriesOnlineScraper",
    "TGStorytimeScraper",
    "TicklingForumScraper",
]
