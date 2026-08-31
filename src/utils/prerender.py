#!/usr/bin/env python3
"""Pre-render the scripted audio cues in src/configs/audio_config/*.json to WAV.

Each JSON holds a `voice` plus `messages` of {id, at, text}. This renders every
message once with Kokoro, saves it under cache/audio/<file>/<id>_<hash>.wav and
writes cache/audio/manifest.json, which cue_publisher.py reads for the timeline
and main.py reads to resolve a cue key to a WAV.

Rendering ahead of time is what makes the `at` timestamps accurate: synthesizing
at trigger time would delay a cue by however long Kokoro takes on CPU.

    python src/utils/prerender.py                 # render what changed
    python src/utils/prerender.py --force         # re-render everything
    python src/utils/prerender.py --list          # show the timeline, render nothing

The cache key is a hash of (text, voice, speed, lang_code), so editing a line's
text in the JSON re-renders it automatically and the stale WAV is removed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Sibling import: src/utils/utils.py shadows the `utils` package whenever this
# script's own directory is on sys.path, so import tts directly.
from tts import SAMPLE_RATE, synth_to_file, wav_duration   # noqa: E402

CONFIG_DIR = ROOT / "src" / "configs" / "audio_config"
OUT_DIR = ROOT / "cache" / "audio"


def digest(text: str, voice: str, speed: float, lang_code: str) -> str:
    """Short hash of everything that changes the rendered audio."""
    payload = f"{text}|{voice}|{speed}|{lang_code}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:8]


def load_cues(config_dir: Path) -> list[dict]:
    """Read every *.json in `config_dir` into a flat list of cue dicts.

    Args:
        config_dir (Path): directory holding the audio-config JSON files.

    Returns:
        list[dict]: one dict per message, with the file-level voice/source merged in.

    Raises:
        ValueError: if a message is missing 'id' or 'text', or an id repeats in a file.
    """
    cues = []
    for path in sorted(config_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        voice = data.get("voice", "af_heart")
        speed = float(data.get("speed", 1.0))
        lang = data.get("lang_code", "a")
        seen = set()
        for msg in data.get("messages", []):
            if "id" not in msg or "text" not in msg:
                raise ValueError(f"{path.name}: message needs 'id' and 'text': {msg}")
            if msg["id"] in seen:
                raise ValueError(f"{path.name}: duplicate message id {msg['id']!r}")
            seen.add(msg["id"])
            cues.append({"key": f"{path.stem}/{msg['id']}",
                         "file": path.name,
                         "source": data.get("source", path.stem),
                         "id": msg["id"],
                         "at": msg.get("at"),
                         "text": msg["text"],
                         "voice": voice, "speed": speed, "lang_code": lang,
                         "hash": digest(msg["text"], voice, speed, lang)})
    return cues


def repo_path(path: Path) -> str:
    """Path relative to the project root, or absolute if it lives outside it.

    main.py resolves a cue's `wav` as ROOT / <value>, which pathlib leaves
    untouched when the value is already absolute - so both forms work.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def prune_stale(wav: Path, keep: str) -> list[Path]:
    """Delete same-id WAVs whose hash no longer matches (i.e. the text was edited)."""
    removed = []
    for old in wav.parent.glob(f"{wav.stem.rsplit('_', 1)[0]}_*.wav"):
        if old.name != keep:
            old.unlink()
            removed.append(old)
    return removed


def report_overlaps(cues: list[dict]) -> int:
    """Warn when a cue is still speaking as the next one fires. Returns the count."""
    timed = sorted((c for c in cues if c["at"] is not None), key=lambda c: c["at"])
    clashes = 0
    for cur, nxt in zip(timed, timed[1:]):
        end = cur["at"] + cur["duration_s"]
        if end > nxt["at"]:
            clashes += 1
            print(f"  WARN overlap: {cur['key']} @{cur['at']:.2f}s runs to "
                  f"{end:.2f}s but {nxt['key']} fires @{nxt['at']:.2f}s "
                  f"(-{end - nxt['at']:.2f}s)")
    for cue in cues:
        if cue["at"] is None:
            print(f"  note: {cue['key']} has no 'at' - it will never fire on its own")
    return clashes


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-render scripted audio cues to WAV.")
    parser.add_argument("--config-dir", default=str(CONFIG_DIR), help="audio_config directory")
    parser.add_argument("--out", default=str(OUT_DIR), help="WAV cache directory")
    parser.add_argument("--force", action="store_true", help="re-render even if cached")
    parser.add_argument("--list", action="store_true",
                        help="print the timeline from an existing cache; render nothing")
    opts = parser.parse_args()

    out = Path(opts.out)
    cues = load_cues(Path(opts.config_dir))
    if not cues:
        print(f"no cues found in {opts.config_dir}")
        return

    rendered = cached = 0
    for cue in cues:
        wav = out / cue["file"].removesuffix(".json") / f"{cue['id']}_{cue['hash']}.wav"
        if wav.exists() and not opts.force:
            cue["duration_s"] = round(wav_duration(wav), 2)
            status, cached = "cached", cached + 1
        elif opts.list:
            cue["duration_s"] = 0.0
            status = "MISSING"
        else:
            cue["duration_s"] = round(
                synth_to_file(cue["text"], wav, cue["voice"], cue["speed"], cue["lang_code"]), 2)
            status, rendered = "RENDER", rendered + 1
            for old in prune_stale(wav, wav.name):
                print(f"  removed stale {old.name}")
        cue["wav"] = repo_path(wav)
        at = f"@{cue['at']:6.2f}s" if cue["at"] is not None else "  (no at)"
        print(f"  {cue['key']:<40} {cue['voice']:<10} {at}  "
              f"{cue['duration_s']:5.2f}s  {status}")

    manifest = out / "manifest.json"
    if not opts.list:
        ordered = sorted(cues, key=lambda c: (c["at"] is None, c["at"] or 0.0))
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(
            {"sample_rate": SAMPLE_RATE,
             "cues": {c["key"]: {k: v for k, v in c.items() if k != "key"} for c in ordered}},
            indent=2) + "\n", encoding="utf-8")

    print(f"\n{len(cues)} cues | {rendered} rendered | {cached} cached"
          + ("" if opts.list else f" -> {repo_path(manifest)}"))
    if report_overlaps(cues):
        print("  -> shorten a line or move an 'at' so the cues do not collide")


if __name__ == "__main__":
    main()
