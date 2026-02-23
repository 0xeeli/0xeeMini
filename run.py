#!/usr/bin/env python3
# ─────────────────────────────────────────────────────
# 0xeeMini — Launcher
# Nécessaire car le package '0xeemini' commence par un chiffre
# et ne peut pas être lancé via `python -m 0xeemini.main`
# ─────────────────────────────────────────────────────
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

mod = importlib.import_module("0xeemini.main")
mod.main()
