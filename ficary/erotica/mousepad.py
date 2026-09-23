"""The Mousepad (tapatalk.com/groups/themousepad) — foot-fetish story forum.

Unlike every other erotica adapter, the source is a *forum*, not a
story archive: a story is a phpBB topic, and its chapters are the
posts the topic's author made in that topic. Reader comments are
interleaved between chapters in the same thread, so the download path
filters to posts whose ``post_author_id`` equals the thread's
``topic_author_id`` and drops everything else.

Transport is the Tapatalk mobiquo XML-RPC API (see
:mod:`ficary.erotica.tapatalk` for why the HTML site is unusable),
which means this scraper never touches :meth:`BaseScraper._fetch`.
It still uses the base class's delay machinery between thread-window
calls and its meta/chapter caches so cache_doctor and the library
tooling see the same shapes as every other site.

The author-only filter still lets through the author's *own*
non-story posts. Live data says length alone can't sort those out —
observed threads have 138-word in-character interludes and 452-word
"thanks for reading" confessions — so the adapter labels rather than
deletes: a leading bold / ``# `` header line is lifted into the
chapter title (audible in the TOC, skippable by ear), and a post is
dropped only on two corroborating signals — it *opens* by quoting a
non-author post AND has fewer than :data:`SKIP_MAX_WORDS` of its own
words once quotes are stripped. That combination is a reply to a
commenter, nothing else. Dropped posts are logged and recorded in
``story.metadata["skipped_posts"]`` so nothing vanishes silently.
"""

import logging
import re
import warnings
from typing import Optional

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper
from .tapatalk import (
    MOUSEPAD_BASE,
    MOUSEPAD_GROUP,
    THREAD_WINDOW,
    TOPIC_WINDOW,
    decode_value,
    iso_datetime,
    mobiquo_call,
    topic_url,
)

logger = logging.getLogger(__name__)

MP_VIEWTOPIC_URL_RE = re.compile(
    r"tapatalk\.com/groups/" + MOUSEPAD_GROUP
    + r"/viewtopic\.php\?(?:[^#\s]*&)?t=(?P<id>\d+)",
    re.I,
)

# phpBB SEO permalinks: ``<slug>-t197281.html`` (``-s<offset>`` on
# paged views). Tapatalk renders these for every topic, so pasted
# links arrive in this shape at least as often as viewtopic.php.
MP_SLUG_URL_RE = re.compile(
    r"tapatalk\.com/groups/" + MOUSEPAD_GROUP
    + r"/[a-z0-9_-]+-t(?P<id>\d+)(?:-s\d+)?(?:\.html)?",
    re.I,
)

# Forum-section URL shapes. Tapatalk renders every section as a
# ``<slug>-f<id>/`` permalink (what the address bar shows while you
# browse) and still answers phpBB's own ``viewforum.php?f=<id>``.
MP_VIEWFORUM_URL_RE = re.compile(
    r"tapatalk\.com/groups/" + MOUSEPAD_GROUP
    + r"/viewforum\.php\?(?:[^#\s]*&)?f=(?P<id>\d+)",
    re.I,
)
MP_SLUG_FORUM_URL_RE = re.compile(
    r"tapatalk\.com/groups/" + MOUSEPAD_GROUP
    + r"/[a-z0-9_-]+-f(?P<id>\d+)(?:-s\d+)?(?:\.html)?/?$",
    re.I,
)
# The board's front page. Pasting it means "show me what's here", which
# is answered with the section list rather than a download.
MP_GROUP_ROOT_RE = re.compile(
    r"tapatalk\.com/groups/" + MOUSEPAD_GROUP + r"/?$",
    re.I,
)

# The board groups its sections under named categories; this is the one
# holding fiction. Matched by id first and by name second so a
# re-numbering upstream doesn't silently empty the section list.
STORY_CATEGORY_ID = "135"
STORY_CATEGORY_NAME = "stories"

# Used when ``get_forum`` is unreachable or returns a shape we don't
# recognise. Ids verified live 2026-09-23; the descriptions are the
# board's own. Without this a transient API failure would present an
# empty section list, which reads as "this board has no stories".
FALLBACK_STORY_FORUMS: tuple[tuple[str, str, str], ...] = (
    ("72", "Stories",
     "Share your foot stories here, fiction or non-fiction!"),
    ("94", "Story Requests",
     "Request old stories and post ideas for other writers to work on."),
    ("97", "Classic Story Library",
     "An archive of the most popular stories in MousePad history."),
    ("96", "Experiences and Anecdotes",
     "Quick accounts of things that have happened to you."),
)

SUMMARY_MAX_CHARS = 300

# Skip gate: an author post that opens by quoting someone else AND has
# fewer than this many of its own words is a reply to a commenter.
# Observed floor for story-shaped author posts is ~138 words; observed
# comment-replies run ~10-50.
SKIP_MAX_WORDS = 60

# A leading bold/# line longer than this is a bolded first sentence,
# not a chapter header — leave it in the body.
TITLE_MAX_CHARS = 80

# Tapatalk renders phpBB quotes as literal BBCode even in HTML mode:
# ``[quote uid=10796411 name="PretentiousOne" post=1312855]...[/quote]``
# (attribute set varies; ``uid=0`` appears for guest-rendered quotes).
QUOTE_BLOCK_RE = re.compile(
    r"\[quote\b([^\]]*)\]((?:(?!\[/?quote\b).)*)\[/quote\]",
    re.I | re.DOTALL,
)
QUOTE_LEAD_RE = re.compile(
    r"^(?:\s|<br\s*/?>)*\[quote\b([^\]]*)\]", re.I,
)
QUOTE_NAME_RE = re.compile(r'name="([^"]*)"', re.I)
QUOTE_UID_RE = re.compile(r"uid=(\d+)", re.I)

LEAD_TITLE_BOLD_RE = re.compile(
    r"^\s*<(b|strong)>(?P<t>[^<]{1,%d})</\1>\s*(?:<br\s*/?>\s*)+" % TITLE_MAX_CHARS,
    re.I,
)
LEAD_TITLE_HASH_RE = re.compile(
    r"^\s*#{1,6}\s*(?P<t>[^<\n]{1,%d}?)\s*(?:<br\s*/?>\s*)+" % TITLE_MAX_CHARS,
)
# Plain-text "Chapter 12" / "Part 3 - The Beach" opener lines (no bold,
# no hashes) — common on older threads. Lifting them keeps the author's
# own numbering visible when it doesn't match the post sequence (e.g. a
# thread whose Chapter 1 post was lost to a split).
LEAD_TITLE_CHAPTER_RE = re.compile(
    r"^\s*(?P<t>(?:Chapter|Part)\s+\d+[^<\n]{0,%d}?)\s*(?:<br\s*/?>\s*)+"
    % TITLE_MAX_CHARS,
    re.I,
)


def _render_quotes(html: str) -> str:
    """Convert BBCode quote blocks to attributed ``<blockquote>``
    markup so exports don't ship raw ``[quote]`` tags. Innermost-first
    replacement handles nested quotes."""
    def _one(m: re.Match) -> str:
        name_m = QUOTE_NAME_RE.search(m.group(1))
        attrib = (
            f"<p><em>{name_m.group(1)} wrote:</em></p>" if name_m else ""
        )
        return f"<blockquote>{attrib}{m.group(2)}</blockquote>"

    while QUOTE_BLOCK_RE.search(html):
        html = QUOTE_BLOCK_RE.sub(_one, html)
    return html


def _strip_quotes(html: str) -> str:
    """Remove quote blocks entirely — used to count a post's *own*
    words for the skip gate."""
    while QUOTE_BLOCK_RE.search(html):
        html = QUOTE_BLOCK_RE.sub("", html)
    return html


def _is_comment_reply(html: str, author_id: str, author_name: str) -> bool:
    """Two-signal gate for "author replying to a commenter".

    Signal 1: the post *opens* with a quote of someone who isn't the
    author (matched by uid when present and non-zero, else by name —
    the attribute set varies between quotes). An author re-quoting
    their own story text never trips this.
    Signal 2: under :data:`SKIP_MAX_WORDS` words once every quote
    block is stripped.
    """
    lead = QUOTE_LEAD_RE.match(html)
    if not lead:
        return False
    attrs = lead.group(1)
    uid_m = QUOTE_UID_RE.search(attrs)
    if uid_m and uid_m.group(1) not in ("", "0"):
        if uid_m.group(1) == author_id:
            return False
    else:
        name_m = QUOTE_NAME_RE.search(attrs)
        if name_m and name_m.group(1).strip().lower() == author_name.strip().lower():
            return False
    own_text = BeautifulSoup(
        _strip_quotes(html), "lxml",
    ).get_text(" ", strip=True)
    return len(own_text.split()) < SKIP_MAX_WORDS


def _lift_title(html: str) -> tuple[str, str]:
    """Pull a leading header line out as the chapter title.

    Mousepad authors head their posts with a whole-line bold title
    (``<b>Author's Confession</b>``) or a markdown-style ``# Title``
    line. Lifting it lets the export's TOC read "Chapter 30. Author's
    Confession" — the label a listener needs to skip a note — and the
    line is removed from the body so it doesn't render twice. Returns
    ``("", html)`` untouched when no header is found.
    """
    for pattern in (
        LEAD_TITLE_BOLD_RE, LEAD_TITLE_HASH_RE, LEAD_TITLE_CHAPTER_RE,
    ):
        m = pattern.match(html)
        if m:
            # Odd spacing like ``# ## Title`` leaves hash residue in
            # the capture — scrub it so titles read clean.
            return m.group("t").strip().lstrip("#").strip(), html[m.end():]
    return "", html


class MousepadScraper(BaseScraper):
    """Scraper for The Mousepad story forums."""

    site_name = "mousepad"

    @staticmethod
    def parse_story_id(url_or_id):
        """Return the topic id (as a string of digits)."""
        text = str(url_or_id).strip()
        for pattern in (MP_VIEWTOPIC_URL_RE, MP_SLUG_URL_RE):
            m = pattern.search(text)
            if m:
                return m.group("id")
        if re.fullmatch(r"\d+", text):
            return text
        raise ValueError(
            f"Cannot parse Mousepad topic id from: {text!r}\n"
            "Expected e.g. https://www.tapatalk.com/groups/themousepad/"
            "viewtopic.php?t=197281 or a bare topic id."
        )

    @classmethod
    def cache_key_for_url(cls, url_or_id):
        return int(cls.parse_story_id(url_or_id))

    # ── Forum sections ───────────────────────────────────────────

    @staticmethod
    def parse_forum_id(url_or_id) -> str:
        """Return the forum id in ``url_or_id``, or ``""``.

        ``""`` means "a section wasn't named" — the board's front page.
        Callers answer that with the section list rather than a
        download, so the empty string is a real answer here, not a
        failure.
        """
        text = str(url_or_id).strip()
        for pattern in (MP_VIEWFORUM_URL_RE, MP_SLUG_FORUM_URL_RE):
            m = pattern.search(text)
            if m:
                return m.group("id")
        return ""

    @staticmethod
    def is_forum_url(url) -> bool:
        """True for a section listing or the board's front page.

        A thread carries the section id in ``?f=`` alongside its own
        ``?t=``, so the topic patterns are tried first: without that,
        every story link followed out of a forum listing would open the
        section picker instead of downloading the story.
        """
        text = str(url).strip()
        for topic_pattern in (MP_VIEWTOPIC_URL_RE, MP_SLUG_URL_RE):
            if topic_pattern.search(text):
                return False
        return bool(
            MP_VIEWFORUM_URL_RE.search(text)
            or MP_SLUG_FORUM_URL_RE.search(text)
            or MP_GROUP_ROOT_RE.search(text)
        )

    @staticmethod
    def forum_url(forum_id) -> str:
        """Canonical section URL. The slug-free ``viewforum.php`` form
        survives a section rename, same reasoning as
        :func:`~ficary.erotica.tapatalk.topic_url`."""
        return f"{MOUSEPAD_BASE}/viewforum.php?f={int(str(forum_id))}"

    @classmethod
    def _walk_forum_tree(cls, nodes, wanted: bool = False) -> list[dict]:
        """Flatten the fiction part of ``get_forum``'s nested reply.

        ``wanted`` turns true once the walk enters the fiction category
        and stays true for everything below it, which is what picks up
        the sections nested one level deeper (Story Requests and the
        Classic Story Library both hang off Stories, not off the
        category). Categories themselves carry ``sub_only`` and hold no
        threads, so they're descended into but never listed.
        """
        out: list[dict] = []
        for node in nodes or []:
            fid = decode_value(node.get("forum_id"))
            name = decode_value(node.get("forum_name"))
            in_story_tree = wanted or (
                fid == STORY_CATEGORY_ID
                or name.strip().lower() == STORY_CATEGORY_NAME
            )
            is_category = str(node.get("sub_only")).lower() == "true"
            if in_story_tree and not is_category and fid:
                out.append({
                    "id": fid,
                    "name": name or f"Forum {fid}",
                    "description": decode_value(node.get("description")),
                })
            out.extend(cls._walk_forum_tree(node.get("child"), in_story_tree))
        return out

    @classmethod
    def story_forums(cls, *, with_counts: bool = True,
                     progress=None) -> list[dict]:
        """List the board's story sections, newest-activity order intact.

        Each dict is ``{"id", "name", "description", "topics", "url"}``.
        ``topics`` is ``None`` when the count wasn't asked for or the
        section didn't answer, so a display can tell "none" from
        "unknown" instead of printing a confident zero.

        Falls back to :data:`FALLBACK_STORY_FORUMS` if ``get_forum`` is
        unreachable — an empty list would read as "this board has no
        stories", which is a worse lie than a slightly stale one.
        """
        def report(line: str) -> None:
            logger.info("%s", line)
            if progress:
                progress(line)

        forums: list[dict] = []
        try:
            resp = mobiquo_call("get_forum")
            nodes = resp if isinstance(resp, list) else (resp.get("list") or [])
            forums = cls._walk_forum_tree(nodes)
        except Exception as exc:
            report(
                f"Couldn't read the forum list from the board ({exc}); "
                "using the sections known at build time."
            )
        if not forums:
            forums = [
                {"id": fid, "name": name, "description": desc}
                for fid, name, desc in FALLBACK_STORY_FORUMS
            ]
        for f in forums:
            f["url"] = cls.forum_url(f["id"])
            f["topics"] = None
        if not with_counts:
            return forums
        for f in forums:
            try:
                resp = mobiquo_call("get_topic", f["id"], 0, 0)
                f["topics"] = int(resp.get("total_topic_num") or 0)
            except Exception as exc:
                # Leave ``topics`` as None; the section is still
                # listed and still downloadable.
                logger.warning(
                    "Mousepad: no topic count for forum %s: %s", f["id"], exc,
                )
        return forums

    def scrape_forum_works(self, url, progress=None):
        """Return ``(forum_name, [work_dict, ...])`` for one section.

        Walks the section's topic listing in
        :data:`~ficary.erotica.tapatalk.TOPIC_WINDOW` chunks, newest
        activity first. The board's biggest section runs to thousands of
        threads, so each window is reported as it lands rather than
        leaving the caller in silence for a minute.

        A window that fails part-way through returns what was collected
        so far, saying plainly how much of the section that is — the
        alternative is throwing away a minute of listing, and a silent
        partial would be indistinguishable from a short section.
        """
        forum_id = self.parse_forum_id(url)
        if not forum_id:
            raise ValueError(
                f"{url} names the board, not one of its sections. "
                "Use MousepadScraper.story_forums() to list them."
            )

        def report(line: str) -> None:
            logger.info("%s", line)
            if progress:
                progress(line)

        works: list[dict] = []
        seen: set[str] = set()
        forum_name = f"Forum {forum_id}"
        total = 0
        start = 0
        while True:
            try:
                resp = mobiquo_call(
                    "get_topic", forum_id, start, start + TOPIC_WINDOW - 1,
                )
            except Exception as exc:
                report(
                    f"Listing stopped after {len(works)} of "
                    f"{total or 'an unknown number of'} threads in "
                    f"{forum_name}: {exc}"
                )
                break
            name = decode_value(resp.get("forum_name"))
            if name:
                forum_name = name
            # The server clamps an out-of-range offset to the listing's
            # tail rather than returning nothing, so walking past the
            # end re-serves the same rows forever. Bound it ourselves
            # against the reported total.
            total = int(resp.get("total_topic_num") or 0) or total
            if total and start >= total:
                break
            rows = resp.get("topics") or []
            if not rows:
                break
            for t in rows:
                topic_id = decode_value(t.get("topic_id"))
                title = decode_value(t.get("topic_title"))
                if not topic_id or not title or topic_id in seen:
                    continue
                seen.add(topic_id)
                works.append({
                    "title": title,
                    "author": decode_value(t.get("topic_author_name")),
                    "url": topic_url(topic_id),
                    "summary": decode_value(t.get("short_content")),
                    # Chapters are the author's posts, which can only be
                    # counted by opening the thread. ``reply_number``
                    # counts everyone's posts, so reporting it as a
                    # chapter count would be a wrong number, not a
                    # rough one.
                    "words": "", "chapters": "", "rating": "M",
                    "fandom": "", "status": "",
                    "section": forum_name,
                    "site": "mousepad",
                    "updated": iso_datetime(t.get("post_time")),
                })
            start += len(rows)
            report(
                f"  {forum_name}: listed {len(works)} of {total} threads"
            )
            if start >= total:
                break
            self._delay()
        return forum_name, works

    def _fetch_thread(self, topic_id: str) -> tuple[dict, list[dict]]:
        """Walk the thread's post windows and return
        ``(first_response, all_posts)``.

        Advances by however many posts each window actually returned
        (the server may cap below :data:`THREAD_WINDOW`); a window
        that returns nothing before ``total_post_num`` is reached ends
        the walk rather than looping forever.
        """
        first = mobiquo_call(
            "get_thread", topic_id, 0, THREAD_WINDOW - 1, True,
        )
        posts: list[dict] = list(first.get("posts") or [])
        total = int(first.get("total_post_num") or len(posts))
        while len(posts) < total:
            self._delay()
            window = mobiquo_call(
                "get_thread", topic_id,
                len(posts), len(posts) + THREAD_WINDOW - 1, True,
            )
            batch = window.get("posts") or []
            if not batch:
                logger.warning(
                    "Mousepad topic %s: server stopped at %d/%d posts",
                    topic_id, len(posts), total,
                )
                break
            posts.extend(batch)
        return first, posts

    # Another poster only overrides the topic author as "the story's
    # author" when they've written at least this many quote-stripped
    # words AND at least double the topic author's — conservative
    # enough that a chatty superfan can't hijack a real author's
    # thread, decisive enough for mangled threads.
    AUTHOR_OVERRIDE_MIN_WORDS = 100
    AUTHOR_OVERRIDE_FACTOR = 2

    @classmethod
    def _story_author_id(cls, thread: dict, posts: list[dict]) -> str:
        """Identify who is actually telling the story.

        Normally that's the topic author. But old threads that lost
        their original first post to a moderator split/merge can list
        a *commenter* as topic author — t=157450 is real: the API's
        post #1 is a reader's "great start!" reply and the story
        starts at post #2 under a different account, so filtering on
        the topic author exported 13 comments and none of the story.
        Whoever wrote the bulk of the thread's quote-stripped words is
        the storyteller; the override thresholds keep normal threads
        on the topic author.
        """
        totals: dict[str, int] = {}
        with warnings.catch_warnings():
            # A post whose quote-stripped remainder is a bare URL makes
            # bs4 emit MarkupResemblesLocatorWarning; it's parsing data,
            # not being pointed at a page — the warning is spurious.
            warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
            for p in posts:
                aid = decode_value(p.get("post_author_id"))
                text = BeautifulSoup(
                    _strip_quotes(decode_value(p.get("post_content"))),
                    "lxml",
                ).get_text(" ", strip=True)
                totals[aid] = totals.get(aid, 0) + len(text.split())
        topic_author = decode_value(thread.get("topic_author_id"))
        if not totals:
            return topic_author
        dominant = max(totals, key=totals.get)
        if dominant != topic_author:
            topic_words = totals.get(topic_author, 0)
            if (
                totals[dominant] >= cls.AUTHOR_OVERRIDE_MIN_WORDS
                and totals[dominant]
                >= cls.AUTHOR_OVERRIDE_FACTOR * max(topic_words, 1)
            ):
                logger.info(
                    "Mousepad: treating %s (%d words) as the story author "
                    "over topic author %s (%d words)",
                    dominant, totals[dominant], topic_author, topic_words,
                )
                return dominant
        return topic_author

    @classmethod
    def _author_posts(cls, thread: dict, posts: list[dict]) -> list[dict]:
        """Cut the thread down to the story: posts by the story's
        author (see :meth:`_story_author_id`).

        Everything else in the thread is reader comments. Matching on
        the stable ``post_author_id`` (not the display name) survives
        username changes.
        """
        author_id = cls._story_author_id(thread, posts)
        return [
            p for p in posts
            if decode_value(p.get("post_author_id")) == author_id
        ]

    @staticmethod
    def _summary_from_html(html: str) -> str:
        text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
        if len(text) <= SUMMARY_MAX_CHARS:
            return text
        return text[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"

    @classmethod
    def _prepare_chapters(
        cls, thread: dict, story_posts: list[dict],
    ) -> tuple[list[tuple[str, str]], list[dict]]:
        """Turn the author's posts into ``(title, html)`` chapters.

        Applies the comment-reply skip gate, lifts leading header
        lines into titles, and renders quote BBCode in what remains.
        Returns ``(chapters, skipped)`` where each skipped entry keeps
        enough of the post to identify it after the fact.
        """
        # The self-quote check keys on the STORY author (who may not be
        # the topic author on mangled threads) — take identity from the
        # already-filtered posts.
        author_id = (
            decode_value(story_posts[0].get("post_author_id"))
            if story_posts
            else decode_value(thread.get("topic_author_id"))
        )
        author_name = (
            decode_value(story_posts[0].get("post_author_name"))
            if story_posts
            else decode_value(thread.get("topic_author_name"))
        )
        chapters: list[tuple[str, str]] = []
        skipped: list[dict] = []
        for p in story_posts:
            raw = decode_value(p.get("post_content"))
            if _is_comment_reply(raw, author_id, author_name):
                preview = BeautifulSoup(
                    _strip_quotes(raw), "lxml",
                ).get_text(" ", strip=True)[:80]
                logger.info(
                    "Mousepad: skipping author comment-reply post %s (%r)",
                    decode_value(p.get("post_id")), preview,
                )
                skipped.append({
                    "post_id": decode_value(p.get("post_id")),
                    "preview": preview,
                })
                continue
            title, body = _lift_title(raw)
            chapters.append((title, _render_quotes(body)))
        return chapters, skipped

    def get_chapter_count(self, url_or_id):
        topic_id = self.parse_story_id(url_or_id)
        thread, posts = self._fetch_thread(topic_id)
        chapters, _ = self._prepare_chapters(
            thread, self._author_posts(thread, posts),
        )
        return len(chapters)

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        topic_id = self.parse_story_id(url_or_id)
        story_id = int(topic_id)

        logger.info("Fetching Mousepad topic %s...", topic_id)
        thread, posts = self._fetch_thread(topic_id)
        story_posts = self._author_posts(thread, posts)
        prepared, skipped = self._prepare_chapters(thread, story_posts)
        if not prepared:
            raise ValueError(
                f"Mousepad topic {topic_id} has no story posts by its "
                "author — nothing to download."
            )

        title = decode_value(thread.get("topic_title")) or f"Topic {topic_id}"
        author = (
            decode_value(story_posts[0].get("post_author_name"))
            or decode_value(thread.get("topic_author_name"))
            or "Unknown"
        )
        num_chapters = len(prepared)

        meta = {
            "title": title,
            "author": author,
            "author_url": "",
            "summary": self._summary_from_html(prepared[0][1]),
            "num_chapters": num_chapters,
            "chapter_titles": {
                str(n): t for n, (t, _) in enumerate(prepared, 1) if t
            },
            "extra": {
                "topic_id": topic_id,
                "forum": decode_value(thread.get("forum_name")),
                "updated": iso_datetime(story_posts[-1].get("post_time")),
                "total_posts": len(posts),
                "skipped_posts": skipped,
            },
        }
        self._save_meta_cache(story_id, meta)

        story = Story(
            id=story_id,
            title=title,
            author=author,
            summary=meta["summary"],
            url=topic_url(topic_id),
            metadata=meta["extra"],
        )

        if skip_chapters >= num_chapters:
            return story

        for i, (ch_title, ch_html) in enumerate(prepared, 1):
            if i <= skip_chapters:
                continue
            if not chapter_in_spec(i, chapters):
                continue
            ch = Chapter(number=i, title=ch_title, html=ch_html)
            self._save_chapter_cache(story_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(i, num_chapters, ch.title, False)

        return story
