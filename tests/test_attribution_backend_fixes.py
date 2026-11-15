"""Regression tests for the two attribution backends that fell back to builtin on every story:
fastcoref (its clusters were read as spans when they were strings, and its last release can't
load under transformers 5) and BookNLP (setuptools 82 removed the pkg_resources it imports).
The fakes copy the real libraries' behaviour, including fastcoref returning strings by default."""
import os
import sys
import types

import pytest

from ficary import attribution
from ficary.tts import Segment


@pytest.fixture(autouse=True)
def _clean_attribution_state(monkeypatch):
    attribution._failed_runs.clear()
    attribution._booknlp_cache.clear()
    monkeypatch.setattr(attribution, "_fastcoref_patched", False)
    yield
    attribution._failed_runs.clear()
    attribution._booknlp_cache.clear()


# ── fastcoref ─────────────────────────────────────────────────────


class _FakeCorefResult:
    """Behaves like fastcoref 2.1.6's CorefResult."""

    def __init__(self, text, clusters):
        self._text = text
        self._clusters = clusters

    def get_clusters(self, as_strings=True):
        # The real default is as_strings=True: mention strings, not character spans.
        if as_strings:
            return [[self._text[start:end] for start, end in cluster] for cluster in self._clusters]
        return [list(cluster) for cluster in self._clusters]


def _install_fake_fastcoref(monkeypatch, clusters_for):
    """Put a fake ``fastcoref`` in sys.modules whose FCoref answers with ``clusters_for(text)``."""

    class FakeFCoref:
        def __init__(self, device="cpu"):
            self.device = device

        def predict(self, texts):
            return [_FakeCorefResult(text, clusters_for(text)) for text in texts]

    module = types.ModuleType("fastcoref")
    module.FCoref = FakeFCoref
    monkeypatch.setitem(sys.modules, "fastcoref", module)


def test_fastcoref_maps_a_pronoun_speaker_to_the_named_character(monkeypatch):
    text = 'Anna walked in. "Hello," she said.'
    anna = (text.index("Anna"), text.index("Anna") + len("Anna"))
    she = (text.index("she"), text.index("she") + len("she"))
    _install_fake_fastcoref(monkeypatch, lambda _: [[anna, she]])

    segments = [Segment("Hello,", speaker="she")]
    refined = attribution._refine_with_fastcoref(segments, text)

    assert refined[0].speaker == "Anna"


def test_fastcoref_runs_through_the_dispatcher_without_falling_back(monkeypatch):
    text = 'Ben sat down. "I am tired," he said.'
    ben = (text.index("Ben"), text.index("Ben") + len("Ben"))
    he = (text.index(" he ") + 1, text.index(" he ") + 3)
    _install_fake_fastcoref(monkeypatch, lambda _: [[ben, he]])
    monkeypatch.setattr(attribution, "is_installed", lambda backend: True)

    segments = [Segment("I am tired,", speaker="he")]
    refined = attribution.refine_speakers(segments, text, backend="fastcoref")

    assert not attribution.has_failed("fastcoref")
    assert refined[0].speaker == "Ben"


def _install_fake_fcoref_model_class(monkeypatch, model_class):
    """Fake the package path the shim imports: fastcoref.coref_models.modeling_fcoref."""
    package = types.ModuleType("fastcoref")
    package.__path__ = []
    models_package = types.ModuleType("fastcoref.coref_models")
    models_package.__path__ = []
    modeling = types.ModuleType("fastcoref.coref_models.modeling_fcoref")
    modeling.FCorefModel = model_class
    monkeypatch.setitem(sys.modules, "fastcoref", package)
    monkeypatch.setitem(sys.modules, "fastcoref.coref_models", models_package)
    monkeypatch.setitem(sys.modules, "fastcoref.coref_models.modeling_fcoref", modeling)


def test_fastcoref_model_gets_the_attribute_transformers_5_reads(monkeypatch):
    class FCorefModel:
        pass

    _install_fake_fcoref_model_class(monkeypatch, FCorefModel)
    attribution._patch_fastcoref_for_transformers5()

    assert FCorefModel.all_tied_weights_keys == {}


def test_fastcoref_shim_leaves_a_fixed_release_alone(monkeypatch):
    class FCorefModel:
        all_tied_weights_keys = {"head.weight": "embeddings.weight"}

    _install_fake_fcoref_model_class(monkeypatch, FCorefModel)
    attribution._patch_fastcoref_for_transformers5()

    assert FCorefModel.all_tied_weights_keys == {"head.weight": "embeddings.weight"}


def test_fastcoref_shim_is_a_no_op_when_the_class_has_moved(monkeypatch):
    # A plain module, not a package, so the shim's import fails the way it would after a rename.
    monkeypatch.setitem(sys.modules, "fastcoref", types.ModuleType("fastcoref"))
    attribution._patch_fastcoref_for_transformers5()


# ── BookNLP and pkg_resources ─────────────────────────────────────


@pytest.fixture
def no_pkg_resources(monkeypatch):
    """Make ``import pkg_resources`` raise ImportError, as it does on setuptools 82 and later."""
    monkeypatch.setitem(sys.modules, "pkg_resources", None)
    with pytest.raises(ImportError):
        import pkg_resources  # noqa: F401


@pytest.fixture
def fake_booknlp_on_disk(tmp_path, monkeypatch):
    """A booknlp package laid out like the real one: the four modules that need it do
    ``import pkg_resources`` at the top and look up a data file next to themselves."""
    english = tmp_path / "booknlp" / "english"
    english.mkdir(parents=True)
    (tmp_path / "booknlp" / "__init__.py").write_text("")
    (tmp_path / "booknlp" / "booknlp.py").write_text("from booknlp.english import english_booknlp\n")
    (english / "__init__.py").write_text("")
    for name in ("english_booknlp", "entity_tagger", "litbank_coref", "name_coref"):
        (english / f"{name}.py").write_text(
            "import pkg_resources\n"
            "def data_file():\n"
            "    return pkg_resources.resource_filename(__name__, 'data/aliases.txt')\n"
        )
    for loaded in [name for name in sys.modules if name == "booknlp" or name.startswith("booknlp.")]:
        monkeypatch.delitem(sys.modules, loaded)
    # Not syspath_prepend: that helper imports pkg_resources itself.
    monkeypatch.setattr(sys, "path", [str(tmp_path)] + sys.path)
    yield english
    for loaded in [name for name in sys.modules if name == "booknlp" or name.startswith("booknlp.")]:
        del sys.modules[loaded]


def test_booknlp_loads_and_finds_its_data_files_without_pkg_resources(no_pkg_resources, fake_booknlp_on_disk):
    attribution._load_booknlp_modules()

    entity_tagger = sys.modules["booknlp.english.entity_tagger"]
    assert entity_tagger.data_file() == str(fake_booknlp_on_disk / "data" / "aliases.txt")


def test_the_stand_in_is_gone_once_booknlp_has_loaded(no_pkg_resources, fake_booknlp_on_disk):
    attribution._load_booknlp_modules()

    # Anything else that imports pkg_resources later must get the ImportError it would
    # have got anyway, not a module that only has resource_filename.
    with pytest.raises(ImportError):
        import pkg_resources  # noqa: F401
    assert sys.modules["pkg_resources"] is None


def test_a_working_pkg_resources_is_left_alone(monkeypatch):
    real = types.ModuleType("pkg_resources")
    monkeypatch.setitem(sys.modules, "pkg_resources", real)

    with attribution._pkg_resources_for_booknlp_imports():
        import pkg_resources
        assert pkg_resources is real

    assert sys.modules["pkg_resources"] is real


def test_booknlp_modules_load_before_anything_patches_them(monkeypatch):
    calls = []

    class _StopBeforeBookNLP(Exception):
        pass

    def first_patch():
        calls.append("first patch")
        raise _StopBeforeBookNLP

    monkeypatch.setattr(attribution, "_ensure_booknlp_models", lambda size: calls.append("models"))
    monkeypatch.setattr(attribution, "_load_booknlp_modules", lambda: calls.append("load modules"))
    monkeypatch.setattr(attribution, "_patch_booknlp_windows_paths", first_patch)

    with pytest.raises(_StopBeforeBookNLP):
        attribution._get_booknlp_model("small")

    assert calls == ["models", "load modules", "first patch"]
