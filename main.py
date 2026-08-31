"""Unified entry point: config-driven drive narration (single- or multi-agent).

    python main.py --config src/configs/action_pipeline.toml
    python main.py --config src/configs/multi_agent_pipeline.toml
    python main.py --config ... --eval --events 3 --csv out.csv   # bounded, logged

The pipeline is chosen by the TOML config. --eval logs every narration event
(models, per-agent inference times, outputs, captured images, speech time) to
MLflow; --csv appends the same as flat rows. --events/--timeout bound the run so
it exits on its own (used by the per-model study loop). --model/--provider
override the config's models. The HUD/overlay nodes + RViz must already be
running on the same ROS network.
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import queue
import sys
import threading
import time
from pathlib import Path

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from bot.bot import (AgentConfig, LLMBot,                        # noqa: E402
                     Action_Based_Narration_Input, Action_Based_Narration_Output,
                     Multi_Agent_Narration_Input, Multi_Agent_Narration_Output,
                     Road_Geometry_Input, Road_Geometry_Output,
                     Obstacles_description_Input, Obstacles_description_Output)
from utils.tts import (PRIORITY_CUE, PRIORITY_NARRATION,         # noqa: E402
                       make_tts)
from utils.utils import load_config, load_env                    # noqa: E402
from utils.vision import capture, rviz_bbox                      # noqa: E402
from utils.vehicle_actions_extraction import VehicleActionsExtractor  # noqa: E402

CONFIGS_DIR = SRC / "configs"
CUE_MANIFEST = ROOT / "cache" / "audio" / "manifest.json"
AGENT_ROLES = ("road_geometry", "obstacles", "narration")
CSV_FIELDS = ["pipeline", "event", "model", "driving_action", "road_geometry",
              "obstacles", "narration", "road_s", "obstacles_s", "narration_s",
              "total_infer_s", "speech_s"]
load_env(str(ROOT / ".env"))


def apply_model_override(cfg: dict, model: str | None, provider: str | None = None) -> dict:
    """Override every agent section's model (and provider), e.g. from --model."""
    if not model:
        return cfg
    for role in AGENT_ROLES:
        if role in cfg:
            cfg[role]["model"] = model
            if provider:
                cfg[role]["provider"] = provider
    return cfg


def append_csv(path: str, records: list[dict]) -> None:
    """Append per-event rows to `path` (writing the header only for a new file)."""
    if not records:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    new = not target.exists()
    with target.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new:
            writer.writeheader()
        for row in records:
            writer.writerow(row)


def _b64_to_pil(b64: str):
    from PIL import Image
    return Image.open(io.BytesIO(base64.b64decode(b64)))


def _timed(fn):
    """Run fn(); return (result, seconds)."""
    start = time.perf_counter()
    return fn(), time.perf_counter() - start


class Narrator(Node):
    """Runs either pipeline, chosen by the config. Shared front-end (turn
    detection + drone/infra reports) feeds a serial worker that narrates. With
    eval_mode=True, every event is logged to MLflow."""

    def __init__(self, cfg: dict, use_tts: bool = True, eval_mode: bool = False,
                 experiment: str = "narration_eval",
                 cue_manifest: str | None = None) -> None:
        super().__init__("narrator")
        self.pipeline = cfg["pipeline"]
        self.eval = eval_mode
        self.event_count = 0
        self.records: list[dict] = []   # flat per-event rows for the eval CSV
        cap = cfg.get("capture", {})
        self.tts = make_tts(use_tts)
        self.extractor = VehicleActionsExtractor(on_route_deviation=self.on_action)

        if self.pipeline == "multiagent":
            self.geometry = LLMBot(AgentConfig(**cfg["road_geometry"]),
                                   Road_Geometry_Output, CONFIGS_DIR)
            self.obstacles = LLMBot(AgentConfig(**cfg["obstacles"]),
                                    Obstacles_description_Output, CONFIGS_DIR)
            self.narration = LLMBot(AgentConfig(**cfg["narration"]),
                                    Multi_Agent_Narration_Output, CONFIGS_DIR)
            self._bev_bbox = rviz_bbox(cap.get("bev", "narration_bev"))
            self._persp_bbox = rviz_bbox(cap.get("perspective", "narration_chase"))
        else:  # "action"
            self.narration = LLMBot(AgentConfig(**cfg["narration"]),
                                    Action_Based_Narration_Output, CONFIGS_DIR)
            self._bbox = rviz_bbox(cap.get("window", "rviz"))

        self._models = {r: (cfg[r].get("provider", "ollama"), cfg[r]["model"])
                        for r in AGENT_ROLES if r in cfg}
        self._drone = "none"
        self._infra = "none"
        self._memory: list[str] = []

        self._jobs: queue.Queue = queue.Queue(maxsize=16)
        self._worker = threading.Thread(target=self._run_worker, name="narrator", daemon=True)
        self._worker.start()
        self.create_subscription(String, "/drone/reports", self.on_drone, 10)
        self.create_subscription(String, "/infrastructure/reports", self.on_infra, 10)

        # Scripted cues: pre-rendered WAVs fired by cue_publisher.py at fixed
        # drive timestamps. They bypass the LLM entirely (no capture, no queue)
        # so the audio lands on the frame the timestamp was authored against.
        self._cues = self._load_cues(cue_manifest or CUE_MANIFEST)
        self.create_subscription(String, "/narration/cue", self.on_cue, 10)

        self._mlflow = None
        if self.eval:
            import mlflow
            mlflow.set_experiment(experiment)
            self._mlflow = mlflow
        self.get_logger().info(f"narrator running: {self.pipeline} pipeline"
                               + (" [eval -> mlflow]" if self.eval else ""))

    # ------------------------------------------------------------ scripted cues
    def _load_cues(self, path) -> dict:
        """Load the pre-rendered cue manifest, or {} if it hasn't been built.

        Args:
            path: cache/audio/manifest.json written by src/utils/prerender.py.

        Returns:
            dict: cue key -> manifest entry (wav, text, duration_s, at, ...).
        """
        manifest = Path(path)
        if not manifest.exists():
            self.get_logger().info(
                f"no cue manifest at {manifest} (run 'python src/utils/prerender.py' "
                "to enable the scripted audio cues)")
            return {}
        cues = json.loads(manifest.read_text(encoding="utf-8")).get("cues", {})
        self.get_logger().info(f"{len(cues)} scripted cues loaded from {manifest.name}")
        return cues

    def on_cue(self, msg: String) -> None:
        """Play a pre-rendered cue; it outranks LLM narration so it can't be cut off."""
        cue = self._cues.get(msg.data)
        if cue is None:
            self.get_logger().warning(
                f"unknown cue {msg.data!r}; re-run src/utils/prerender.py")
            return
        wav = ROOT / cue["wav"]
        if not wav.exists():
            self.get_logger().warning(f"cue {msg.data}: missing WAV {wav}")
            return
        self.get_logger().info(
            f"CUE {msg.data} ({cue.get('duration_s', 0.0):.1f}s): {cue['text']}")
        self._memory.append(cue["text"])   # the LLM sees what was already spoken
        self.tts.play_file(str(wav), priority=PRIORITY_CUE)

    # ------------------------------------------------------------ triggers
    def on_action(self, direction: str, phase: str) -> None:
        when = "now taking" if phase == "executing" else "approaching"
        action = (f"TURN {direction.upper()} (intersection) - we are {when} a "
                  f"{direction} turn at the oncoming intersection")
        self._enqueue({"kind": "turn", "driving_action": action})

    def on_drone(self, msg: String) -> None:
        if not msg.data or msg.data == self._drone:
            return
        self._drone = msg.data
        self._enqueue({"kind": "drone", "drone_data": msg.data,
                       "driving_action": "SLOW DOWN (external report) - the drone reports a hazard ahead"})

    def on_infra(self, msg: String) -> None:
        if not msg.data or msg.data == self._infra:
            return
        self._infra = msg.data
        self._enqueue({"kind": "infrastructure", "infrastructure_data": msg.data,
                       "driving_action": "SLOW DOWN (external report) - the infrastructure reports a hazard ahead"})

    def _enqueue(self, job: dict) -> None:
        if self.pipeline == "multiagent":
            job["bev"] = capture(bbox=self._bev_bbox)
            job["perspective"] = capture(bbox=self._persp_bbox)
        else:
            job["image"] = capture(bbox=self._bbox)
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            self.get_logger().warning("narration queue full; dropping an event")

    # ------------------------------------------------------------ worker (serial)
    def _run_worker(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                self._narrate(job)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self.get_logger().error(f"narration job failed: {exc}")

    def _narrate(self, job: dict) -> None:
        res = (self._narrate_multiagent(job) if self.pipeline == "multiagent"
               else self._narrate_action(job))
        line = res.get("narration", "")
        if not line:
            self.get_logger().warning("empty narration; skipping.")
            return
        self.get_logger().info(f"NARRATION: {line}")
        self._memory.append(line)
        _, res["speech_s"] = _timed(lambda: (self.tts.speak(line, PRIORITY_NARRATION),
                                             self.tts.wait(60.0)))
        self.event_count += 1
        self.records.append(self._record(job, res))
        if self._mlflow is not None:
            self._log(job, res)

    def _record(self, job: dict, res: dict) -> dict:
        """Flat per-event row: the 3 bots' outputs + their inference times."""
        return {"pipeline": self.pipeline,
                "event": job.get("kind", "event"),
                "model": self._models.get("narration", ("", "?"))[1],
                "driving_action": job.get("driving_action", ""),
                "road_geometry": res.get("road", ""),
                "obstacles": res.get("obstacles", ""),
                "narration": res.get("narration", ""),
                "road_s": round(res.get("road_s", 0.0), 3),
                "obstacles_s": round(res.get("obstacles_s", 0.0), 3),
                "narration_s": round(res.get("narration_s", 0.0), 3),
                "total_infer_s": round(sum(res.get(k, 0.0)
                                           for k in ("road_s", "obstacles_s", "narration_s")), 3),
                "speech_s": round(res.get("speech_s", 0.0), 3)}

    def _narrate_action(self, job: dict) -> dict:
        out, s = _timed(lambda: self.narration.invoke(Action_Based_Narration_Input(
            driving_action=job["driving_action"],
            drone_data=job.get("drone_data", "none"),
            infrastructure_data=job.get("infrastructure_data", "none"),
            action_memory="\n".join(self.extractor.get_actions()),
            narration_memory="\n".join(self._memory) or "(none yet)"),
            image=job["image"]))
        self.get_logger().info(f"narration inference: {s:.2f} s")
        return {"narration": out.narration.strip(), "narration_s": s, "image": job.get("image")}

    def _narrate_multiagent(self, job: dict) -> dict:
        if job["bev"]:
            road_out, road_s = _timed(lambda: self.geometry.invoke(
                Road_Geometry_Input(Bev_image=job["bev"])))
            road = road_out.road_geometry.strip()
        else:
            road, road_s = "unclear", 0.0
        if job["perspective"]:
            obst_out, obst_s = _timed(lambda: self.obstacles.invoke(
                Obstacles_description_Input(front_perspective_image=job["perspective"])))
            obstacles = obst_out.obstacles_description.strip()
        else:
            obstacles, obst_s = "no notable obstacles", 0.0
        self.get_logger().info(f"ROAD ({road_s:.2f}s): {road}")
        self.get_logger().info(f"OBSTACLES ({obst_s:.2f}s): {obstacles}")
        narr_out, narr_s = _timed(lambda: self.narration.invoke(Multi_Agent_Narration_Input(
            driving_action=job["driving_action"],
            road_geometry_description=road, obstacles_description=obstacles,
            drone_data=job.get("drone_data", "none"),
            infrastructure_data=job.get("infrastructure_data", "none"),
            action_memory="\n".join(self.extractor.get_actions()),
            narration_memory="\n".join(self._memory) or "(none yet)")))
        self.get_logger().info(f"narration inference: {narr_s:.2f} s")
        return {"narration": narr_out.Narration.strip(), "road": road, "obstacles": obstacles,
                "road_s": road_s, "obstacles_s": obst_s, "narration_s": narr_s,
                "bev": job.get("bev"), "perspective": job.get("perspective")}

    def _log(self, job: dict, res: dict) -> None:
        """Log one narration event to MLflow (params, timings, outputs, images)."""
        m = self._mlflow
        kind = job.get("kind", "event")
        model = self._models.get("narration", ("", "?"))[1]
        with m.start_run(run_name=f"{self.pipeline}:{model}:{kind}"):
            params = {"pipeline": self.pipeline, "event": kind, "model": model,
                      "driving_action": job["driving_action"]}
            for role, (prov, mdl) in self._models.items():
                params[f"model_{role}"] = mdl
                params[f"provider_{role}"] = prov
            m.log_params(params)
            metrics = {"speech_s": res.get("speech_s", 0.0),
                       "total_infer_s": sum(res.get(k, 0.0)
                                            for k in ("road_s", "obstacles_s", "narration_s"))}
            for k in ("road_s", "obstacles_s", "narration_s"):
                if k in res:
                    metrics[k] = res[k]
            m.log_metrics(metrics)
            m.log_text(res["narration"], "narration.txt")
            if "road" in res:
                m.log_text(res["road"], "road_geometry.txt")
            if "obstacles" in res:
                m.log_text(res["obstacles"], "obstacles.txt")
            for name, key in (("input_bev.jpg", "bev"),
                              ("input_perspective.jpg", "perspective"),
                              ("input_image.jpg", "image")):
                if res.get(key):
                    try:
                        m.log_image(_b64_to_pil(res[key]), name)
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass

    def close(self) -> None:
        self._jobs.put(None)
        self._worker.join(timeout=60.0)   # let an in-flight event finish logging (MLflow)
        self.tts.close()
        self.extractor.destroy_node()
        self.destroy_node()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Config-driven drive narrator.")
    parser.add_argument("--config", default=str(CONFIGS_DIR / "action_pipeline.toml"),
                        help="pipeline TOML config")
    parser.add_argument("--no-tts", action="store_true", help="log only, no audio")
    parser.add_argument("--eval", action="store_true", help="log each event to MLflow")
    parser.add_argument("--events", type=int, default=0,
                        help="stop after N narration events (0 = run until interrupted)")
    parser.add_argument("--timeout", type=float, default=0.0,
                        help="stop after this many seconds (0 = no limit)")
    parser.add_argument("--csv", default=None, help="append per-event rows to this CSV on exit")
    parser.add_argument("--model", default=None, help="override all agents' model")
    parser.add_argument("--provider", default=None, help="override provider (ollama/openai)")
    parser.add_argument("--experiment", default="narration_eval", help="MLflow experiment")
    parser.add_argument("--cues", default=str(CUE_MANIFEST),
                        help="pre-rendered cue manifest (src/utils/prerender.py)")
    opts, ros_argv = parser.parse_known_args(argv)
    cfg = apply_model_override(load_config(opts.config), opts.model, opts.provider)

    rclpy.init(args=ros_argv)
    node = Narrator(cfg, use_tts=not opts.no_tts, eval_mode=opts.eval,
                    experiment=opts.experiment, cue_manifest=opts.cues)
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    executor.add_node(node.extractor)
    start = time.monotonic()
    try:
        if opts.events > 0 or opts.timeout > 0:   # bounded run (per-model eval)
            while rclpy.ok():
                if opts.events > 0 and node.event_count >= opts.events:
                    break
                if opts.timeout > 0 and (time.monotonic() - start) >= opts.timeout:
                    break
                executor.spin_once(timeout_sec=0.5)
        else:                                      # normal live run
            executor.spin()
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.close()                               # joins the worker (last row is appended)
        if opts.csv:
            append_csv(opts.csv, list(node.records))
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
