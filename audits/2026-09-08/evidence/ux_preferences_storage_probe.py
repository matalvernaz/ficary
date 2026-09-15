"""Inspect native wx config filename; reproduce collision only in temp dir."""
import gc
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import wx
app=wx.App(False)
log=wx.LogStderr()
wx.Log.SetActiveTarget(log)
report={'wx_version':wx.version(), 'native_ficary_local_filename':wx.FileConfig.GetLocalFileName('ficary')}
with tempfile.TemporaryDirectory(prefix='ficary-ux-prefs-') as tmp:
    target=Path(tmp)/'.ficary'
    target.mkdir()
    from ficary.prefs import Prefs
    def factory(*a,**k):
        return wx.FileConfig(appName='ficary',localFilename=str(target),style=wx.CONFIG_USE_LOCAL_FILE)
    with patch('wx.Config',factory), patch('ficary.prefs._migrate_legacy_wx_config'):
        prefs=Prefs()
        result=prefs.set('audit_probe','synthetic-value')
        report['set_return']=result
        report['same_instance_read']=prefs.get('audit_probe')
        report['explicit_flush_result']=prefs._cfg.Flush()
        second=Prefs()
        report['new_instance_read']=second.get('audit_probe')
        report['temp_files']=[p.name for p in Path(tmp).iterdir()]
        del prefs,second
        gc.collect()
print(json.dumps(report,indent=2))
