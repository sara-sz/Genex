"""Source-level isolation guarantees for the therapist service.

Static checks that the service code never imports genex_core, never imports the
parent `api` package, and never references parent GCS storage/buckets.
"""

from __future__ import annotations

import ast
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"


def _py_files():
    return sorted(APP_DIR.rglob("*.py"))


def _imported_names(path: pathlib.Path):
    """Return module names, considering ALL imports (absolute + relative)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def _absolute_imported_names(path: pathlib.Path):
    """Return only ABSOLUTE module names (relative imports refer to app.* itself).

    A relative `from .api ...`/`from ..api ...` targets the therapist service's
    OWN `app.api` subpackage, never the parent HTTP `api` package (which is only
    reachable via an absolute import).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.append(node.module or "")
    return names


def test_no_genex_core_import():
    for path in _py_files():
        for mod in _imported_names(path):
            assert not mod.startswith("genex_core"), f"{path} imports genex_core"


def test_no_parent_api_import():
    # The parent HTTP layer is the top-level (absolute) package `api`. Relative
    # `.api`/`..api` imports are the therapist service's own subpackage and are fine.
    for path in _py_files():
        for mod in _absolute_imported_names(path):
            root = mod.split(".")[0]
            assert root != "api", f"{path} imports parent 'api' package ({mod})"


def test_no_parent_gcs_or_storage_references():
    needles = [
        "google.cloud.storage",
        "from google.cloud import storage",
        "genex-api-prod-sessions",
        "genex-api-dev-sessions",
        "genex-parent-sessions",
        "sessions/{uid}",
    ]
    for path in _py_files():
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, f"{path} references parent GCS/storage: {needle}"


def test_no_genex_core_string_anywhere_in_app():
    for path in _py_files():
        text = path.read_text(encoding="utf-8")
        # Allow the word in comments explaining we DON'T use it, but not as an import
        # path token like 'genex_core.'. Enforce absence of the dotted token.
        assert "genex_core." not in text, f"{path} references genex_core.*"
