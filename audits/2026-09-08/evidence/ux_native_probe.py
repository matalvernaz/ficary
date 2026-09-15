"""Isolated wx GTK probes. No account, library, or real preferences access."""
import json
import threading
import time
from unittest.mock import patch
import wx
from ficary.gui_search import SearchFrame, _ffn_search_spec, _ao3_search_spec, _royalroad_search_spec, _wattpad_search_spec
from ficary.preferences import PreferencesDialog

class MemoryPrefs:
    def __init__(self): self.values = {}
    def get(self, key, default=''): return self.values.get(key, default)
    def get_bool(self, key): return bool(self.values.get(key, False))
    def set(self, key, value): self.values[key] = value
    def set_bool(self, key, value): self.values[key] = bool(value)

class Main(wx.Frame):
    def __init__(self):
        super().__init__(None, title='Isolated audit')
        self.prefs = MemoryPrefs()
        self._global_busy = False
        self.messages = []
        self._search_frames = {}
    @property
    def _downloading(self): return self._global_busy
    def _set_busy(self, busy, kind=None): self._global_busy = busy
    def _log(self, message): self.messages.append(message)
    def _notify_search_frame_closed(self, key): self._search_frames.pop(key, None)

def pump(seconds=.1):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        wx.Yield()
        time.sleep(.01)

app = wx.App(False)
main = Main()
main.Show()
report = {'wx_version': wx.version()}
started, release, finished = threading.Event(), threading.Event(), threading.Event()
def fake_search(*args, **kwargs):
    started.set()
    release.wait(3)
    finished.set()
    return [], 2
with patch('ficary.search.fetch_until_limit', fake_search):
    frame = SearchFrame(main, 'ffn', _ffn_search_spec())
    main._search_frames['ffn'] = frame
    frame.Show()
    frame.query_ctrl.SetValue('audit-only-query')
    frame._on_search()
    assert started.wait(2)
    report['search_close'] = {'busy_while_running': main._global_busy}
    frame.Close()
    pump()
    release.set()
    assert finished.wait(2)
    pump()
    report['search_close']['busy_after_worker_finished'] = main._global_busy
    reopened = SearchFrame(main, 'ffn', _ffn_search_spec())
    reopened.Show()
    pump()
    report['search_close']['reopened_search_enabled'] = reopened.search_btn.IsEnabled()
    reopened.Close()
    pump()
main._global_busy = False

dlg = PreferencesDialog(main, main.prefs)
dlg.Show()
dlg.notebook.SetSelection(1)
pump()
page = dlg.notebook.GetPage(1)
report['preferences_downloads'] = {
    'dialog_size': list(dlg.GetSize()),
    'page_size': list(page.GetClientSize()),
    'page_type': type(page).__name__,
    'sizer_minimum': list(page.GetSizer().CalcMin()),
    'controls': [],
}
for key in ('format_ctrl','html_style_ctrl','chapter_notes_ctrl','cf_solve_ctrl','webnovel_cookie_ctrl','ao3_cookie_ctrl','ao3_user_agent_ctrl','scribblehub_cookie_ctrl','subscribestar_cookie_ctrl'):
    ctrl = getattr(dlg, key)
    rect = ctrl.GetRect()
    report['preferences_downloads']['controls'].append({'field': key, 'rect': list(rect), 'within_page': rect.x >= 0 and rect.y >= 0 and rect.GetRight() < page.GetClientSize().width and rect.GetBottom() < page.GetClientSize().height})
dlg.Destroy()
pump()
report['search_geometry'] = {}
for key, factory in [('ffn',_ffn_search_spec),('ao3',_ao3_search_spec),('royalroad',_royalroad_search_spec),('wattpad',_wattpad_search_spec)]:
    frame = SearchFrame(main,key,factory())
    frame.Show()
    pump()
    panel = frame.query_ctrl.GetParent()
    clipped = []
    for ctrl in panel.GetChildren():
        if not isinstance(ctrl,(wx.TextCtrl,wx.Choice,wx.Button,wx.CheckBox)) or not ctrl.IsShown(): continue
        rect = ctrl.GetRect()
        if rect.x < 0 or rect.y < 0 or rect.GetRight() >= panel.GetClientSize().width or rect.GetBottom() >= panel.GetClientSize().height:
            clipped.append({'name':ctrl.GetName(),'label':ctrl.GetLabel(),'rect':list(rect)})
    report['search_geometry'][key]={'size':list(frame.GetSize()),'panel_size':list(panel.GetClientSize()),'sizer_minimum':list(panel.GetSizer().CalcMin()),'clipped':clipped}
    frame.Close()
    pump()
main.Destroy()
pump()
print(json.dumps(report,indent=2))
