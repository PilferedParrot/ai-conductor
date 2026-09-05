"""Run PilferedParrot from a source checkout with ``python -m pilferedparrot``."""

import sys

if sys.platform == "win32":
    from .windows import main
else:
    from .cli import main

raise SystemExit(main())
