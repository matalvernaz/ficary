"""Native wx controls, synthetic story, mock playback; no external requests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import wx
from ficary import portable
from ficary.models import Chapter
from ficary.reader.source import StorySource
from ficary.reader.chunker import chunk_text
from ficary.reader import gui
from ficary.prefs import KEY_READER_THEME, KEY_READER_FONT_PT, KEY_READER_TTS_MODE

class Prefs(dict):
    def get_bool(self, key): return bool(self.get(key))
    def set(self, key, value): self[key] = value

app = wx.App(False)
with tempfile.TemporaryDirectory(prefix='ficary-audit-reader-') as tmp:
    portable._cached_root = Path(tmp)
    prefs = Prefs({KEY_READER_THEME:'light', KEY_READER_FONT_PT:14, KEY_READER_TTS_MODE:'appvoice'})
    engine = MagicMock(available=True)
    source = StorySource(title='Synthetic audit', author='Audit', story_key='audit-reader', chapter_count=2,
        loader=lambda n: Chapter(number=n, title=f'Title {n}', html='<p>First sentence of the chapter.</p><p>Second sentence to resume here.</p>'))
    with patch.object(gui, 'get_engine', return_value=engine), patch.object(gui, 'LiveTTSController') as controllers:
        frame = gui.ReaderFrame(None, prefs, source)
        controller = controllers.return_value
        controller.is_active.return_value = False
        restored = frame._text_prefix_len + len('First sentence of the chapter.\n\n')
        frame.text.SetInsertionPoint(restored)
        frame._begin_playback('piper:en_US-amy-medium')
        print('Resume caret:', restored)
        print('Text sent to TTS:', repr(controller.start.call_args.args[0]))
        chunks = chunk_text(frame._current_rc.text)
        frame.text.SetInsertionPoint(0)
        frame._highlight_chunk(chunks[-1])
        frame._save_position()
        print('Saved position after highlighting final chunk:', frame._state.load_position(source.story_key))
        frame._on_stop_tts(None)
        frame.mode.SetSelection(0)
        frame._resolving_voice = True
        frame._begin_playback('piper:en_US-amy-medium')
        print('Late resolution starts after Stop + Screen reader mode:', frame._live is controller)
        print('Total start calls:', controller.start.call_count)
        frame.Close()
        app.Yield()

from ficary.reader.live_tts import LiveTTSController
from ficary.tts_providers import VoiceInfo
import shutil

with tempfile.TemporaryDirectory(prefix='ficary-audit-reader-finish-') as tmp:
    portable._cached_root = Path(tmp)
    prefs = Prefs({KEY_READER_THEME:'light', KEY_READER_FONT_PT:14, KEY_READER_TTS_MODE:'appvoice', 'tts_providers':'piper'})
    engine = MagicMock(available=True)
    engine.play_file.side_effect = lambda *a,**kw: kw['on_done']()
    created=[]
    def synth(voice,text,path,**kw): path.write_bytes(b'synthetic audio')
    def make_controller(*a,**kw):
        c=LiveTTSController(*a,**kw,synth=synth); created.append(c); return c
    with patch.object(gui,'get_engine',return_value=engine),patch.object(gui,'LiveTTSController',side_effect=make_controller):
        frame=gui.ReaderFrame(None,prefs,source)
        with patch('ficary.tts_providers.all_voices',return_value=[VoiceInfo('edge:foreign','edge','foreign','ar-SA','Female','Foreign'),VoiceInfo('piper:english','piper','english','en-US','Female','English')]):
            print('Reader default with only Piper selected:',frame._default_voice())
        frame._begin_playback('mock'); created[-1]._worker.join(3)
        frame._load_chapter(2); frame._begin_playback('mock'); created[-1]._worker.join(3)
        frame.Close(); app.Yield()
        print('First completed chapter temp files survive reader close:',created[0]._tmp_dir.exists())
        print('Latest completed chapter temp files survive reader close:',created[-1]._tmp_dir.exists())
        for c in created: shutil.rmtree(c._tmp_dir,ignore_errors=True)
