"""The run folder: each layer's text, audio and captured frame written together.

One run creates one timestamped folder with a subfolder per layer, and every
event contributes files that share a stem, so a line and the audio of that line
are always neighbours:

    output/src2/20260916-114500/
      layer1/turn_01_left.txt   turn_01_left.wav   turn_01_left.jpg
      layer3/report_01_infrapole.txt   report_01_infrapole.wav
      transcript.jsonl          summary.md

transcript.jsonl carries one machine-readable record per event (timings, model,
frame origin); summary.md is the same run as a readable table plus the script.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class RunFolder:
    """Creates the run folder and writes one event's artifacts at a time."""

    def __init__(self, root: str | Path, run_id: str | None = None) -> None:
        """Create `root/<run_id>` (timestamp by default) and its transcript.

        Args:
            root (str | Path): directory the run folders are created under.
            run_id (str | None, optional): folder name; defaults to a timestamp.
        """
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
        self.path = Path(root) / self.run_id
        self.path.mkdir(parents=True, exist_ok=True)
        self.transcript = self.path / "transcript.jsonl"
        self.records: list[dict] = []

    def layer_dir(self, layer: str) -> Path:
        """Return (creating it if needed) the folder that holds `layer`'s files."""
        directory = self.path / layer
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def save(self, layer: str, stem: str, body: str, meta: dict,
             image_b64: str | None = None) -> dict:
        """Write `<stem>.txt` (and the frame) for one event and return its paths.

        Args:
            layer (str): subfolder name, e.g. "layer1".
            stem (str): shared file stem for this event's text / audio / frame.
            body (str): the readable content of the .txt file.
            meta (dict): key/value header written above the body.
            image_b64 (str | None, optional): base64 JPEG to save as `<stem>.jpg`.

        Returns:
            dict: {"txt": ..., "wav": ..., "image": ...} as repo-relative paths
            ("wav" is where :class:`core.speech.Speaker` should render the audio).
        """
        directory = self.layer_dir(layer)
        txt = directory / f"{stem}.txt"
        header = "\n".join(f"{key}: {value}" for key, value in meta.items())
        txt.write_text(f"{header}\n\n{body.strip()}\n", encoding="utf-8")
        paths = {"txt": str(txt), "wav": str(directory / f"{stem}.wav"), "image": None}
        if image_b64:
            image = directory / f"{stem}.jpg"
            try:
                image.write_bytes(base64.b64decode(image_b64))
                paths["image"] = str(image)
            except (binascii.Error, ValueError) as exc:
                logger.warning("could not save the captured frame for %s (%s)", stem, exc)
        return paths

    def log(self, record: dict) -> None:
        """Append one event record to transcript.jsonl and keep it for the summary."""
        self.records.append(record)
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_summary(self, header: dict | None = None) -> Path:
        """Write summary.md (run settings, per-event table, full spoken script).

        Args:
            header (dict | None, optional): run settings listed at the top.

        Returns:
            Path: the summary file.
        """
        events = sorted(self.records, key=lambda r: r.get("drive_time_s", 0.0))
        lines = [f"# Narration run {self.run_id}", ""]
        for key, value in (header or {}).items():
            lines.append(f"- **{key}**: {value}")
        lines += ["", f"{len(events)} narration event(s).", "",
                  "| t (s) | layer | event | model | infer (s) | speech (s) | files |",
                  "| ---: | --- | --- | --- | ---: | ---: | --- |"]
        for rec in events:
            stem = Path(rec.get("txt", "")).stem
            lines.append(
                f"| {rec.get('drive_time_s', 0.0):.0f} | {rec.get('layer', '')} "
                f"| {rec.get('event', '')} | {rec.get('model', '')} "
                f"| {rec.get('infer_s', 0.0):.2f} | {rec.get('speech_s', 0.0):.2f} "
                f"| `{rec.get('layer', '')}/{stem}.txt` + `.wav` |")
        lines += ["", "## Spoken script", ""]
        for rec in events:
            lines.append(f"- **t={rec.get('drive_time_s', 0.0):.0f}s "
                         f"[{rec.get('event', '')}]** {rec.get('narration', '')}")
        summary = self.path / "summary.md"
        summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return summary
