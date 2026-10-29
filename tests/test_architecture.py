"""Layer rules, checked on the real import graph (scripts/arch_graph.py).

Each rule names what it protects; a broken one fails with the offending
import. Every rule also has a test proving it fires on a fake module.
"""

import importlib.util
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("arch_graph", ROOT / "scripts" / "arch_graph.py")
arch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arch)

TOOLKITS = {"gi", "AppKit", "Foundation", "objc", "PyObjCTools"}
# Core modules that may start processes or threads; the rest stay pure.
SIDE_EFFECTS = {
    "github",
    "repos",
    "repo_sync",
    "ai_usage",
    "browser",
    "backup",
    "runner",
    "log",  # its lock
    "features.links",  # launches the editor
}
MODELS = {"menu_model", "prefs_model"}
SHARED_FEATURE_MODULES = {"features", "features.context"}


def violations(graph):
    """Every broken rule, as "module: what it imports — the rule"."""
    out = []
    for name, (deps, ext) in sorted(graph.items()):
        layer = arch.layer(name)
        if layer != "backends":
            for dep in sorted(d for d in deps if d.startswith("backends.")):
                out.append(f"{name}: imports {dep} — only backends.select() picks a backend")
            for mod in sorted(ext & TOOLKITS):
                out.append(f"{name}: imports {mod} — the core never touches a toolkit")
            for mod in sorted(ext & {"subprocess", "threading"}):
                if name not in SIDE_EFFECTS:
                    out.append(f"{name}: imports {mod} — keep pure modules pure")
        else:
            platform = name.split(".")[1] if "." in name else None
            for dep in sorted(d for d in deps if d.startswith("backends.")):
                if platform and dep.split(".")[1] != platform:
                    out.append(f"{name}: imports {dep} — a backend never uses another")
        if name in MODELS and "controller" in deps:
            out.append(f"{name}: imports controller — models are built from data")
        if layer == "features" and name not in SHARED_FEATURE_MODULES:
            if "controller" in deps:
                out.append(f"{name}: imports controller — features use their Context")
            for dep in sorted(
                d for d in deps if arch.layer(d) == "features" and d not in SHARED_FEATURE_MODULES
            ):
                out.append(f"{name}: imports {dep} — features don't import each other")
    return out


class RealPackageTests(unittest.TestCase):
    def test_no_layer_rule_is_broken(self):
        self.assertEqual(violations(arch.scan()), [])

    def test_the_graph_in_the_docs_is_current(self):
        self.assertEqual(arch.main(["--check"]), 0, "run python3 scripts/arch_graph.py")


class RulesFireTests(unittest.TestCase):
    """A fake package with one offending module per rule."""

    def _violations(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "awaitray"
            for rel, src in {"__init__.py": "", "controller.py": "", **files}.items():
                path = pkg / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(textwrap.dedent(src))
            return violations(arch.scan(pkg))

    def _fires(self, files, expected):
        found = self._violations(files)
        self.assertTrue(any(expected in v for v in found), found)

    def test_core_never_picks_a_backend(self):
        self._fires(
            {
                "backends/__init__.py": "",
                "backends/linux/__init__.py": "",
                "backends/linux/app.py": "",
                "cli.py": "from .backends.linux import app\n",
            },
            "cli: imports backends.linux.app",
        )

    def test_core_never_touches_a_toolkit(self):
        self._fires({"icon.py": "import gi\n"}, "icon: imports gi")
        self._fires({"icon.py": "from AppKit import NSImage\n"}, "icon: imports AppKit")

    def test_pure_modules_start_no_process(self):
        self._fires({"pr_state.py": "import subprocess\n"}, "pr_state: imports subprocess")
        self.assertEqual(self._violations({"github.py": "import subprocess\n"}), [])

    def test_models_dont_know_the_controller(self):
        self._fires({"menu_model.py": "from .controller import Controller\n"}, "menu_model")
        self._fires({"prefs_model.py": "def f():\n    from . import controller\n"}, "prefs_model")

    def test_features_stay_apart(self):
        files = {
            "features/__init__.py": "",
            "features/context.py": "",
            "features/links.py": "from .context import Context\n",
            "features/inbox.py": "from .links import Links\n",
        }
        self._fires(files, "features.inbox: imports features.links")
        self._fires(
            {**files, "features/inbox.py": "from ..controller import Controller\n"},
            "features.inbox: imports controller",
        )

    def test_a_backend_never_uses_another(self):
        self._fires(
            {
                "backends/__init__.py": "",
                "backends/linux/__init__.py": "",
                "backends/linux/app.py": "from ..macos import menu\n",
                "backends/macos/__init__.py": "",
                "backends/macos/menu.py": "",
            },
            "backends.linux.app: imports backends.macos.menu",
        )


if __name__ == "__main__":
    unittest.main()
