"""Run with `python -m awaitray`.

No arguments: start the tray app (platform backend). With arguments: the
command-line interface (`python -m awaitray --help`).
"""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from . import cli  # the CLI never imports a GUI toolkit

        return cli.main(sys.argv[1:])
    from . import backends
    from . import config as cfg_mod

    cfg_mod.drop_legacy_cache()
    backends.select().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
