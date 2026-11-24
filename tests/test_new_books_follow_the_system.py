"""New books and audiobooks get the system's usual permissions; Ficary's own files stay private; an audiobook is put in place only once it is complete.

Every file Ficary writes goes through a temporary file that is swapped in when complete. That temporary file was created owner-only, so on Linux and Mac every new book could be read by its owner alone, and a media server running under another account could not see it until its permissions were changed by hand. The maintainer chose: new books follow the system (the umask and the folder's default ACL), settings, cookies, keys and caches stay private, and a book that already exists keeps its permissions.

The audiobook was the one book not written that way: ffmpeg wrote it straight to its final name, so a power cut or a crash during that last step left a cut-off audiobook that looked finished, and a re-make that failed wrecked the good one already there.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from ficary import atomic, tts
from ficary.models import Chapter, Story

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix permission bits")


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def umask():
    """Sets the process umask for one test and puts the old one back."""
    previous = os.umask(0o022)
    os.umask(previous)
    holder = {}

    def set_to(value: int) -> None:
        holder.setdefault("old", os.umask(value))

    yield set_to
    os.umask(holder.get("old", previous))


def leftovers(folder: Path) -> list[str]:
    """Temporary files left behind in ``folder``: their names start with a dot."""
    return sorted(entry.name for entry in folder.iterdir() if entry.name.startswith("."))


# ── The safe-write module ────────────────────────────────────────


class TestSafeWrites:
    @pytest.mark.parametrize("mask, expected", [(0o022, 0o644), (0o002, 0o664), (0o027, 0o640), (0o077, 0o600)])
    def test_a_new_shared_file_gets_what_any_new_file_would(self, tmp_path, umask, mask, expected):
        umask(mask)
        target = tmp_path / "Story - Author.txt"
        atomic.atomic_write_text(target, "the book", shared=True)
        assert mode_of(target) == expected
        assert target.read_text(encoding="utf-8") == "the book"

    def test_a_new_private_file_is_owner_only_whatever_the_umask(self, tmp_path, umask):
        umask(0o002)
        target = tmp_path / "settings.ini"
        atomic.atomic_write_text(target, "key=value\n")
        assert mode_of(target) == 0o600

    @pytest.mark.parametrize("shared", [True, False])
    def test_a_file_that_exists_keeps_its_permissions(self, tmp_path, umask, shared):
        umask(0o022)
        target = tmp_path / "book.txt"
        target.write_text("old", encoding="utf-8")
        target.chmod(0o640)
        atomic.atomic_write_text(target, "new", shared=shared)
        assert mode_of(target) == 0o640
        assert target.read_text(encoding="utf-8") == "new"

    def test_bytes_follow_the_same_rules(self, tmp_path, umask):
        umask(0o022)
        target = tmp_path / "cover.bin"
        atomic.atomic_write_bytes(target, b"\x00\x01", shared=True)
        assert mode_of(target) == 0o644

    def test_a_path_handed_to_another_writer_follows_them_too(self, tmp_path, umask):
        umask(0o022)
        shared = tmp_path / "book.epub"
        private = tmp_path / "cookies.json"
        with atomic.atomic_path(shared, shared=True) as temporary:
            temporary.write_bytes(b"zip")
        with atomic.atomic_path(private) as temporary:
            temporary.write_bytes(b"{}")
        assert mode_of(shared) == 0o644
        assert mode_of(private) == 0o600

    def test_no_temporary_file_is_left_behind(self, tmp_path, umask):
        umask(0o022)
        atomic.atomic_write_text(tmp_path / "a.txt", "a", shared=True)
        with atomic.atomic_path(tmp_path / "b.epub", shared=True) as temporary:
            temporary.write_bytes(b"b")
        assert leftovers(tmp_path) == []

    def test_a_failed_shared_write_leaves_the_old_file_and_no_temporary(self, tmp_path, umask):
        umask(0o022)
        target = tmp_path / "book.epub"
        target.write_bytes(b"the good book")
        with pytest.raises(RuntimeError):
            with atomic.atomic_path(target, shared=True) as temporary:
                temporary.write_bytes(b"half a bo")
                raise RuntimeError("the writer broke")
        assert target.read_bytes() == b"the good book"
        assert leftovers(tmp_path) == []

    def test_the_temporary_name_keeps_the_suffix_asked_for(self, tmp_path):
        seen = {}
        with atomic.atomic_path(tmp_path / "Story.m4b", suffix=".m4b", shared=True) as temporary:
            seen["name"] = temporary.name
            temporary.write_bytes(b"x")
        assert seen["name"].startswith(".Story.m4b.")
        assert seen["name"].endswith(".m4b")


# ── The books themselves ─────────────────────────────────────────


def a_story() -> Story:
    return Story(
        id=4321, title="Permissions", author="Nobody", summary="A story.",
        url="https://www.royalroad.com/fiction/4321/permissions",
        chapters=[Chapter(1, "One", "<p>The first chapter.</p>"), Chapter(2, "Two", "<p>The second.</p>")],
    )


class TestBooks:
    @pytest.mark.parametrize("fmt", ["txt", "html", "epub"])
    def test_a_new_book_can_be_read_by_other_accounts_where_the_system_allows(self, tmp_path, umask, fmt):
        from ficary.exporters import EXPORTERS, check_format_deps

        try:
            check_format_deps(fmt)
        except ImportError:
            pytest.skip(f"{fmt} export needs an optional package")
        umask(0o022)
        written = Path(EXPORTERS[fmt](a_story(), str(tmp_path)))
        assert written.exists()
        assert mode_of(written) == 0o644

    def test_settings_stay_private(self, tmp_path, umask):
        from ficary.prefs import _FileStore

        umask(0o022)
        store = _FileStore(tmp_path / "settings.ini")
        store.Write("ao3_cookie", "a secret")
        assert store.Flush()
        assert mode_of(tmp_path / "settings.ini") == 0o600


# ── The audiobook ────────────────────────────────────────────────


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def silent_mp3(path: Path, seconds: float = 1.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
         "-t", str(seconds), "-c:a", "libmp3lame", "-b:a", "32k", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def chapters(tmp_path):
    folder = tmp_path / "chapters"
    folder.mkdir()
    return [(silent_mp3(folder / f"ch{number}.mp3"), f"Chapter {number}") for number in (1, 2)]


@needs_ffmpeg
class TestTheAudiobook:
    def test_a_new_audiobook_is_complete_and_follows_the_system(self, tmp_path, umask, chapters):
        umask(0o022)
        out = tmp_path / "out"
        out.mkdir()
        target = out / "Permissions - Nobody.m4b"
        assert tts.build_m4b(chapters, a_story(), target) == target
        assert mode_of(target) == 0o644
        probed = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-of", "csv=p=0", str(target)],
            capture_output=True, text=True, check=True,
        )
        assert len(probed.stdout.strip().splitlines()) == 2
        assert leftovers(out) == []

    def test_a_replaced_audiobook_keeps_its_permissions(self, tmp_path, umask, chapters):
        umask(0o022)
        target = tmp_path / "Permissions - Nobody.m4b"
        target.write_bytes(b"an older audiobook")
        target.chmod(0o640)
        tts.build_m4b(chapters, a_story(), target)
        assert mode_of(target) == 0o640
        assert target.read_bytes() != b"an older audiobook"

    def test_a_failure_part_way_leaves_the_old_audiobook_whole(self, tmp_path, monkeypatch, chapters):
        """What a crash or power cut in the last step used to do: the half-written file sat at the final name."""
        target = tmp_path / "Permissions - Nobody.m4b"
        target.write_bytes(b"the good audiobook")

        def write_half_then_fail(chapter_files, story, output_path, tmp_dir, **options):
            Path(output_path).write_bytes(b"half an audiob")
            raise RuntimeError("ffmpeg failed during m4b mux (exit 1)")

        monkeypatch.setattr(tts, "_build_m4b_inner", write_half_then_fail)
        with pytest.raises(RuntimeError, match="m4b mux"):
            tts.build_m4b(chapters, a_story(), target)
        assert target.read_bytes() == b"the good audiobook"
        assert leftovers(tmp_path) == []

    def test_a_failure_with_no_old_audiobook_leaves_nothing_at_the_name(self, tmp_path, monkeypatch, chapters):
        target = tmp_path / "Permissions - Nobody.m4b"

        def write_half_then_fail(chapter_files, story, output_path, tmp_dir, **options):
            Path(output_path).write_bytes(b"half an audiob")
            raise RuntimeError("ffmpeg failed during m4b mux (exit 1)")

        monkeypatch.setattr(tts, "_build_m4b_inner", write_half_then_fail)
        with pytest.raises(RuntimeError):
            tts.build_m4b(chapters, a_story(), target)
        assert not target.exists()
        assert leftovers(tmp_path) == []

    def test_ffmpeg_writes_the_temporary_name_not_the_real_one(self, tmp_path, monkeypatch, chapters):
        written_to = []
        real_inner = tts._build_m4b_inner

        def watch(chapter_files, story, output_path, tmp_dir, **options):
            written_to.append(Path(output_path))
            return real_inner(chapter_files, story, output_path, tmp_dir, **options)

        monkeypatch.setattr(tts, "_build_m4b_inner", watch)
        target = tmp_path / "Permissions - Nobody.m4b"
        tts.build_m4b(chapters, a_story(), target)
        assert len(written_to) == 1
        assert written_to[0] != target
        assert written_to[0].parent == target.parent
        assert written_to[0].suffix == ".m4b"

    def test_a_cancel_during_assembly_replaces_nothing(self, tmp_path, chapters):
        import threading

        target = tmp_path / "Permissions - Nobody.m4b"
        target.write_bytes(b"the good audiobook")
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(tts.AudiobookCancelled, match="final assembly"):
            tts.build_m4b(chapters, a_story(), target, cancel_event=cancel)
        assert target.read_bytes() == b"the good audiobook"
        assert leftovers(tmp_path) == []
