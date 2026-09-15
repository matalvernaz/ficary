"""Deterministic live-TTS Stop race with blocked highlight and mock engine."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import tempfile,threading
from unittest.mock import MagicMock
from ficary.reader.live_tts import LiveTTSController

entered=threading.Event(); release=threading.Event()
engine=MagicMock()
def highlight(chunk): entered.set(); assert release.wait(3)
def synth(voice,text,path,**kw): path.write_bytes(b'synthetic audio')
with tempfile.TemporaryDirectory(prefix='ficary-audit-live-stop-') as tmp:
    controller=LiveTTSController(engine,voice='mock',synth=synth,tmp_dir=Path(tmp),on_highlight=highlight)
    controller.start('A sentence.')
    assert entered.wait(3)
    worker=controller._worker
    controller.stop()
    before=engine.play_file.call_count
    release.set();worker.join(3)
    print('Play calls before releasing stale worker:',before)
    print('Play calls after Stop already returned:',engine.play_file.call_count)
    print('Old worker finished:',not worker.is_alive())
