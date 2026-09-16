"""Layer 1 - turn detection -> RViz frame -> VLM -> spoken commentary.

A turn event is the trigger. On the real vehicle it comes from the decision HUD
(src/utils/vehicle_actions_extraction.py, which needs a live ROS graph); here it
comes from config/dummy_turns.json, so the layer behaves identically without a
rosbag. When a turn fires, the frame is grabbed AT THAT MOMENT on the timeline
thread, then the slow part - VLM, then speech - runs on the serial worker.

The VLM is told the direction as ground truth and asked to describe the scene as
it actually is; its refined line is written to this layer's folder as text and
rendered to a WAV of the same name right beside it.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from core.artifacts import RunFolder
from core.frames import FrameSource
from core.llm import make_agent, model_label
from core.schemas import TurnCommentaryInput, TurnCommentaryOutput
from core.scheduler import Timeline
from core.speech import Speaker
from core.worker import SerialWorker

logger = logging.getLogger(__name__)

LAYER = "layer1"


def load_turns(path: str | Path) -> list[dict]:
    """Read the dummy turn-detection timeline.

    Args:
        path (str | Path): JSON file with {"turns": [{"at", "direction", "phase"}]}.

    Returns:
        list[dict]: the turns, earliest first.

    Raises:
        ValueError: if an entry is missing 'at' or 'direction'.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    turns = data["turns"] if isinstance(data, dict) else data
    for turn in turns:
        if "at" not in turn or "direction" not in turn:
            raise ValueError(f"turn entry needs 'at' and 'direction': {turn}")
    return sorted(turns, key=lambda t: float(t["at"]))


class TurnCommentaryLayer:
    """Turns the scripted turn events into image-grounded spoken commentary."""

    def __init__(self, cfg: dict, prompts_dir: str | Path, frames: FrameSource,
                 speaker: Speaker, run: RunFolder, worker: SerialWorker,
                 memory: list[str], use_llm: bool = True) -> None:
        """Wire the layer to its model and the shared run resources.

        Args:
            cfg (dict): the `[layer1]` config section (provider / model / prompt / vision).
            prompts_dir (str | Path): base directory for the section's `prompt` path.
            frames (FrameSource): supplies the frame shown to the VLM.
            speaker (Speaker): renders and plays the line.
            run (RunFolder): where the text / audio / frame are written.
            worker (SerialWorker): runs the VLM call and the speech off the clock thread.
            memory (list[str]): lines already spoken on this drive (shared with layer 3).
            use_llm (bool, optional): False narrates from a template (dry run).
        """
        self._frames, self._speaker, self._run = frames, speaker, run
        self._worker, self._memory = worker, memory
        self._model = model_label(cfg, use_llm)
        self._agent = make_agent(cfg, TurnCommentaryOutput, prompts_dir, use_llm)
        self._count = 0

    def register(self, timeline: Timeline, turns: list[dict]) -> None:
        """Schedule every turn in `turns` on `timeline`.

        Args:
            timeline (Timeline): the drive clock.
            turns (list[dict]): entries from :func:`load_turns`.
        """
        for turn in turns:
            direction = str(turn["direction"]).lower()
            phase = str(turn.get("phase", "upcoming")).lower()
            timeline.add(float(turn["at"]),
                         f"LAYER 1  turn detected: {direction.upper()} ({phase})",
                         lambda drive_time, d=direction, p=phase: self.on_turn(drive_time, d, p))

    def on_turn(self, drive_time: float, direction: str, phase: str) -> None:
        """Handle one turn-detection event: grab the frame now, narrate on the worker.

        Args:
            drive_time (float): seconds into the drive.
            direction (str): "left" or "right".
            phase (str): "upcoming" or "executing".
        """
        frame, origin = self._frames.grab()          # captured at the event's moment
        self._count += 1
        index = self._count
        self._worker.submit(f"layer1 turn {index}",
                            lambda: self._narrate(index, drive_time, direction, phase,
                                                  frame, origin))

    def _narrate(self, index: int, drive_time: float, direction: str, phase: str,
                 frame: str | None, origin: str) -> None:
        """Run the VLM, write `<stem>.txt` / `<stem>.jpg`, speak and log the event."""
        action = f"TURN {direction.upper()} (intersection)"
        started = time.perf_counter()
        scene, narration = self._commentary(action, direction, phase, frame)
        infer_s = time.perf_counter() - started
        if not narration:
            logger.warning("layer 1: empty narration for turn %d; nothing saved", index)
            return
        logger.info("LAYER 1 (%.2fs): %s", infer_s, narration)

        stem = f"turn_{index:02d}_{direction}"
        paths = self._run.save(
            LAYER, stem,
            f"SCENE (as the model sees it)\n{scene}\n\nNARRATION\n{narration}",
            {"layer": "1 - turn detection -> VLM commentary",
             "drive_time_s": f"{drive_time:.1f}",
             "driving_action": action,
             "turn_phase": phase,
             "frame": origin,
             "model": self._model,
             "inference_s": f"{infer_s:.2f}"},
            image_b64=frame)
        self._memory.append(narration)
        speech_s = self._speaker.say(narration, paths["wav"])
        self._run.log({"layer": LAYER, "event": f"turn_{direction}", "drive_time_s": drive_time,
                       "driving_action": action, "turn_phase": phase, "frame": origin,
                       "model": self._model, "scene": scene, "narration": narration,
                       "infer_s": round(infer_s, 3), "speech_s": round(speech_s, 3),
                       **paths})

    def _commentary(self, action: str, direction: str, phase: str,
                    frame: str | None) -> tuple[str, str]:
        """Return (scene, narration) from the VLM, or a template line in dry-run mode."""
        if self._agent is None:
            return ("(dry run - no model was contacted)",
                    f"We are taking a {direction} turn at the intersection ahead.")
        out = self._agent.invoke(
            TurnCommentaryInput(driving_action=action, turn_direction=direction,
                                turn_phase=phase,
                                narration_memory="\n".join(self._memory) or "(none yet)"),
            image=frame)
        return out.scene.strip(), out.narration.strip()
