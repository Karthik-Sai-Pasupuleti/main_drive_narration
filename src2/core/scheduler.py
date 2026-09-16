"""The drive clock: fires the scripted cues of the 120 s run at their timestamps.

This replaces the rosbag. Instead of reading events out of a recording, every
layer registers what it wants to happen and when (a turn at 15 s, the infra-pole
report at 30 s, ...) and the timeline fires them against the wall clock.

`speed` scales the clock: --speed 10 replays the same 120 s script in 12 s, which
is how the wiring gets tested without sitting through a full drive. Cue actions
receive the *drive* time, so the logs read the same at any speed.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(order=True)
class Cue:
    """One scheduled action: `action(drive_time)` fired `at` seconds into the drive."""

    at: float
    label: str = field(compare=False)
    action: Callable[[float], None] = field(compare=False)


class Timeline:
    """Sorted cue list played back against the wall clock."""

    def __init__(self, speed: float = 1.0) -> None:
        """Create an empty timeline.

        Args:
            speed (float, optional): clock multiplier (2.0 = twice as fast).
        """
        self.speed = max(speed, 0.01)
        self._cues: list[Cue] = []

    def add(self, at: float, label: str, action: Callable[[float], None]) -> None:
        """Schedule `action` at `at` seconds of drive time.

        Args:
            at (float): drive time in seconds.
            label (str): short name shown in the run log.
            action (Callable[[float], None]): called with the drive time.
        """
        self._cues.append(Cue(float(at), label, action))

    @property
    def cues(self) -> list[Cue]:
        """The scheduled cues, earliest first."""
        return sorted(self._cues)

    def run(self, duration_s: float) -> float:
        """Play the cues, then hold until `duration_s` of drive time has passed.

        Args:
            duration_s (float): total drive length in seconds.

        Returns:
            float: drive time actually elapsed when the run ended.
        """
        start = time.monotonic()
        elapsed = lambda: (time.monotonic() - start) * self.speed   # drive seconds
        for cue in self.cues:
            if cue.at > duration_s:
                logger.info("skipping %s @%.1fs (past the %.0fs drive)", cue.label,
                            cue.at, duration_s)
                continue
            self._sleep_until(cue.at, start)
            logger.info("t=%6.1fs  %s", cue.at, cue.label)
            cue.action(cue.at)
        self._sleep_until(duration_s, start)
        return elapsed()

    def _sleep_until(self, drive_time: float, start: float) -> None:
        """Block until `drive_time` of (speed-scaled) drive time has elapsed."""
        remaining = drive_time / self.speed - (time.monotonic() - start)
        if remaining > 0:
            time.sleep(remaining)
