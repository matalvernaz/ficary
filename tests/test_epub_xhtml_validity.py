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
