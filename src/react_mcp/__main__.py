"""Allow ``python -m react_mcp`` and ``react-mcp`` to share one entrypoint."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
