"""Prefs migrations. Pure logic — no wx, no display."""

from __future__ import annotations


def test_legacy_output_dir_migrates_into_library_path():
    """Someone who only ever set "Default output folder" must come out
    of the upgrade with that folder as their library, not with nothing."""
    from ficary.prefs import (
        KEY_LIBRARY_PATH, KEY_OUTPUT_DIR, _migrate_output_dir_to_library,
    )

    class _Cfg:
        def __init__(self, values):
            self.values = dict(values)

        def Read(self, key, default=""):
            return self.values.get(key, default)

        def Write(self, key, value):
            self.values[key] = value

        def DeleteEntry(self, key):
            self.values.pop(key, None)

        def Flush(self):
            pass

    cfg = _Cfg({KEY_OUTPUT_DIR: "/old/library"})
    _migrate_output_dir_to_library(cfg)
    assert cfg.values[KEY_LIBRARY_PATH] == "/old/library"
    assert KEY_OUTPUT_DIR not in cfg.values

    # A configured library wins; the stale value is dropped, not applied.
    cfg = _Cfg({KEY_OUTPUT_DIR: "/old/staging", KEY_LIBRARY_PATH: "/real/lib"})
    _migrate_output_dir_to_library(cfg)
    assert cfg.values[KEY_LIBRARY_PATH] == "/real/lib"
    assert KEY_OUTPUT_DIR not in cfg.values
