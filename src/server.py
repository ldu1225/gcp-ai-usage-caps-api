#!/usr/bin/env python3
"""Entrypoint for the headless GCP AI Usage & Spend Caps API server."""

import os
import sys

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from usage_caps_api.app import run_server  # noqa: E402

if __name__ == "__main__":
    run_server()
