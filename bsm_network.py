#!/usr/bin/env python3
"""Entrypoint for BSM network controller."""

from __future__ import annotations

import sys

from bsm_network.main import main


if __name__ == "__main__":
    sys.exit(main())
