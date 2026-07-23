"""Entry point for ``python -m pagefetch``.

Without arguments, launches the interactive CLI menu.
Use ``python -m pagefetch --cli [args...]`` for the argparse-based command-line interface.
"""

import sys

if "--cli" in sys.argv:
    sys.argv.remove("--cli")
    from .cli import main
    raise SystemExit(main())
else:
    from .interactive import interactive_main
    raise SystemExit(interactive_main())

