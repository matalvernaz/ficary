"""Capture response diagnostics with synthetic cookie values only."""
from pathlib import Path
import io
import json
import logging
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
with tempfile.TemporaryDirectory(prefix='ficary-log-audit-') as scratch:
    with patch.object(Path,'home',return_value=Path(scratch)):
        from ficary.scraper import BaseScraper
    logger=logging.getLogger('ficary.scraper'); logger.setLevel(logging.DEBUG)
    buf=io.StringIO(); handler=logging.StreamHandler(buf);logger.addHandler(handler)
    sc=BaseScraper(use_cache=False)
    value='SYNTHETIC_SESSION_VALUE'
    resp=SimpleNamespace(status_code=200,headers={'Set-Cookie':f'session={value}; Secure; HttpOnly'},text='')
    sess=SimpleNamespace(cookies=SimpleNamespace(jar=[]))
    sc._log_fetch_diagnostic(resp,sess,'audit','https://example.invalid/story')
    print(json.dumps({'cookie_value_in_debug_log':value in buf.getvalue(),'synthetic_log':buf.getvalue()},indent=2))
