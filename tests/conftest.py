"""Prevent network-backed runtime installation during deterministic tests."""

import os

os.environ["PAGEFETCH_AUTO_INSTALL"] = "0"

