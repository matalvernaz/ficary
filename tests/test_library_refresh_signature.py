"""CI guardrail: the GUI's Check-for-Updates flow drives
``cli._download_one`` via the Namespace built by
``library.refresh.default_refresh_args``. Adding a required arg to
``_download_one`` without updating ``default_refresh_args`` produces an
opaque AttributeError at GUI-update time — the user sees "Update
failed" with no diagnostic.

This test inspects the :class:`ficary.jobs.DownloadJob` produced by
``default_refresh_args`` and asserts its field set covers every
attribute ``_download_one`` actually reads off ``args``. Since the
round-10 refactor the schema lives in one dataclass instead of a
hand-maintained fake Namespace; this canary now polices that schema.

Best-effort: extracts attribute names by static AST scan of
``_download_one``'s body looking for ``args.<name>`` accesses. False
positives (e.g. ``args`` is a dict-keyed local elsewhere) are scored
as "expected attribute" — over-strict is the right failure mode here.
"""

from __future__ import annotations

import ast
import inspect

import ficary.cli as cli_module
from ficary.library.refresh import default_refresh_args


def _attrs_read_from(func_obj) -> set[str]:
    """Return every ``args.<name>`` attribute access inside ``func_obj``."""
    import textwrap
    source = textwrap.dedent(inspect.getsource(func_obj))
    tree = ast.parse(source)
    accessed: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "args"
        ):
            accessed.add(node.attr)
    return accessed


def test_default_refresh_args_covers_download_one_reads() -> None:
    """Every ``args.<X>`` ``_download_one`` reads must be present on
    the Namespace returned by ``default_refresh_args``. Catches the
    silent-AttributeError regression the GUI surfaces as opaque
    "Update failed"."""
    download_one = cli_module._download_one
    reads = _attrs_read_from(download_one)

    ns = default_refresh_args()
    provided = set(vars(ns).keys())

    missing = reads - provided
    assert not missing, (
        "DownloadJob is missing field(s) that cli._download_one reads "
        f"off args: {sorted(missing)}. Add them to ficary.jobs."
        "DownloadJob with the argparse default."
    )


def test_download_job_covers_build_scraper_reads() -> None:
    """Same guarantee for ``_build_scraper`` — it reads the scraper-
    construction fields (cookies, fichub, cf_solve, delays) and used to
    rely on getattr defaults that silently masked missing fields."""
    reads = _attrs_read_from(cli_module._build_scraper)
    from ficary.jobs import DownloadJob
    provided = set(DownloadJob().__dataclass_fields__)
    missing = reads - provided
    assert not missing, (
        "DownloadJob is missing field(s) that cli._build_scraper reads "
        f"off args: {sorted(missing)}. Add them to ficary.jobs."
        "DownloadJob with the argparse default."
    )
