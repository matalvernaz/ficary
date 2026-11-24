"""The words on screen say "AI" where they said "LLM"; the names scripts and saved settings rely on do not change.

A screen reader spells "LLM" out letter by letter, and it means nothing to anyone who does not know the term. The maintainer chose to say "AI" in the windows, the status pane, the command-line help and the manual, and to keep command-line options, saved-setting names and environment variables as they were, so nothing that already works breaks.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from ficary import attribution, prefs

PACKAGE = Path(__file__).resolve().parents[1] / "ficary"

# Names that are not words on screen: environment variables and the like.
_NAME_LIKE = re.compile(r"^[A-Z0-9_]+$")
# Product names that happen to contain the letters.
_PRODUCT_NAMES = ("vLLM",)


def visible_strings_saying_llm(package: Path = PACKAGE):
    """Every string literal in ``package`` that says "LLM" (or tags a line "[llm") and that a user can see: docstrings, debug-level log lines and names of environment variables are left out."""
    found = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

        def hidden(node):
            current = node
            while current in parents:
                parent = parents[current]
                if isinstance(parent, ast.Expr) and parent.value is current:
                    return True
                if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute) and parent.func.attr == "debug":
                    return True
                current = parent
            return False

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            text = node.value
            without_products = text
            for product in _PRODUCT_NAMES:
                without_products = without_products.replace(product, "")
            if "LLM" not in without_products and "[llm" not in without_products:
                continue
            if hidden(node) or _NAME_LIKE.match(text.strip()) or "FICARY_LLM" in text:
                continue
            found.append(f"{path.relative_to(package.parent)}:{node.lineno}: {text[:80]!r}")
    return found


class TestTheWords:
    def test_nothing_on_screen_says_llm(self):
        assert visible_strings_saying_llm() == []

    def test_the_speaker_detection_choice_is_called_ai(self):
        assert attribution.BACKENDS["llm"]["display"] == "AI (Ollama / OpenAI / Anthropic)"

    def test_a_refusal_without_a_readable_reason_names_the_ai_server(self):
        assert attribution.LLMRejected(503, "openai-compatible", "<html>").explain() == (
            "The AI server refused the request (HTTP 503)"
        )

    def test_the_note_check_lines_are_tagged_ai_notes(self):
        run = attribution.NoteCheckRun({"provider": "anthropic", "model": "m"})
        message = run.chapter_failed(attribution.LLMUnavailable("connection refused"), "Chapter 2")
        assert message.startswith("  [AI notes] Chapter 2: ")
        assert "skipping the AI check for the remaining chapters" in message

    def test_the_guard_itself_notices_the_old_word(self, tmp_path):
        """A string saying LLM in a label is found; the same word in a docstring or a debug line is not."""
        package = tmp_path / "ficary"
        package.mkdir()
        (package / "sample.py").write_text(
            '"""LLM in a docstring is fine."""\n'
            "import logging\n"
            "logger = logging.getLogger(__name__)\n"
            'logger.debug("LLM in a debug line is fine")\n'
            'LABEL = "Use LLM to catch notes"\n'
            'PRODUCT = "vLLM is a product name"\n'
            'ENV = "FICARY_LLM_TIMEOUT_S"\n',
            encoding="utf-8",
        )
        found = visible_strings_saying_llm(package)
        assert len(found) == 1
        assert "Use LLM to catch notes" in found[0]


class TestWhatKeepsItsName:
    def test_saved_settings_keep_their_names(self):
        assert (
            prefs.KEY_LLM_STRIP_NOTES, prefs.KEY_LLM_PROVIDER, prefs.KEY_LLM_MODEL,
            prefs.KEY_LLM_API_KEY, prefs.KEY_LLM_ENDPOINT, prefs.KEY_LLM_REQUEST_TIMEOUT_S,
        ) == (
            "llm_strip_notes", "llm_provider", "llm_model",
            "llm_api_key", "llm_endpoint", "llm_request_timeout_s",
        )

    def test_command_line_options_keep_their_names(self):
        from ficary.cli import _build_parser

        args = _build_parser().parse_args([
            "--attribution", "llm", "--llm-provider", "ollama", "--llm-model", "m",
            "--llm-api-key", "k", "--llm-endpoint", "http://localhost:11434",
            "--strip-notes", "--llm-strip-notes", "https://example.invalid/s/1",
        ])
        assert (args.attribution, args.llm_provider, args.llm_model, args.llm_api_key) == ("llm", "ollama", "m", "k")
        assert args.llm_strip_notes is True

    def test_the_environment_variable_keeps_its_name(self, monkeypatch):
        monkeypatch.setenv("FICARY_LLM_TIMEOUT_S", "42")
        assert attribution._llm_request_timeout_s() == 42


wx = pytest.importorskip("wx")


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="builds a real window (run under xvfb-run -a)")
class TestTheAddStoryWindow:
    @pytest.fixture
    def window(self, wx_app):
        from ficary.gui import MainFrame

        frame = MainFrame()
        yield frame
        frame._clip_timer.Stop()
        frame.Destroy()

    def test_the_labels(self, window):
        assert window.llm_strip_notes_ctrl.GetLabel() == "Use AI to catch missed A/N (s&lower)"
        assert window.llm_settings_main_btn.GetLabel() == "AI settin&gs…"
        assert window.llm_settings_btn.GetLabel() == "AI &settings..."

    def test_alt_l_still_reaches_the_checkbox_and_alt_g_is_new_and_free(self, window):
        letters = []
        for child in window._add_story_panel.GetChildren():
            label = child.GetLabel() if hasattr(child, "GetLabel") else ""
            if "&" in label:
                letters.append(label[label.index("&") + 1].lower())
        assert letters.count("l") == 1
        assert letters.count("g") == 1
