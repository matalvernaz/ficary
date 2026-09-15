"""Isolated SMTP/TTS/Piper boundary probes; no network, credentials or binaries."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import asyncio, io, ssl, smtplib, tarfile, tempfile
from unittest.mock import patch
from ficary import tts
from ficary.tts_providers import piper

smtp = smtplib.SMTP_SSL()
print('SMTP_SSL defaults:', {'check_hostname':smtp.context.check_hostname,'verify_mode':smtp.context.verify_mode.name})
ctx = ssl._create_stdlib_context()
print('STARTTLS default context:', {'check_hostname':ctx.check_hostname,'verify_mode':ctx.verify_mode.name})

async def fallback_probe(tmp):
    voices=[]
    async def synth(seg, voice, path, **kwargs):
        voices.append(voice)
        if voice.startswith('piper:'): return False
        path.write_bytes(b'synthetic mp3')
        return True
    with patch.object(tts, '_generate_segment_audio', synth):
        result = await tts._generate_with_semaphore(asyncio.Semaphore(1),tts.Segment('Private local-only text.'),'piper:en_US-amy-medium',tmp/'fallback.mp3',0,1,narrator_voice='piper:en_US-amy-medium')
    print('Local Piper failure fallback voices:', voices)
    print('Cloud fallback returned success:', result is not None)

with tempfile.TemporaryDirectory(prefix='ficary-audit-audio-') as tmp:
    tmp=Path(tmp)
    asyncio.run(fallback_probe(tmp))
    data=io.BytesIO()
    with tarfile.open(fileobj=data,mode='w:gz') as tar:
        item=tarfile.TarInfo('piper/piper'); payload=b'fake binary'; item.size=len(payload); item.mode=0o755
        tar.addfile(item,io.BytesIO(payload))
        item=tarfile.TarInfo('piper/libexample.so'); payload=b'fake library'; item.size=len(payload)
        tar.addfile(item,io.BytesIO(payload))
    install=tmp/'install'; logs=[]
    with patch.object(piper,'piper_binary_dir',return_value=install), patch.object(piper.shutil,'which',return_value=None), patch.object(piper,'_piper_release_asset',return_value=('mock.tar.gz','tar')), patch.object(piper.urllib.request,'urlopen',return_value=io.BytesIO(data.getvalue())):
        ok=piper.install_piper_binary(logs.append)
        executable=piper.piper_executable()
        print('Piper install result:',ok)
        print('Piper install logs:',logs)
        print('Resolved executable is directory:',bool(executable and Path(executable).is_dir()))
        print('Second install result:',piper.install_piper_binary(logs.append))
