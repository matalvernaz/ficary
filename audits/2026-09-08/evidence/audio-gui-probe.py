"""Unbound real GUI handlers with control doubles and blocked fake renders."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
from ficary.gui import MainFrame
from ficary import tts

frame=MagicMock()
frame.format_ctrl.GetSelection.return_value=0
frame.format_ctrl.GetString.return_value='audio'
frame.output_ctrl.GetValue.return_value='/tmp/ficary-audit-unused'
frame.name_ctrl.GetValue.return_value='{title}'
frame.strip_notes_ctrl.GetValue.return_value=False
frame.llm_strip_notes_ctrl.GetValue.return_value=False
frame.hr_stars_ctrl.GetValue.return_value=False
frame._selected_attribution_backend.return_value='llm'
frame._selected_size.return_value=None
frame._llm_config_for_render.return_value={'provider':'ollama','model':'audit'}
frame._enabled_tts_providers.return_value=['piper']
frame.speech_rate_ctrl.GetValue.return_value=0
frame.abs_send_ctrl.GetValue.return_value=False
frame.prefs.get.return_value=''
frame.prefs.get_bool.return_value=False
params=MainFrame._snapshot_download_params(frame)
print('LLM attribution selected, notes stripping disabled:',params.audio_backend,params.llm_render_config)

frame._resolve_output_dir.return_value='/tmp/ficary-audit-unused'
frame._render_cancel=None
starts={'A':threading.Event(),'B':threading.Event()}
releases={'A':threading.Event(),'B':threading.Event()}
events={}
def render(story,*a,**kwargs):
    events[story.id]=kwargs['cancel_event']; starts[story.id].set(); assert releases[story.id].wait(3)
    return None
with patch.object(tts,'generate_audiobook',render),patch('ficary.gui.wx.CallAfter',side_effect=lambda callback,*a,**k: callback(*a,**k)):
    a=threading.Thread(target=MainFrame._export_story,args=(frame,SimpleNamespace(id='A'),params))
    b=threading.Thread(target=MainFrame._export_story,args=(frame,SimpleNamespace(id='B'),params))
    a.start(); assert starts['A'].wait(3)
    b.start(); assert starts['B'].wait(3)
    print('Second render overwrites cancel target:',frame._render_cancel is events['B'])
    releases['A'].set(); a.join(3)
    print('First render completes while second active; cancel pointer:',frame._render_cancel)
    MainFrame._on_cancel_render(frame,None)
    print('Cancel after first completes reaches second:',events['B'].is_set())
    releases['B'].set(); b.join(3)
