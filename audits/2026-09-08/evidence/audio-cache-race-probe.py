"""Mock synthesis/ffmpeg + deterministic soundscape race; no audio/network."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import asyncio, tempfile, threading
from types import SimpleNamespace
from unittest.mock import patch
from ficary import tts
from ficary.models import Chapter, Story
from ficary.audio.engine import AudioEngine, _Backend, CHANNEL_AMBIENT
from ficary.audio.events import Event, ReaderEvent
from ficary.soundscape.session import SoundscapeSession
from ficary.soundscape.model import Soundscape, Sound

with tempfile.TemporaryDirectory(prefix='ficary-audit-cache-') as tmp:
    tmp=Path(tmp); cache=tmp/'cache'; cache.mkdir(); build=tmp/'build'; build.mkdir()
    segments=[tts.Segment('Successful sentence that is long enough to avoid small-segment merging.',speaker='A'), tts.Segment('LOST sentence that is long enough to avoid small-segment merging.',speaker='B')]
    story=SimpleNamespace(id='audit',title='Synthetic',author='Audit',url='https://example.invalid',metadata={},chapters=[Chapter(number=1,title='Only chapter',html='<p>Synthetic</p>')])
    mapper=tts.VoiceMapper(); calls=[]
    async def synth(seg, voice, path, **kw):
        calls.append(seg.text)
        if seg.text.startswith('LOST'): return False
        path.write_bytes(seg.text.encode()); return True
    async def heading(*a,**k): return False
    def concat(cmd,**kw):
        source=Path(cmd[cmd.index('-i')+1])
        inputs=[Path(line[len("file '"):-1]) for line in source.read_text().splitlines()]
        Path(cmd[-1]).write_bytes(b'|'.join(p.read_bytes() for p in inputs))
        return SimpleNamespace(returncode=0,stderr='')
    def mux(chapters,story,out,*a,**kw): out.write_bytes(b'|'.join(p.read_bytes() for p,title in chapters))
    options=dict(story=story,output_dir=tmp,build_tmp=build,cache_root=cache,mapper=mapper,narrator=tts.NARRATOR_VOICE,speech_rate=0,progress_callback=None,all_segments=[segments])
    with patch.object(tts,'_generate_segment_audio',synth), patch.object(tts,'_make_silence_clip',return_value=None), patch.object(tts,'_run_silent',concat), patch.object(tts,'_synthesize_heading',heading), patch.object(tts,'build_m4b',mux):
        out=tts._generate_audiobook_inner(**options)
        print('First render output:',out.read_text())
        print('First render synth calls:',len(calls))
        out=tts._generate_audiobook_inner(**options)
        print('Second render synth calls:',len(calls))
        print('Second render still omits LOST sentence:',b'LOST' not in out.read_bytes())
        cancel=threading.Event()
        async def cancel_heading(*a,**kw): cancel.set(); return False
        with patch.object(tts,'_synthesize_heading',cancel_heading):
            result=tts._generate_audiobook_inner(**options,cancel_event=cancel)
            print('Cancel set during last heading but render returns success:',cancel.is_set() and result.is_file())

class Backend(_Backend):
    available=True
    def __init__(self):
        self.blocked=threading.Event(); self.release=threading.Event(); self.lock=threading.Lock(); self.n=0; self.stopped=[]
    def load(self,path,*,looping):
        with self.lock: self.n+=1; handle=self.n
        if handle==1: self.blocked.set(); assert self.release.wait(3)
        return handle
    def stop(self,handle): self.stopped.append(handle)

backend=Backend(); engine=AudioEngine(backend)
old=Soundscape('Old',[Sound('old.wav')]); new=Soundscape('New',[Sound('new.wav')])
with patch('ficary.soundscape.session.library.resolve_source',side_effect=lambda source:Path(source)):
    session=SoundscapeSession(engine,old)
    session._on_event(Event(ReaderEvent.READER_OPENED,story_key='audit'))
    assert backend.blocked.wait(3)
    old_thread=session._build_thread
    session.set_soundscape(new)
    session._join_build()
    print('New soundscape handles before old load completes:',list(engine._channels[CHANNEL_AMBIENT].handles))
    backend.release.set(); old_thread.join(3)
    print('New soundscape handles after old load completes:',list(engine._channels[CHANNEL_AMBIENT].handles))
    print('Session still marked started:',session._started)
    session.close(); engine.shutdown()
