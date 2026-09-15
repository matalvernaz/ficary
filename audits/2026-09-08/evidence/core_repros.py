"""Offline audit probes for export safety and scraper integration contracts."""
from pathlib import Path
import contextlib
import io
import json
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
with tempfile.TemporaryDirectory(prefix='ficary-core-audit-') as scratch:
    root = Path(scratch)
    with patch.object(Path, 'home', return_value=root):
        from ficary import portable
    portable._cached_root = root / 'portable'
    from ficary import cli
    from ficary.models import Story, Chapter
    from ficary.exporters import export_html
    from ficary.updater import extract_source_url, read_chapters
    from ficary.scraper import BaseScraper
    from ficary.scribblehub import ScribbleHubScraper
    from ficary.subscribestar import SubscribeStarScraper
    from ficary.sites import extract_story_url
    from ficary.url_classifier import classify
    from bs4 import BeautifulSoup

    def story(n, title='Same Title', author='Same Author', count=1):
        return Story(n,title,author,'',f'https://www.fanfiction.net/s/{n}',chapters=[Chapter(i,f'Section {i}',f'<p>story {n} chapter {i}</p>') for i in range(1,count+1)])
    results={}
    out=root/'collisions'; out.mkdir()
    first=export_html(story(101),str(out))
    second=export_html(story(202),str(out))
    results['fresh_export_collision']={'same_path':first==second,'remaining_files':len(list(out.glob('*.html'))),'remaining_source':extract_source_url(first)}

    out=root/'update'; out.mkdir()
    existing=export_html(story(101,title='Renamed by user'),str(out))
    victim=export_html(story(202),str(out))
    args=cli._build_parser().parse_args(['-f','html'])
    fake=Mock(site_name='ffn'); fake.parse_story_id.return_value=101
    fresh=story(101,count=2); fresh.chapters=fresh.chapters[1:]
    fake.download.return_value=fresh
    with patch.object(cli,'_build_scraper',return_value=fake), patch.object(cli,'_auto_index_exported'), contextlib.redirect_stdout(io.StringIO()):
        ok=cli._download_one(fresh.url,args,out,update_path=existing,existing_chapters=1,status_callback=lambda _:None)
    results['update_collision']={'success':ok,'unrelated_book_survives':victim.exists(),'updated_file_chapters':len(read_chapters(existing))}

    sc=BaseScraper(cache_dir=root/'cache',delay_range=(0,0)); sc.site_name='audit'
    calls=[]
    def fetch(urls):
        calls.extend(urls)
        return [f'<p>Body for {url}</p>' for url in urls]
    sc._fetch_parallel=fetch
    def materialise(chapters):
        return sc._materialise_chapters(story_id=1,chapter_list=[{'url':u,'title':u} for u in chapters],skip_chapters=0,chapter_spec=None,parse_chapter=lambda soup:str(soup.p),progress_callback=None)
    materialise(['A','B']); calls.clear()
    new=materialise(['A','INSERTED','B'])
    results['ordinal_cache_insertion']={'expected':['A','INSERTED','B'],'actual_bodies':[c.html for c in new],'fetched':calls}

    sc=ScribbleHubScraper(use_cache=False,delay_range=(0,0))
    recent='<a class="toc_a" href="https://www.scribblehub.com/read/1-x/chapter/90/">Chapter 9</a><a class="toc_a" href="https://www.scribblehub.com/read/1-x/chapter/80/">Chapter 8</a>'
    fake=Mock(); fake.post.return_value=SimpleNamespace(status_code=503,text='Unavailable')
    with patch.object(sc,'_session',return_value=fake):
        toc=sc._fetch_full_toc(1,BeautifulSoup(recent,'lxml'))
    results['scribblehub_partial_toc']={'reported_chapter_count':len(toc),'titles':[c['title'] for c in toc],'raised':False}

    sc=SubscribeStarScraper(session_cookie='session=audit',use_cache=False,delay_range=(0,0))
    args=cli._build_parser().parse_args(['--subscribestar-story','A Tale','-f','audio'])
    with patch.object(cli,'_build_scraper',return_value=sc), patch.object(cli,'check_format_deps'), patch.object(sc,'download_creator_story',return_value=story(101)),contextlib.redirect_stdout(io.StringIO()):
        try: cli._handle_subscribestar_story(['https://subscribestar.adult/creator'],args,root)
        except Exception as exc: results['subscribestar_audio']={'error':type(exc).__name__,'message':str(exc)}
    posts=[{'id':'1','title':'A Tale Pt.1','body_html':'<p>First tale</p>'},{'id':'2','title':'Another Tale Pt.1','body_html':'<p>Other tale</p>'}]
    with patch.object(sc,'_enumerate_posts',return_value=posts):
        works=[sc.download_creator_story('creator',title) for title in ('A Tale','Another Tale')]
    results['subscribestar_identity']={'titles':[s.title for s in works],'ids':[s.id for s in works],'urls':[s.url for s in works]}
    with patch.object(sc,'_fetch',return_value='<html><body><p>The literal &lt;secret&gt; vanishes.</p></body></html>'):
        html=sc._fetch_gdoc_html('https://docs.google.com/document/d/audit123/edit')
    results['google_doc_text_escape']={'html':html,'rendered_text':BeautifulSoup(html,'lxml').get_text()}
    urls=['https://www.scribblehub.com/series/1234/a-story/','https://subscribestar.adult/posts/1234']
    results['site_registry']=[{'url':u,'clipboard_match':extract_story_url(u),'classification':classify(u).kind} for u in urls]
    print(json.dumps(results,indent=2))
