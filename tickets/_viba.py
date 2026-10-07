"""Make the vendored `viba` importable, whatever the working directory is.

`viba` is a submodule bundled under its own root directory, so its package root
is not on `sys.path` by default. Every module here imports this one first.
"""

from __future__ import annotations

import sys
from pathlib import Path

VENDORED_VIBA = Path(__file__).resolve().parent.parent / "viba"

if ((VENDORED_VIBA / "viba" / "viba_type_descriptor.py").is_file()
        and str(VENDORED_VIBA) not in sys.path):
    sys.path.insert(0, str(VENDORED_VIBA))
