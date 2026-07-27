"""Grab the current RViz view as a base64 JPEG for the vision-language model.

The narrator captures one frame when an event triggers and feeds it to the VLM
so the narration can be grounded in what the passenger sees. Capture is
BEST-EFFORT: on any failure (no X display, screensaver, ...) capture() returns
None and the pipeline narrates text-only.

Uses Pillow's ImageGrab, so the narrator process just has to share the $DISPLAY
that RViz renders on (launch/narration.sh exports it).
"""
from __future__ import annotations

import base64
import io
import logging
import re
import subprocess

logger = logging.getLogger(__name__)


def rviz_bbox(name: str = "rviz") -> tuple[int, int, int, int] | None:
    """Return the RViz window's screen bbox via xwininfo, else None.

    Lets capture() grab ONLY the RViz window (with its planned-path overlay)
    instead of the whole screen. Returns None if xwininfo is missing or no
    matching window is found, so the caller falls back to a full-screen grab.

    Args:
        name (str): case-insensitive substring of the RViz window title.

    Returns:
        tuple[int, int, int, int] | None: (left, top, right, bottom), or None.
    """
    try:
        tree = subprocess.run(["xwininfo", "-root", "-tree"],
                              capture_output=True, text=True, timeout=5).stdout
        win_id = next((m.group(1) for line in tree.splitlines()
                       if name.lower() in line.lower()
                       for m in [re.search(r"(0x[0-9a-fA-F]+)", line)] if m), None)
        if not win_id:
            logger.warning("no RViz window matching %r; grabbing the full screen.", name)
            return None
        info = subprocess.run(["xwininfo", "-id", win_id],
                              capture_output=True, text=True, timeout=5).stdout
        left = int(re.search(r"Absolute upper-left X:\s*(-?\d+)", info).group(1))
        top = int(re.search(r"Absolute upper-left Y:\s*(-?\d+)", info).group(1))
        width = int(re.search(r"Width:\s*(\d+)", info).group(1))
        height = int(re.search(r"Height:\s*(\d+)", info).group(1))
        return (left, top, left + width, top + height)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("could not resolve the RViz window (%s); grabbing full screen.", exc)
        return None


def capture(bbox: tuple[int, int, int, int] | None = None, max_dim: int = 1280,
            downscale: float = 2.0, quality: int = 80) -> str | None:
    """Return the current screen (or bbox region) as a base64 JPEG, else None.

    Args:
        bbox (tuple[int, int, int, int] | None): optional (left, top, right,
            bottom) crop, e.g. the RViz monitor; None grabs the whole screen.
        max_dim (int): cap on the longest edge before downscale is applied.
        downscale (float): shrink the frame by this factor for faster inference
            (2.0 -> half width & height).
        quality (int): JPEG quality (1-95).

    Returns:
        str | None: raw base64 JPEG (no data: prefix), or None on failure.
    """
    try:
        from PIL import ImageGrab

        img = ImageGrab.grab(bbox=bbox).convert("RGB")
        width, height = img.size
        scale = min(max_dim / max(width, height), 1.0) / max(downscale, 1.0)
        if scale < 1.0:
            img = img.resize((max(1, round(width * scale)), max(1, round(height * scale))))
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality)
        return base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("RViz image capture failed (%s); narrating text-only.", exc)
        return None
