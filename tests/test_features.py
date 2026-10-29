"""The features package keeps its shape: a feature sees the app only through
its Context, never another feature or the controller."""

import ast
import unittest
from pathlib import Path

FEATURES = Path(__file__).resolve().parent.parent / "awaitray" / "features"
SHARED = {"__init__", "context"}


def _imports(path):
    """Module names a file imports, resolved inside awaitray (".x" → "x")."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level == 1:  # from .x import y / from . import x
                yield from (f"features.{base or a.name}" for a in node.names)
            elif node.level == 2:  # from ..x import y / from .. import x
                yield from ([base] if base else [a.name for a in node.names])
            elif base.startswith("awaitray."):
                yield base.removeprefix("awaitray.")
        elif isinstance(node, ast.Import):
            yield from (a.name.removeprefix("awaitray.") for a in node.names)


class FeatureBoundaryTests(unittest.TestCase):
    def test_features_import_neither_each_other_nor_the_controller(self):
        features = {p.stem for p in FEATURES.glob("*.py")} - SHARED
        self.assertTrue(features)
        for name in sorted(features):
            imported = set(_imports(FEATURES / f"{name}.py"))
            others = {f"features.{f}" for f in features - {name}}
            with self.subTest(feature=name):
                self.assertFalse(imported & others, "use a Context callable instead")
                self.assertNotIn("controller", imported)
                self.assertFalse({"backends", "gi", "AppKit", "objc"} & imported)


if __name__ == "__main__":
    unittest.main()
