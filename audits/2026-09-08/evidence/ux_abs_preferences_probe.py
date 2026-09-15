"""Native event-loop and cancel semantics, synthetic ABS request and settings."""
import json
import time
from unittest.mock import patch
import wx
from ficary.preferences import PreferencesDialog
class Prefs:
    def __init__(self):self.values={'abs_url':'https://old.invalid','abs_token':'synthetic-old-token'}
    def get(self,key,default=''):return self.values.get(key,default)
    def get_bool(self,key):return bool(self.values.get(key,False))
    def set(self,key,value):self.values[key]=value
    def set_bool(self,key,value):self.values[key]=bool(value)
app=wx.App(False)
prefs=Prefs()
dlg=PreferencesDialog(None,prefs)
dlg.Show()
dlg.notebook.SetSelection(3)
wx.Yield()
dlg.abs_url_ctrl.SetValue('https://new.invalid')
dlg.abs_token_ctrl.SetValue('synthetic-new-token')
timeline=[]
def request(_prefs):
    timeline.append({'event':'request_start','main_thread':wx.IsMainThread()})
    time.sleep(.15)
    timeline.append({'event':'request_end'})
    return []
timer=wx.CallLater(20,lambda:timeline.append({'event':'ui_timer'}))
with patch('ficary.audiobookshelf.list_libraries',request):
    dlg._on_abs_fetch_libraries(None)
wx.Yield()
# No OK/save event. Destroy the cancelled dialog, as the owner does.
dlg.Destroy()
wx.Yield()
print(json.dumps({'timeline':timeline,'settings_after_cancel_without_ok':prefs.values},indent=2))
