"""Round-10 H2: EPUB chapters are well-formed, EPUB3-clean XHTML.

Two tiers: an always-on structural check (strict XML parse + a denylist
of the obsolete elements/attributes epubcheck rejects), and an optional
epubcheck gate that runs only when the tool is on PATH.
"""
import shutil
import subprocess
import zipfile

import pytest

from ficary.exporters import _xhtml_sanitize, export_epub
from ficary.models import Chapter, Story

pytest.importorskip("ebooklib")
from lxml import etree  # noqa: E402  (after importorskip)

XHTML_NS = "http://www.w3.org/1999/xhtml"

# The elements/attributes epubcheck rejects (RSC-005) that scraped
# fanfic carries and _xhtml_sanitize is meant to remove.
_BANNED_TAGS = ("center", "font", "strike", "big", "tt", "u")
_BANNED_ATTRS = ("align", "bgcolor", "size", "noshade", "width", "color", "face")

_NASTY_HTML = (
    '<center>Centred by tag.</center>'
    '<p align="center">Centred by attribute.</p>'
    '<font color="red" size="4">Red and large.</font>'
    '<p>Ampersand &amp; raw & bare, and <b>bold</b>.</p>'
    '<hr size="2" noshade width="80%">'
    '<strike>struck</strike> <big>big</big> <tt>mono</tt>'
    '<p>Astral: \U0001F600 and high unicode: ☃.</p>'
    '<img src="https://example.com/remote.jpg" alt="a remote pic">'
)


def _nasty_story():
    s = Story(id=7, title="Nasty & Markup", author="A", summary="",
              url="https://www.fanfiction.net/s/7")
    s.chapters = [
        Chapter(number=1, title="One", html=_NASTY_HTML),
        Chapter(number=2, title="Two", html="<p>Clean chapter.</p>"),
    ]
    return s


class TestSanitizeUnit:
    def test_obsolete_tags_converted(self):
        out = _xhtml_sanitize('<center>x</center><font color="red">y</font>')
        assert "<center" not in out and "<font" not in out
        assert "x" in out and "y" in out  # text preserved

    def test_align_becomes_class(self):
        out = _xhtml_sanitize('<p align="center">hi</p>')
        assert "align=" not in out
        assert "center" in out

    def test_remote_img_becomes_placeholder(self):
        out = _xhtml_sanitize('<img src="https://x/y.jpg" alt="pic">')
        assert "<img" not in out
        assert "[image: pic]" in out

    def test_never_empties_nonempty(self):
        out = _xhtml_sanitize("<p>real text</p>")
        assert "real text" in out

    def test_empty_input_stays_empty(self):
        assert _xhtml_sanitize("").strip() == ""


class TestEpubXhtmlValidity:
    def test_chapters_are_well_formed_and_clean(self, tmp_path):
        epub_path = export_epub(_nasty_story(), output_dir=str(tmp_path))
        parser = etree.XMLParser(resolve_entities=False, recover=False)
        checked = 0
        with zipfile.ZipFile(epub_path) as zf:
            for name in zf.namelist():
                if not name.endswith(".xhtml"):
                    continue
                raw = zf.read(name)
                # Strict parse: raises on any malformedness (bare &,
                # unclosed void tags, mis-nesting).
                tree = etree.fromstring(raw, parser)
                # Every chapter/title doc is in the XHTML namespace.
                assert tree.tag.startswith(f"{{{XHTML_NS}}}")
                lowered = raw.lower()
                for tag in _BANNED_TAGS:
                    assert f"<{tag}".encode() not in lowered, (
                        f"{tag} survived in {name}")
                for attr in _BANNED_ATTRS:
                    assert f"{attr}=".encode() not in lowered, (
                        f"{attr}= survived in {name}")
                checked += 1
        assert checked >= 2  # both chapters + title page

    def test_no_remote_images_in_package(self, tmp_path):
        epub_path = export_epub(_nasty_story(), output_dir=str(tmp_path))
        with zipfile.ZipFile(epub_path) as zf:
            for name in zf.namelist():
                if name.endswith(".xhtml"):
                    body = zf.read(name).decode("utf-8", "replace")
                    assert "https://example.com/remote.jpg" not in body


@pytest.mark.skipif(
    shutil.which("epubcheck") is None,
    reason="epubcheck not on PATH (optional deeper gate)",
)
class TestEpubcheckGate:
    def test_zero_errors_or_warnings(self, tmp_path):
        import json
        epub_path = export_epub(_nasty_story(), output_dir=str(tmp_path))
        proc = subprocess.run(
            ["epubcheck", "--json", "-", str(epub_path)],
            capture_output=True, text=True,
        )
        # epubcheck 4.2.x prints a couple of human-readable lines
        # ("No errors or warnings detected.", "Epub Name: ...") to
        # stdout before the JSON document — slice from the first brace.
        brace = proc.stdout.find("{")
        assert brace != -1, (
            f"epubcheck produced no JSON (rc={proc.returncode}):\n"
            f"{proc.stdout[:400]}\n{proc.stderr[:400]}"
        )
        report = json.loads(proc.stdout[brace:])
        messages = report.get("messages", [])
        bad = [m for m in messages
               if m.get("severity") in ("ERROR", "WARNING", "FATAL")]
        assert not bad, "epubcheck flagged: " + "; ".join(
            f"{m.get('severity')} {m.get('ID')} {m.get('message', '')[:80]}"
            for m in bad
        )


# ── Links, ids and code (2026-11-24) ─────────────────────────────
# Each of these made the whole EPUB invalid; Amazon's Send to Kindle
# refuses invalid EPUBs. The author's text survives every one.

_LINK_TROUBLE = (
    '<p>Before<script>document.write("x")</script> after.</p>'
    '<noscript>Turn scripts on.</noscript>'
    '<p onclick="steal()" onmouseover="x()">Clickable paragraph.</p>'
    '<p>Pasted from Word.<a name="_GoBack" rel="nofollow"></a></p>'
    '<p>See note<a href="#fn1">1</a>, and the <a href="#chapter_1_endnotes">end notes</a>.</p>'
    '<p><a name="fn1"></a>The footnote itself.</p>'
    '<p><a name="old-anchor">Named text</a> stays as text.</p>'
    '<p id="twice">First.</p><p id="twice">Second.</p>'
    '<p><a href="https://example.invalid/elsewhere" target="_blank">An outside link</a>.</p>'
)


class TestLinksIdsAndCode:
    def tidy(self, html: str) -> str:
        return _xhtml_sanitize(html)

    def test_scripts_go_and_the_text_around_them_stays(self):
        out = self.tidy(_LINK_TROUBLE)
        assert "<script" not in out and "document.write" not in out
        assert "<noscript" not in out
        assert "Before" in out and "after." in out

    def test_event_attributes_go(self):
        out = self.tidy(_LINK_TROUBLE)
        assert "onclick" not in out and "onmouseover" not in out
        assert "Clickable paragraph." in out

    def test_a_word_bookmark_goes(self):
        out = self.tidy('<p>Pasted from Word.<a name="_GoBack" rel="nofollow"></a></p>')
        assert out == "<p>Pasted from Word.</p>"

    def test_a_footnote_keeps_its_link_and_its_target(self):
        out = self.tidy(_LINK_TROUBLE)
        assert '<a href="#fn1">1</a>' in out
        assert '<a id="fn1"></a>' in out
        assert "The footnote itself." in out

    def test_a_link_to_something_not_in_the_chapter_keeps_its_text(self):
        out = self.tidy(_LINK_TROUBLE)
        assert "#chapter_1_endnotes" not in out
        assert "end notes" in out

    def test_a_named_anchor_with_text_keeps_the_text(self):
        out = self.tidy(_LINK_TROUBLE)
        assert "old-anchor" not in out
        assert "<p>Named text stays as text.</p>" in out

    def test_an_id_used_twice_keeps_its_first_use(self):
        out = self.tidy(_LINK_TROUBLE)
        assert out.count('id="twice"') == 1
        assert "First." in out and "Second." in out

    def test_an_outside_link_is_left_alone(self):
        out = self.tidy(_LINK_TROUBLE)
        assert '<a href="https://example.invalid/elsewhere" target="_blank">An outside link</a>' in out


def _link_trouble_story():
    story = Story(id=8, title="Links & Code", author="A", summary="", url="https://www.fanfiction.net/s/8")
    story.chapters.append(Chapter(number=1, title="One", html=_LINK_TROUBLE))
    return story


def _ao3_fixture_story():
    """The AO3 work in the fixtures: its first chapter has a Word bookmark."""
    from pathlib import Path

    from bs4 import BeautifulSoup

    from ficary.ao3 import AO3Scraper

    page = (Path(__file__).parent / "fixtures" / "ao3_work_full.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(page, "lxml")
    meta = AO3Scraper._parse_metadata(soup)
    story = Story(id=9, title=meta["title"], author=meta["author"], summary="", url="https://archiveofourown.org/works/9")
    story.chapters.extend(AO3Scraper._parse_chapters(soup, meta["title"]))
    return story


def _epubcheck_problems(epub_path) -> list[str]:
    import json

    proc = subprocess.run(["epubcheck", "--json", "-", str(epub_path)], capture_output=True, text=True, timeout=300)
    brace = proc.stdout.find("{")
    assert brace != -1, f"epubcheck produced no JSON (rc={proc.returncode}): {proc.stdout[:300]} {proc.stderr[:300]}"
    report = json.loads(proc.stdout[brace:])
    return [
        f"{m.get('severity')} {m.get('ID')} {m.get('message', '')[:100]}"
        for m in report.get("messages", [])
        if m.get("severity") in ("ERROR", "WARNING", "FATAL")
    ]


@pytest.mark.skipif(shutil.which("epubcheck") is None, reason="epubcheck not on PATH (optional deeper gate)")
class TestEpubcheckOnLinksAndRealWork:
    def test_links_ids_and_code_pass(self, tmp_path):
        assert _epubcheck_problems(export_epub(_link_trouble_story(), output_dir=str(tmp_path))) == []

    def test_the_real_ao3_work_with_a_word_bookmark_passes(self, tmp_path):
        assert _epubcheck_problems(export_epub(_ao3_fixture_story(), output_dir=str(tmp_path))) == []


class TestLinksToTheStorySite:
    """SOL wraps the cover in a link back to the story page,
    ``/s/40467/...#cont``: relative to the site, so it points at nothing
    in the EPUB (epubcheck RSC-007)."""

    def test_a_link_relative_to_the_site_keeps_its_text(self):
        out = _xhtml_sanitize('<p><a href="/s/40467/ouroboros-dorm-dipping#cont">Cover</a> then text.</p>')
        assert out == "<p>Cover then text.</p>"

    def test_a_javascript_link_keeps_its_text(self):
        out = _xhtml_sanitize('<p><a href="javascript:report(1)">Report</a></p>')
        assert out == "<p>Report</p>"

    def test_a_protocol_relative_link_becomes_https(self):
        out = _xhtml_sanitize('<p><a href="//example.invalid/x">there</a></p>')
        assert '<a href="https://example.invalid/x">there</a>' in out

    @pytest.mark.parametrize("address", ["https://example.invalid/x", "http://example.invalid/y", "mailto:someone@example.invalid"])
    def test_web_and_mail_links_stay(self, address):
        out = _xhtml_sanitize(f'<p><a href="{address}">here</a></p>')
        assert f'<a href="{address}">here</a>' in out
