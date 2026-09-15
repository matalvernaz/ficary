"""Isolated entrypoint, export-format update and job-identity probes."""
from pathlib import Path
import contextlib
import io
import json
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
with tempfile.TemporaryDirectory(prefix='ficary-entry-audit-') as scratch:
    root=Path(scratch)
    with patch.object(Path,'home',return_value=root):
        from ficary import portable
    portable._cached_root=root/'portable'
    from ficary import cli
    from ficary.models import Story,Chapter
    from ficary.exporters import export_epub
    from ficary.gui import MainFrame
    result={}
    err=io.StringIO()
    with contextlib.redirect_stderr(err):
        try: cli.main([])
        except SystemExit as exc: result['pip_no_args']={'exit_code':exc.code,'error':err.getvalue().splitlines()[-1]}
    old=Story(101,'Synthetic','Author','','https://www.fanfiction.net/s/101',chapters=[Chapter(1,'First','<p>First body</p>')])
    path=export_epub(old,str(root))
    args=cli._build_parser().parse_args(['-u',str(path),'-f','html'])
    fake=Mock(site_name='ffn'); fake.parse_story_id.return_value=101
    fake.download.return_value=Story(101,'Synthetic','Author','',old.url,chapters=[Chapter(2,'Second','<p>Second body</p>')])
    with patch.object(cli,'_build_scraper',return_value=fake),patch.object(cli,'_auto_index_exported'),contextlib.redirect_stdout(io.StringIO()):
        code=cli._handle_update_file(args)
    import zipfile
    result['update_format']={'exit_code':code,'suffix':path.suffix,'still_valid_epub_zip':zipfile.is_zipfile(path),'starts_with_html':path.read_bytes().startswith(b'<!DOCTYPE html>')}
    entered=threading.Event(); release=threading.Event(); ran=[]
    def first():
        ran.append('epub@folder-A');entered.set();release.wait(3);return 'epub'
    def second():ran.append('html@folder-B');return 'html'
    main=SimpleNamespace(_log=lambda _:None)
    f1=MainFrame._enqueue_site_job(main,old.url,first)
    assert entered.wait(2)
    f2=MainFrame._enqueue_site_job(main,old.url,second)
    release.set()
    result['different_output_deduped']={'same_future':f1 is f2,'first_result':f1.result(2),'second_result':f2.result(2),'jobs_executed':ran}
    print(json.dumps(result,indent=2))
