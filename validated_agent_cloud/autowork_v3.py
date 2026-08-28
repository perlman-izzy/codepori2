#!/usr/bin/env python3
"""V3 launcher: preserve v2 and fix its repo-root import-path failure."""

from __future__ import annotations

from pathlib import Path
import runpy
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Execute the preserved v2 wrapper as __main__ after establishing package identity.
runpy.run_module("validated_agent_cloud.autowork_v2", run_name="__main__")
