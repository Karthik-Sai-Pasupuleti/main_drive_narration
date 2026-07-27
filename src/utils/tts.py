"""Non-blocking text-to-speech with a fallback chain.

make_tts() returns the best backend available: Kokoro (neural, on CPU) ->
espeak-ng (system fallback) -> silent. All share one contract: speak(text)
returns immediately and preempts whatever is playing (latest wins); close()
stops playback.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np

try:
    from kokoro import KPipeline  # heavy, optional; make_tts() falls back if absent
except ImportError:
    KPipeline = None

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24_000  # Kokoro output sample rate (Hz)


class TextToSpeech:
    """Latest-wins Kokoro TTS: a new line interrupts whatever is playing."""

    enabled = True

    def __init__(self, voice: str = "af_heart", speed: float = 1.0,
                 lang_code: str = "a") -> None:
        """Start the Kokoro pipeline and the background playback thread.

        Args:
            voice (str): Kokoro voice id.
            speed (float): speaking rate multiplier.
            lang_code (str): Kokoro language code.

        Raises:
            ImportError: if the kokoro package is not installed.
        """
        self._synth = {"voice": voice, "speed": speed}
        self._cond = threading.Condition()
        self._pending: str | None = None            # next line to play (latest wins)
        self._closed = False
        self._proc: subprocess.Popen | None = None  # current aplay process
        self._idle = threading.Event()              # set when nothing is playing/queued
        self._idle.set()
        if KPipeline is None:
            raise ImportError("kokoro is not installed")
        self._pipeline = KPipeline(lang_code=lang_code, device="cpu")
        self._thread = threading.Thread(target=self._run, name="tts", daemon=True)
        self._thread.start()

    def speak(self, text: str) -> None:
        """Queue text as the next line to play, preempting the current one.

        Args:
            text (str): the line to speak; blank text is ignored.
        """
        if not text.strip():
            return
        with self._cond:
            self._idle.clear()
            self._pending = text          # replaces any waiting line (latest wins)
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()    # cut off the line currently playing
            self._cond.notify()

    def wait(self, timeout: float | None = None) -> None:
        """Block until the current line has finished playing (or timeout)."""
        self._idle.wait(timeout)

    def _run(self) -> None:
        """Worker loop: play the latest pending line until closed."""
        while True:
            with self._cond:
                while self._pending is None and not self._closed:
                    self._idle.set()      # nothing playing/queued
                    self._cond.wait()
                if self._closed:
                    return
                text, self._pending = self._pending, None
            try:
                self._synthesize_and_play(text)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("TTS failed: %s", exc)

    def _synthesize_and_play(self, text: str) -> None:
        """Synthesize text with Kokoro and play it, stopping early if preempted.

        Args:
            text (str): the line to synthesize and play.
        """
        chunks = [np.asarray(result.audio, dtype=np.float32)
                  for result in self._pipeline(text, **self._synth)]
        if not chunks:
            return
        pcm = (np.clip(np.concatenate(chunks), -1.0, 1.0) * 32767).astype(np.int16)
        with NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = handle.name
        try:
            # pylint: disable=no-member  # wave.open("wb") returns Wave_write; astroid mis-infers
            with wave.open(path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(pcm.tobytes())
            with subprocess.Popen(["aplay", "-q", path]) as self._proc:
                while self._proc.poll() is None:
                    with self._cond:
                        preempted = self._pending is not None or self._closed
                    if preempted:
                        self._proc.terminate()
                        break
                    time.sleep(0.05)
        finally:
            Path(path).unlink(missing_ok=True)

    def close(self) -> None:
        """Stop the worker thread and any current playback."""
        with self._cond:
            self._closed = True
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
            self._cond.notify()
        self._thread.join(timeout=15.0)


class EspeakTTS:
    """Latest-wins espeak-ng fallback used when Kokoro isn't installed."""

    enabled = True

    def __init__(self, rate: int = 160) -> None:
        """Set up the espeak-ng backend.

        Args:
            rate (int): speaking rate in words per minute.
        """
        self._rate = rate
        self._proc: subprocess.Popen | None = None

    def speak(self, text: str) -> None:
        """Speak text via espeak-ng, terminating any line already playing.

        Args:
            text (str): the line to speak; blank text is ignored.
        """
        if not text.strip():
            return
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = subprocess.Popen(["espeak-ng", "-s", str(self._rate), text])

    def wait(self, timeout: float | None = None) -> None:
        """Block until the current line has finished playing (or timeout)."""
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.wait(timeout)
            except subprocess.TimeoutExpired:
                pass

    def close(self) -> None:
        """Stop any current playback."""
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


class MuteTTS:
    """No-op backend: narration is logged / overlaid but never spoken."""

    enabled = False

    def speak(self, text: str) -> None:  # noqa: D102
        """Ignore the line (no audio).

        Args:
            text (str): unused.
        """
        pass

    def wait(self, timeout: float | None = None) -> None:  # noqa: D102
        """No-op (nothing plays)."""
        pass

    def close(self) -> None:  # noqa: D102
        """Do nothing."""
        pass


def make_tts(enabled: bool = True):
    """Return the best available TTS backend (Kokoro -> espeak-ng -> silent).

    Args:
        enabled (bool, optional): if False, force the silent backend. Defaults to True.

    Returns:
        TextToSpeech | EspeakTTS | MuteTTS: the selected backend.
    """
    if not enabled:
        return MuteTTS()
    try:
        return TextToSpeech()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("Kokoro TTS unavailable (%s).", exc)
    if shutil.which("espeak-ng"):
        logger.info("Falling back to espeak-ng for speech.")
        return EspeakTTS()
    logger.warning("No TTS backend available; narration will be silent.")
    return MuteTTS()
