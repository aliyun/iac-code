#!/usr/bin/env python3
from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

main = import_module("scripts.observability.local_observe.cli").main

if __name__ == "__main__":
    main()
