"""Renders every narration line to a WAV *next to its text file*, then plays it.

Both deliverables come from the same call, which is the point: layer 1 and layer
3 each end up with `<name>.txt` and `<name>.wav` side by side in that layer's
folder. Rendering chain: Kokoro (neural) -> espeak-ng -> text only; playback goes
through the shared backend in src/utils/tts.py so one process owns the speaker.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from core import srcpath  # noqa: F401  # puts ../src on sys.path for the import below
from utils.tts import (PRIORITY_CUE, make_tts, synth_to_file,  # noqa: E402
                       wav_duration)

logger = logging.getLogger(__name__)


class Speaker:
    """Text -> WAV on disk -> playback, with the WAV kept as the run artifact."""

    def __init__(self, play: bool = True, voice: str = "af_heart", speed: float = 1.0,
                 lang_code: str = "a", espeak_rate: int = 160) -> None:
        """Pick the playback backend and remember the voice settings.

        Args:
            play (bool, optional): False still writes the WAVs but plays nothing.
            voice (str, optional): Kokoro voice id.
            speed (float, optional): Kokoro speaking-rate multiplier.
            lang_code (str, optional): Kokoro language code.
            espeak_rate (int, optional): espeak-ng rate (wpm) for the fallback.
        """
        self._voice, self._speed, self._lang = voice, speed, lang_code
        self._espeak_rate = espeak_rate
        self._backend = make_tts(play)

    def say(self, text: str, wav_path: str | Path, wait_s: float = 120.0) -> float:
        """Render `text` to `wav_path`, play it, and return its duration in seconds.

        Args:
            text (str): the line to speak.
            wav_path (str | Path): WAV to write (parent directories are created).
            wait_s (float, optional): how long to wait for playback to finish.

        Returns:
            float: duration of the rendered audio, or 0.0 if nothing could be rendered.
        """
        duration = self._render(text, Path(wav_path))
        if duration <= 0.0:
            self._backend.speak(text)      # no WAV: at least say it if a backend can
            self._backend.wait(wait_s)
            return 0.0
        self._backend.play_file(str(wav_path), priority=PRIORITY_CUE)
        self._backend.wait(wait_s)
        return duration

    def _render(self, text: str, path: Path) -> float:
        """Write `text` to `path` with Kokoro, else espeak-ng; 0.0 if neither works."""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return synth_to_file(text, path, self._voice, self._speed, self._lang)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("Kokoro unavailable (%s); trying espeak-ng", exc)
        if not shutil.which("espeak-ng"):
            logger.warning("no TTS backend available; %s will have no audio", path.name)
            return 0.0
        try:
            subprocess.run(["espeak-ng", "-s", str(self._espeak_rate), "-w", str(path), text],
                           check=True, capture_output=True, timeout=60)
            return wav_duration(path)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("espeak-ng could not render %s (%s)", path.name, exc)
            return 0.0

    def close(self) -> None:
        """Stop any playback and release the backend."""
        self._backend.close()
