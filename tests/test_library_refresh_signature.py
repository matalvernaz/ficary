"""CI guardrail: the GUI's Check-for-Updates flow drives
``cli._download_one`` via the Namespace built by
``library.refresh.default_refresh_args``. Adding a required arg to
``_download_one`` without updating ``default_refresh_args`` produces an
opaque AttributeError at GUI-update time — the user sees "Update
failed" with no diagnostic.

This test inspects the Namespace produced by ``default_refresh_args``
and asserts the set of attributes it carries covers every attribute
``_download_one`` actually reads off ``args``. Catching the regression
in CI is cheaper than reproducing the GUI failure.

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
    source = inspect.getsource(func_obj)
    # The first line's indent has to be normalized so ast.parse accepts
    # the (typically) 4-space-indented body.
    source = inspect.cleandoc(source)
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
        "default_refresh_args is missing attribute(s) that "
        "cli._download_one reads off args: "
        f"{sorted(missing)}. Either add them to default_refresh_args "
        "with sensible defaults, or refactor _download_one to take a "
        "schema dataclass (see audit notes for the v2.5.0 plan)."
    )
