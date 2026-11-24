"""The test suite never reaches a computer other than this one.

``no_network`` in conftest.py refuses every such attempt and fails the
test that made it; ``fixed_voice_list`` gives the audiobook renders the
Microsoft voice list without asking Microsoft. These tests check both,
since a guard that quietly stopped guarding would look exactly like a
suite that never needed it.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


class TestTheGuard:
    def test_a_socket_to_another_computer_is_refused(self, no_network):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with pytest.raises(ConnectionRefusedError, match="tests may not reach"):
                sock.connect(("192.0.2.1", 443))
        assert no_network == ["192.0.2.1:443"]
        no_network.clear()

    def test_looking_up_a_name_is_refused(self, no_network):
        with pytest.raises(ConnectionRefusedError):
            socket.getaddrinfo("example.com", 443)
        assert no_network == ["example.com"]
        no_network.clear()

    def test_curl_cffi_is_refused_before_it_sends_anything(self, no_network):
        curl_requests = pytest.importorskip("curl_cffi.requests")
        with pytest.raises(ConnectionRefusedError):
            curl_requests.Session().get("https://example.com/")
        assert no_network == ["https://example.com/"]
        no_network.clear()

    def test_this_computer_is_allowed(self, no_network):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=5):
                pass
        assert no_network == []

    def test_a_test_that_reaches_out_fails(self, tmp_path):
        """Run a small suite with this conftest in a separate pytest."""
        shutil.copy(REPO / "tests" / "conftest.py", tmp_path / "conftest.py")
        (tmp_path / "test_reaches_out.py").write_text(
            "import socket\n\n"
            "def test_reaches_out():\n"
            "    try:\n"
            "        socket.getaddrinfo('example.com', 443)\n"
            "    except OSError:\n"
            "        pass\n",
            encoding="utf-8",
        )
        environment = dict(os.environ, PYTHONPATH=str(REPO))
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=300,
        )
        assert result.returncode != 0
        assert "This test tried to reach example.com" in result.stdout


def test_the_audiobook_voices_come_from_the_fixed_list():
    from ficary import tts
    from ficary.tts_providers.edge import EdgeProvider

    provider = EdgeProvider()
    if not provider.is_installed():
        pytest.skip("edge-tts is not installed")
    names = {voice.short_name for voice in provider.list_voices()}
    assert names == set(tts.MALE_VOICES) | set(tts.FEMALE_VOICES)
