"""Synthetic saved settings through the real DownloadJob and scraper builder."""
import json
from unittest.mock import patch
from ficary.jobs import DownloadJob
from ficary.cli import _build_scraper
from ficary.scribblehub import ScribbleHubScraper
from ficary.subscribestar import SubscribeStarScraper
class Prefs:
    settings={'cf_solve':True,'scribblehub_cookie':'synthetic-scribblehub','subscribestar_cookie':'synthetic-subscribestar'}
    def get(self,key,default=None):return self.settings.get(key,default)
    def get_bool(self,key):return bool(self.settings.get(key,False))
with patch('ficary.prefs.Prefs',Prefs),patch('ficary.cli._legacy.getenv_compat',return_value=None):
    job=DownloadJob.from_prefs()
    report={'saved_cf_solve':True,'job_cf_solve':job.cf_solve,'job_has_scribblehub_cookie':hasattr(job,'scribblehub_cookie'),'job_has_subscribestar_cookie':hasattr(job,'subscribestar_cookie')}
    for name,cls,url in [('scribblehub',ScribbleHubScraper,'https://www.scribblehub.com/series/123/test/'),('subscribestar',SubscribeStarScraper,'https://subscribestar.adult/example')]:
        with patch.object(cls,'__init__',return_value=None) as init:
            _build_scraper(url,job)
            report[name+'_constructor_kwargs']=init.call_args.kwargs
print(json.dumps(report,indent=2))
