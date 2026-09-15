"""Run existing tests with app persistence isolated from user settings."""
from pathlib import Path
import os
import sys
import tempfile
from unittest.mock import patch

repo = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(repo))
os.chdir(repo)
with tempfile.TemporaryDirectory(prefix='ficary-audit-tests-') as scratch:
    root = Path(scratch)
    # Isolate package bootstrap without changing the process HOME variable.
    with patch.object(Path, 'home', return_value=root):
        from ficary import portable
    portable._cached_root = root / '.ficary'
    import wx
    original_config = wx.Config
    def isolated_config(appName='', *args, **kwargs):
        return wx.FileConfig(appName=appName, localFilename=str(root / (appName + '-settings.ini')), style=wx.CONFIG_USE_LOCAL_FILE)
    wx.Config = isolated_config
    import pytest
    raise SystemExit(pytest.main([
        'tests/', '-q', '-ra', '--tb=short',
        '--cov=ficary',
        '--cov-report=json:audits/2026-09-08/evidence/coverage.json',
        '--cov-report=term',
        '--junitxml=audits/2026-09-08/evidence/pytest.xml',
    ]))
