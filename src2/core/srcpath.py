"""Puts the sibling ``src/`` package directory on ``sys.path``.

``src2`` deliberately re-uses the parts of ``src/`` that have nothing to do with
ROS - the LLM bot (``bot/bot.py``), the TTS backends (``utils/tts.py``) and the
RViz screen grab (``utils/vision.py``) - instead of copying them. Those modules
import each other as top-level packages (``from utils.utils import ...``), so
``src/`` itself has to be importable, exactly as ``main.py`` arranges it.

Only ``core.llm``, ``core.speech`` and ``core.frames`` import this module; the
layers import those wrappers, so this path shim lives in exactly one place.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # repo root (main_drive_narration/)
SRC2 = ROOT / "src2"
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
