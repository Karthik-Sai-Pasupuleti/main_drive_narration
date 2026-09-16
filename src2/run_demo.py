#!/usr/bin/env python3
"""src2 - three-layer drive narration that runs without a rosbag or a ROS graph.

    python src2/run_demo.py                       # the full 120 s drive
    python src2/run_demo.py --speed 10            # same script, replayed in 12 s
    python src2/run_demo.py --dry-run --speed 20  # wiring + files only, no model
    python src2/run_demo.py --model qwen3-vl:4b   # override both layers' model

The layers, and where each one's event comes from now that the bag is gone:

    layer 1  turn detection (config/dummy_turns.json) -> RViz frame -> VLM
             -> refined commentary, saved as text + WAV in <run>/layer1/
    layer 2  three publisher nodes (config/dummy_events.json): the infrastructure
             pole at 30 s, the drone at 60 s, the robot at 90 s, each on its own
             topic of an in-process bus that mirrors a ROS graph
    layer 3  one LLM subscriber under all three nodes: a system prompt holds the
             role, a user prompt holds one placeholder per node -> refined context,
             saved as text + WAV in <run>/layer3/

Everything a run produces lands in one timestamped folder: each line's text and
its audio share a stem and sit side by side, with transcript.jsonl and summary.md
for the whole drive.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

SRC2 = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC2))            # make `core` / `layers` importable

from core.artifacts import RunFolder                              # noqa: E402
from core.bus import Bus                                          # noqa: E402
from core.frames import FrameSource                               # noqa: E402
from core.llm import load_config, load_env, model_label           # noqa: E402
from core.scheduler import Timeline                               # noqa: E402
from core.speech import Speaker                                   # noqa: E402
from core.srcpath import ROOT                                     # noqa: E402
from core.worker import SerialWorker                              # noqa: E402
from layers.layer1_turn_narration import TurnCommentaryLayer, load_turns   # noqa: E402
from layers.layer2_event_publishers import EventPublishers, load_events    # noqa: E402
from layers.layer3_subscriber import SubscriberLayer              # noqa: E402

DEFAULT_CONFIG = SRC2 / "config" / "pipeline.toml"
AGENT_SECTIONS = ("layer1", "layer3")

logger = logging.getLogger("src2")


def resolve(path: str | Path) -> Path:
    """Resolve a config path against src2/ (absolute paths are left alone)."""
    path = Path(path)
    return path if path.is_absolute() else (SRC2 / path).resolve()


def apply_overrides(cfg: dict, model: str | None, provider: str | None) -> dict:
    """Point both agent sections at `model` / `provider` when given (from --model)."""
    for section in AGENT_SECTIONS:
        if section not in cfg:
            continue
        if model:
            cfg[section]["model"] = model
        if provider:
            cfg[section]["provider"] = provider
    return cfg


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the demo."""
    parser = argparse.ArgumentParser(
        description="Three-layer drive narration demo (no rosbag, no ROS graph).")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="pipeline TOML")
    parser.add_argument("--duration", type=float, default=None,
                        help="override the drive length in seconds")
    parser.add_argument("--speed", type=float, default=None,
                        help="clock multiplier (10 = replay the drive ten times faster)")
    parser.add_argument("--out", default=None, help="override the run-folder directory")
    parser.add_argument("--run-id", default=None, help="run-folder name (default: timestamp)")
    parser.add_argument("--model", default=None, help="override both layers' model")
    parser.add_argument("--provider", default=None, help="override the provider (ollama/openai)")
    parser.add_argument("--no-audio", action="store_true",
                        help="still write the WAVs, but play nothing")
    parser.add_argument("--no-capture", action="store_true",
                        help="skip the RViz screen grab; use the recorded frames")
    parser.add_argument("--dry-run", action="store_true",
                        help="exercise the timeline and the files without calling a model")
    return parser


def main(argv=None) -> None:
    """Wire the three layers onto one clock and play the drive."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per Ollama call is noise
    opts = build_parser().parse_args(argv)
    load_env(str(ROOT / ".env"))                  # only needed for provider = "openai"
    cfg = apply_overrides(load_config(opts.config), opts.model, opts.provider)

    tcfg, audio = cfg.get("timeline", {}), cfg.get("audio", {})
    capture_cfg = cfg.get("capture", {})
    duration = opts.duration if opts.duration is not None else float(tcfg.get("duration_s", 120))
    speed = opts.speed if opts.speed is not None else float(tcfg.get("speed", 1.0))
    out_dir = resolve(opts.out or cfg.get("output", {}).get("dir", "../output/src2"))
    use_llm = not opts.dry_run

    run = RunFolder(out_dir, opts.run_id)
    speaker = Speaker(play=not opts.no_audio, voice=audio.get("voice", "af_heart"),
                      speed=float(audio.get("speed", 1.0)),
                      espeak_rate=int(audio.get("espeak_rate", 160)))
    worker = SerialWorker()
    bus = Bus()
    memory: list[str] = []                        # every line spoken, shared by layers 1 and 3

    turns = load_turns(resolve(tcfg["turns"]))
    events = load_events(resolve(tcfg["events"]))
    frames = FrameSource(capture_cfg.get("window", "narration_chase"),
                         resolve(capture_cfg["fallback_frames"])
                         if capture_cfg.get("fallback_frames") else None,
                         live=not opts.no_capture)
    layer1 = TurnCommentaryLayer(cfg["layer1"], SRC2, frames, speaker, run, worker,
                                 memory, use_llm)
    layer2 = EventPublishers(bus, events)
    SubscriberLayer(cfg["layer3"], SRC2, bus, layer2.topics, speaker, run, worker,
                    memory, use_llm)

    timeline = Timeline(speed)
    layer1.register(timeline, turns)
    layer2.register(timeline)

    header = {"config": opts.config,
              "duration_s": f"{duration:.0f}",
              "clock speed": f"{speed}x",
              "layer 1 model": model_label(cfg["layer1"], use_llm),
              "layer 3 model": model_label(cfg["layer3"], use_llm),
              "frames": capture_cfg.get("window", "narration_chase")
                        + ("" if not opts.no_capture else " (capture disabled)"),
              "audio": "written + played" if not opts.no_audio else "written only"}
    logger.info("run folder: %s", run.path)
    for key, value in header.items():
        logger.info("  %-14s %s", key + ":", value)
    logger.info("script (%.0f s, %.1fx):", duration, speed)
    for cue in timeline.cues:
        logger.info("  t=%6.1fs  %s", cue.at, cue.label)

    try:
        timeline.run(duration)
    except KeyboardInterrupt:
        logger.info("interrupted; finishing the queued narrations")
    finally:
        worker.close()                            # let the backlog speak and be written
        speaker.close()
        summary = run.write_summary(header)
        logger.info("%d narration event(s) -> %s", len(run.records), run.path)
        logger.info("summary: %s", summary)


if __name__ == "__main__":
    main()
