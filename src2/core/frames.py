"""The frame layer 1 shows the VLM: a live RViz grab, or a saved frame.

Live capture is best-effort (it needs RViz on a reachable $DISPLAY). Without one
- a headless box, or someone just replaying the pipeline - the source falls back
to the recorded frames in eval/turn_captures, cycling through them turn by turn,
so layer 1 is still a real image-grounded VLM call and never a text-only stub.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

from core import srcpath  # noqa: F401  # puts ../src on sys.path for the import below
from utils.vision import capture, rviz_bbox   # noqa: E402

logger = logging.getLogger(__name__)


class FrameSource:
    """Grabs the RViz window, falling back to a directory of recorded frames."""

    def __init__(self, window: str = "narration_chase", fallback_dir: str | Path | None = None,
                 live: bool = True) -> None:
        """Resolve the RViz window once and index the fallback frames.

        Args:
            window (str, optional): substring of the RViz window title to grab.
            fallback_dir (str | Path | None, optional): directory of JPEG/PNG frames
                used when the live grab fails.
            live (bool, optional): False skips the screen grab entirely.
        """
        self.window = window
        self._bbox = rviz_bbox(window) if live else None
        self._fallbacks = self._index(fallback_dir)
        self._next = 0
        # A missing RViz window would make capture() grab the whole desktop, which
        # is not the car's view: prefer the recorded frames whenever there are any.
        self._live = live and (self._bbox is not None or not self._fallbacks)
        if live and self._bbox is None:
            logger.info("no RViz window %r; layer 1 uses %d recorded frame(s)%s",
                        window, len(self._fallbacks),
                        "" if self._fallbacks else " - falling back to a full-screen grab")

    @staticmethod
    def _index(directory: str | Path | None) -> list[Path]:
        """Return the sorted image files in `directory` (empty when unusable)."""
        if not directory:
            return []
        path = Path(directory)
        if not path.is_dir():
            logger.warning("fallback frame directory %s does not exist", path)
            return []
        frames = sorted(p for p in path.iterdir()
                        if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
        # eval/turn_captures holds a BEV and a front frame per turn; layer 1 narrates
        # the forward view, so keep the front ones whenever the directory has them.
        return [p for p in frames if "front" in p.name.lower()] or frames

    def grab(self) -> tuple[str | None, str]:
        """Return (base64 JPEG, origin) for this event; (None, "none") if neither works.

        Returns:
            tuple[str | None, str]: the frame and where it came from ("rviz:<window>",
            the fallback file name, or "none").
        """
        if self._live:
            frame = capture(bbox=self._bbox)
            if frame:
                return frame, f"rviz:{self.window}" if self._bbox else "screen"
        if not self._fallbacks:
            return None, "none"
        path = self._fallbacks[self._next % len(self._fallbacks)]
        self._next += 1
        return base64.b64encode(path.read_bytes()).decode("ascii"), path.name
