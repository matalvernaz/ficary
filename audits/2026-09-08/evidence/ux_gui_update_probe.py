"""Actual GUI update orchestration, real TXT files, fake upstream in temp dir."""
import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace,MethodType
from ficary.gui import MainFrame,_DownloadParams
from ficary.models import Story,Chapter
from ficary.exporters import export_txt
from ficary.updater import count_chapters
URL='https://www.fanfiction.net/s/12345'
class Prefs:
    def get(self,key,default=''):return default
class Scraper:
    def parse_story_id(self,url):return 12345
    def is_author_url(self,url):return False
    def download(self,url,**kwargs):
        chapters=[Chapter(2,'Second','<p>New body.</p>')]
        if not kwargs.get('skip_chapters'):
            chapters.insert(0,Chapter(1,'First','<p>Old body.</p>'))
        return Story(12345,'Synthetic title','Synthetic author','',URL,chapters=chapters)
with tempfile.TemporaryDirectory(prefix='ficary-ux-update-') as tmp:
    old=export_txt(Story(12345,'Synthetic title','Synthetic author','',URL,chapters=[Chapter(1,'First','<p>Old body.</p>')]),tmp,template='My renamed book')
    before=hashlib.sha256(old.read_bytes()).hexdigest()
    params=_DownloadParams(fmt='epub',raw_output_dir=tmp,filename_template='{title} - {author}',hr_as_stars=False,strip_notes=False,llm_strip_notes=False)
    logs=[]
    main=SimpleNamespace(prefs=Prefs(),_picker_transferred_busy=False,_snapshot_download_params=lambda:params,_scraper_for=lambda *a,**k:Scraper(),_log=logs.append,_auto_index_download=lambda p:None,_set_busy=lambda *a,**k:None,_enqueue_site_job=lambda u,fn,**k:fn())
    for name in ('_run_download','_export_story','_resolve_output_dir'):
        setattr(main,name,MethodType(getattr(MainFrame,name),main))
    MainFrame._begin_update_for_path(main,old)
    report={'original_unchanged':before==hashlib.sha256(old.read_bytes()).hexdigest(),'original_chapters':count_chapters(old),'files':{p.name:count_chapters(p) for p in Path(tmp).glob('*.txt')},'logs':logs}
print(json.dumps(report,indent=2))
