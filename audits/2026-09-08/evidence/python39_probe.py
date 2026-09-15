from pathlib import Path
import sys
import tempfile
import traceback
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
print(sys.version)
with tempfile.TemporaryDirectory(prefix='ficary-python39-') as scratch:
    with patch.object(Path, 'home', return_value=Path(scratch)):
        try:
            import ficary.cli
            print('CLI import succeeded')
        except Exception:
            traceback.print_exc(file=sys.stdout)
