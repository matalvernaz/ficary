"""LLM attribution failure cache probe; synthetic text and mock provider."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from ficary import tts, attribution, character_profile, portable
from ficary.models import Chapter

with tempfile.TemporaryDirectory(prefix='ficary-audit-attribution-') as tmp:
    portable._cached_root=Path(tmp)
    story=SimpleNamespace(id='audit',title='Synthetic',author='Audit',url='https://example.invalid',metadata={'characters':'Alice'},chapters=[Chapter(number=1,title='One',html='<p>"Hello there," said Alice.</p>')])
    config={'provider':'ollama','model':'audit-model'}
    with patch.object(tts,'_check_ffmpeg'),patch.object(tts,'_build_voice_pool',return_value={}),patch.object(tts,'_generate_audiobook_inner',return_value=Path(tmp)/'out.m4b'),patch.object(character_profile,'analyze_story_via_llm',return_value={}),patch.object(attribution,'_refine_with_llm',side_effect=RuntimeError('synthetic provider unavailable')) as refine:
        attribution._failed_runs.clear()
        tts.generate_audiobook(story,tmp,attribution_backend='llm',attribution_llm_config=config)
        print('Failure recorded under:',sorted(attribution._failed_runs))
        print('Pipeline has_failed(llm,None):',attribution.has_failed('llm',None))
        paths=list(tts._attr_cache_root().rglob('*.json'))
        print('Fallback cache entries written:',len(paths))
        print('Fallback cache content:',paths[0].read_text() if paths else None)
        attribution._failed_runs.clear() # equivalent to restarting the process
        tts.generate_audiobook(story,tmp,attribution_backend='llm',attribution_llm_config=config)
        print('Provider attempts after second render (should retry):',refine.call_count)
