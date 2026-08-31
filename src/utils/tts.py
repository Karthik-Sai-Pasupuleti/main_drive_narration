"""Non-blocking text-to-speech with a fallback chain.

make_tts() returns the best backend available: Kokoro (neural, on CPU) ->
espeak-ng (system fallback) -> silent. All share one contract: speak(text)
returns immediately and preempts whatever is playing (latest wins);
play_file(wav) plays a pre-rendered cue and outranks speak() so a scripted line
is never cut off by LLM narration; close() stops playback.

synth_to_file() renders text to a WAV without playing it - used by
src/utils/prerender.py to build the timestamped cue cache.
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

SAMPLE_RATE = 24_000     # Kokoro output sample rate (Hz)
PRIORITY_NARRATION = 0   # LLM narration: dropped while a cue is speaking
PRIORITY_CUE = 1         # scripted cue from cue_publisher.py: always wins

_PIPELINES: dict[str, object] = {}   # lang_code -> KPipeline (the model loads once)


def _pipeline(lang_code: str = "a"):
    """Return a cached Kokoro pipeline for `lang_code`.

    Args:
        lang_code (str): Kokoro language code.

    Returns:
        KPipeline: the shared pipeline for that language.

    Raises:
        ImportError: if the kokoro package is not installed.
    """
    if KPipeline is None:
        raise ImportError("kokoro is not installed")
    if lang_code not in _PIPELINES:
        _PIPELINES[lang_code] = KPipeline(lang_code=lang_code, device="cpu")
    return _PIPELINES[lang_code]


def _render(text: str, voice: str, speed: float, lang_code: str) -> np.ndarray:
    """Synthesize text to int16 PCM at SAMPLE_RATE (empty array if Kokoro yields nothing)."""
    chunks = [np.asarray(result.audio, dtype=np.float32)
              for result in _pipeline(lang_code)(text, voice=voice, speed=speed)]
    if not chunks:
        return np.zeros(0, dtype=np.int16)
    return (np.clip(np.concatenate(chunks), -1.0, 1.0) * 32767).astype(np.int16)


def _write_wav(path: str | Path, pcm: np.ndarray) -> float:
    """Write int16 PCM as a mono WAV; return its duration in seconds."""
    # pylint: disable=no-member  # wave.open("wb") returns Wave_write; astroid mis-infers
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return len(pcm) / SAMPLE_RATE


def synth_to_file(text: str, path: str | Path, voice: str = "af_heart",
                  speed: float = 1.0, lang_code: str = "a") -> float:
    """Render text to a mono WAV on disk without playing it.

    Args:
        text (str): the line to synthesize.
        path (str | Path): destination WAV (parent directories are created).
        voice (str, optional): Kokoro voice id. Defaults to "af_heart".
        speed (float, optional): speaking rate multiplier. Defaults to 1.0.
        lang_code (str, optional): Kokoro language code. Defaults to "a".

    Returns:
        float: duration of the written audio in seconds.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return _write_wav(path, _render(text, voice, speed, lang_code))


def wav_duration(path: str | Path) -> float:
    """Return the duration of an existing WAV file in seconds."""
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())


class TextToSpeech:
    """Kokoro TTS worker: latest-wins within a priority, cues outrank narration."""

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
        self._voice, self._speed, self._lang = voice, speed, lang_code
        self._cond = threading.Condition()
        self._pending: tuple[int, str, bool] | None = None  # (priority, payload, is_file)
        self._active = -1                           # priority of what plays (-1 = idle)
        self._closed = False
        self._proc: subprocess.Popen | None = None  # current aplay process
        self._idle = threading.Event()              # set when nothing is playing/queued
        self._idle.set()
        _pipeline(lang_code)                        # raises ImportError if kokoro is absent
        self._thread = threading.Thread(target=self._run, name="tts", daemon=True)
        self._thread.start()

    def speak(self, text: str, priority: int = PRIORITY_NARRATION) -> None:
        """Queue text as the next line to play, preempting the current one.

        Args:
            text (str): the line to speak; blank text is ignored.
            priority (int, optional): PRIORITY_NARRATION or PRIORITY_CUE.
        """
        if text.strip():
            self._submit(text, False, priority)

    def play_file(self, path: str, priority: int = PRIORITY_CUE) -> None:
        """Play a pre-rendered WAV (no synthesis), preempting lower-priority audio.

        Args:
            path (str): WAV file to play.
            priority (int, optional): defaults to PRIORITY_CUE.
        """
        self._submit(path, True, priority)

    def _submit(self, payload: str, is_file: bool, priority: int) -> None:
        """Hand a line or WAV to the worker, or drop it if outranked."""
        with self._cond:
            outranked = (priority < self._active
                         or (self._pending is not None and priority < self._pending[0]))
            if outranked:
                logger.info("higher-priority audio playing; dropping %.70s", payload)
                return
            self._idle.clear()
            self._pending = (priority, payload, is_file)   # replaces any waiting item
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()    # cut off the line currently playing
            self._cond.notify()

    def wait(self, timeout: float | None = None) -> None:
        """Block until the current line has finished playing (or timeout)."""
        self._idle.wait(timeout)

    def _run(self) -> None:
        """Worker loop: play the latest pending item until closed."""
        while True:
            with self._cond:
                while self._pending is None and not self._closed:
                    self._active = -1
                    self._idle.set()      # nothing playing/queued
                    self._cond.wait()
                if self._closed:
                    return
                priority, payload, is_file = self._pending
                self._pending, self._active = None, priority
            try:
                self._play(payload, is_file)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("TTS failed: %s", exc)

    def _play(self, payload: str, is_file: bool) -> None:
        """Play a pre-rendered WAV, or synthesize `payload` into a temp WAV first.

        Args:
            payload (str): WAV path when is_file, else the line to synthesize.
            is_file (bool): whether payload is an existing WAV path.
        """
        if is_file:
            self._aplay(payload)
            return
        pcm = _render(payload, self._voice, self._speed, self._lang)
        if not len(pcm):
            return
        with NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = handle.name
        try:
            _write_wav(path, pcm)
            self._aplay(path)
        finally:
            Path(path).unlink(missing_ok=True)

    def _aplay(self, path: str) -> None:
        """Play a WAV with aplay, stopping early if preempted or closed."""
        with subprocess.Popen(["aplay", "-q", path]) as self._proc:
            while self._proc.poll() is None:
                with self._cond:
                    preempted = self._pending is not None or self._closed
                if preempted:
                    self._proc.terminate()
                    break
                time.sleep(0.05)

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
        self._active = -1

    def speak(self, text: str, priority: int = PRIORITY_NARRATION) -> None:
        """Speak text via espeak-ng, terminating any lower-priority line playing.

        Args:
            text (str): the line to speak; blank text is ignored.
            priority (int, optional): PRIORITY_NARRATION or PRIORITY_CUE.
        """
        if text.strip():
            self._start(["espeak-ng", "-s", str(self._rate), text], priority)

    def play_file(self, path: str, priority: int = PRIORITY_CUE) -> None:
        """Play a pre-rendered WAV with aplay (espeak is not involved).

        Args:
            path (str): WAV file to play.
            priority (int, optional): defaults to PRIORITY_CUE.
        """
        self._start(["aplay", "-q", path], priority)

    def _start(self, argv: list[str], priority: int) -> None:
        """Run argv, unless something higher-priority is still playing."""
        playing = self._proc is not None and self._proc.poll() is None
        if playing and priority < self._active:
            return
        if playing:
            self._proc.terminate()
        self._active = priority
        self._proc = subprocess.Popen(argv)  # pylint: disable=consider-using-with

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

    def speak(self, text: str, priority: int = PRIORITY_NARRATION) -> None:  # noqa: D102
        """Ignore the line (no audio).

        Args:
            text (str): unused.
            priority (int, optional): unused.
        """
        pass

    def play_file(self, path: str, priority: int = PRIORITY_CUE) -> None:  # noqa: D102
        """Ignore the cue (no audio).

        Args:
            path (str): unused.
            priority (int, optional): unused.
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
