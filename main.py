"""Unified entry point: config-driven drive narration (action pipeline).

    python main.py --config src/configs/action_pipeline.toml
    python main.py --config ... --eval --events 3 --csv out.csv   # bounded, logged

--eval logs every narration event (models, per-agent inference times, outputs,
speech time) to MLflow; --csv appends the same as flat rows. --events/--timeout
bound the run so it exits on its own (used by the per-model study loop).
--model/--provider override the config's models. Turn narration is text-only
(no image); the mobile-pole trigger's vlm method captions the pole's own
camera feed instead.

This is the ONLY node the pipeline needs: it subscribes directly to the raw
Autoware/mobile-pole topics (no factor_overlay.py/hud_relay.py relay process),
so run_demo.sh/run_live.sh only have to get those topics flowing (bag or live
stack) - this file owns all narration-trigger logic. Each trigger is a
subscription + a callback that calls _enqueue() when its condition is met;
adding a future trigger means adding one more such pair.
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                       ReliabilityPolicy)

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from autoware_adapi_v1_msgs.msg import SteeringFactorArray         # noqa: E402
from autoware_perception_msgs.msg import DetectedObjects          # noqa: E402
from sensor_msgs.msg import Image as RosImage                     # noqa: E402
from bot.bot import (AgentConfig, LLMBot,                        # noqa: E402
                     Action_Based_Narration_Input, Action_Based_Narration_Output,
                     Scene_Caption_Input, Scene_Caption_Output, Hazard_Narration_Input)
from utils.detections_to_text import (detections_to_text, parse_detected_objects,  # noqa: E402
                                      LABELS as OBJECT_LABELS)
from utils.tts import make_tts                                   # noqa: E402
from utils.utils import load_config, load_env                    # noqa: E402

CONFIGS_DIR = SRC / "configs"
AGENT_ROLES = ("narration",)
CSV_FIELDS = ["pipeline", "event", "model", "driving_action", "narration",
              "scene_description", "stage1_s", "narration_s", "total_infer_s", "speech_s"]
load_env(str(ROOT / ".env"))

# autoware_adapi_v1_msgs/SteeringFactor: direction / status constants.
STEER_DIR_LEFT, STEER_DIR_RIGHT = 1, 2
STEER_APPROACHING, STEER_TURNING = 1, 3


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


_PIL_MODE = {"rgb8": ("RGB", "RGB"), "bgr8": ("RGB", "BGR"), "mono8": ("L", "L")}


def _ros_image_to_b64_jpeg(msg: RosImage) -> str:
    """sensor_msgs/Image (rgb8/bgr8/mono8) -> base64 JPEG, for a vision LLM call."""
    from PIL import Image
    mode, raw_mode = _PIL_MODE[msg.encoding]
    img = Image.frombytes(mode, (msg.width, msg.height), bytes(msg.data),
                          "raw", raw_mode, msg.step).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _timed(fn):
    """Run fn(); return (result, seconds)."""
    start = time.perf_counter()
    return fn(), time.perf_counter() - start


class Narrator(Node):
    """The whole action pipeline in one node. Every trigger (turn detection,
    mobile-pole hazard detections, ...) is a subscription + a callback that
    calls _enqueue() when its condition is met; all of them feed one serial
    worker that narrates. With eval_mode=True, every event is logged to
    MLflow."""

    def __init__(self, cfg: dict, use_tts: bool = True, eval_mode: bool = False,
                 experiment: str = "narration_eval") -> None:
        super().__init__("narrator")
        self.pipeline = cfg["pipeline"]
        self.eval = eval_mode
        self.event_count = 0
        self.records: list[dict] = []   # flat per-event rows for the eval CSV
        self.tts = make_tts(use_tts)

        self.narration = LLMBot(AgentConfig(**cfg["narration"]),
                                Action_Based_Narration_Output, CONFIGS_DIR)
        self._models = {r: (cfg[r].get("provider", "ollama"), cfg[r]["model"])
                        for r in AGENT_ROLES if r in cfg}
        self._memory: list[str] = []                     # spoken narration lines
        self._action_memory: deque = deque(maxlen=40)     # fired driving_actions

        self._jobs: queue.Queue = queue.Queue(maxsize=16)
        self._worker = threading.Thread(target=self._run_worker, name="narrator", daemon=True)
        self._worker.start()

        be = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT,
                        durability=DurabilityPolicy.VOLATILE, history=HistoryPolicy.KEEP_LAST)

        # Trigger 1: turn detection, straight from Autoware's own steering
        # factors - no factor_overlay.py relay, no HUD text round-trip.
        self._turn_dir: str | None = None
        self._turn_exec = False
        self.create_subscription(SteeringFactorArray, "/api/planning/steering_factors",
                                 self.on_steering_factors, be)

        # Trigger 2: mobile-pole infrastructure hazard detections - optional,
        # only set up if the config has a [mobile_pole] section.
        mp = cfg.get("mobile_pole")
        self._mp_method = None
        if mp:
            self._mp_method = mp.get("method", "detections")
            self._mp_hazard_classes = set(mp.get("hazard_classes", ["pedestrian"]))
            self._mp_min_confidence = mp.get("min_confidence", 0.5)
            self._mp_cooldown_s = mp.get("cooldown_s", 15.0)
            self._mp_last_fired = 0.0
            self._mp_context_bot = LLMBot(AgentConfig(**mp["context"]),
                                          Action_Based_Narration_Output, CONFIGS_DIR)
            if self._mp_method == "vlm":
                self._mp_caption_bot = LLMBot(AgentConfig(**mp["caption"]),
                                              Scene_Caption_Output, CONFIGS_DIR)
                self._mp_latest_image: str | None = None
                image_topic = mp.get("image_topic",
                                     "/mobile_pole/axis_rgb_6_42/image_visualization")
                self.create_subscription(RosImage, image_topic, self.on_mobile_pole_image, be)
            self.create_subscription(DetectedObjects, mp["objects_topic"],
                                     self.on_mobile_pole_objects, be)
            self.get_logger().info(f"mobile_pole trigger: {mp['objects_topic']} "
                                   f"-> method={self._mp_method}")

        self._mlflow = None
        if self.eval:
            import mlflow
            mlflow.set_experiment(experiment)
            self._mlflow = mlflow
        self.get_logger().info(f"narrator running: {self.pipeline} pipeline"
                               + (" [eval -> mlflow]" if self.eval else ""))

    # ------------------------------------------------------------ trigger 1: turns
    def on_steering_factors(self, msg: SteeringFactorArray) -> None:
        """Detect an intersection turn directly from SteeringFactorArray - the
        same (direction, behavior, status) fields factor_overlay.py used to
        render as HUD text, read here without that text round-trip."""
        active = [f for f in msg.factors
                 if f.direction in (STEER_DIR_LEFT, STEER_DIR_RIGHT)
                 and f.status in (STEER_APPROACHING, STEER_TURNING)]
        if not active:
            self._turn_dir, self._turn_exec = None, False
            return
        nearest = min(active, key=lambda f: f.distance[0] if len(f.distance) else 0.0)
        if "intersection" not in (nearest.behavior or "").lower():
            self._turn_dir, self._turn_exec = None, False
            return
        direction = "left" if nearest.direction == STEER_DIR_LEFT else "right"
        executing = nearest.status == STEER_TURNING
        if direction != self._turn_dir:            # a different turn
            self._turn_dir, self._turn_exec = direction, executing
            self.on_turn(direction, "executing" if executing else "upcoming")
            return
        if executing:                              # same turn, now executing
            self._turn_exec = True
            return
        if self._turn_exec:                        # TURNING -> TURN = next turn
            self._turn_exec = False
            self.on_turn(direction, "upcoming")

    def on_turn(self, direction: str, phase: str) -> None:
        when = "now taking" if phase == "executing" else "approaching"
        action = (f"TURN {direction.upper()} (intersection) - we are {when} a "
                  f"{direction} turn at the oncoming intersection")
        self._enqueue({"kind": "turn", "driving_action": action})

    # ------------------------------------------------------------ trigger 2: mobile-pole
    def on_mobile_pole_image(self, msg: RosImage) -> None:
        try:
            self._mp_latest_image = _ros_image_to_b64_jpeg(msg)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.get_logger().warning(f"mobile_pole image decode failed: {exc}")

    def on_mobile_pole_objects(self, msg: DetectedObjects) -> None:
        if not self._mp_method:
            return
        objects = parse_detected_objects(msg)
        hazards = [o for o in objects
                  if OBJECT_LABELS.get(o["label"]) in self._mp_hazard_classes
                  and o["confidence"] >= self._mp_min_confidence]
        if not hazards:
            return
        now = time.monotonic()
        if now - self._mp_last_fired < self._mp_cooldown_s:
            return
        self._mp_last_fired = now
        label = OBJECT_LABELS.get(max(hazards, key=lambda o: o["confidence"])["label"], "object")
        self._enqueue({"kind": "mobile_pole", "mp_objects": hazards,
                       "driving_action": (f"SLOW DOWN (external report) - the infrastructure "
                                         f"reports a possible {label} hazard ahead")})

    # ------------------------------------------------------------ shared queueing
    def _enqueue(self, job: dict) -> None:
        self._action_memory.append(job["driving_action"])
        if job.get("kind") == "mobile_pole" and self._mp_method == "vlm":
            job["image"] = self._mp_latest_image
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
        res = (self._narrate_mobile_pole(job) if job.get("kind") == "mobile_pole"
               else self._narrate_action(job))
        line = res.get("narration", "")
        if not line:
            self.get_logger().warning("empty narration; skipping.")
            return
        self.get_logger().info(f"NARRATION: {line}")
        self._memory.append(line)
        _, res["speech_s"] = _timed(lambda: (self.tts.speak(line), self.tts.wait(60.0)))
        self.event_count += 1
        self.records.append(self._record(job, res))
        if self._mlflow is not None:
            self._log(job, res)

    def _record(self, job: dict, res: dict) -> dict:
        """Flat per-event row: the bots' outputs + their inference times."""
        return {"pipeline": self.pipeline,
                "event": job.get("kind", "event"),
                "model": self._models.get("narration", ("", "?"))[1],
                "driving_action": job.get("driving_action", ""),
                "narration": res.get("narration", ""),
                "scene_description": res.get("scene_description", ""),
                "stage1_s": round(res.get("stage1_s", 0.0), 3),
                "narration_s": round(res.get("narration_s", 0.0), 3),
                "total_infer_s": round(sum(res.get(k, 0.0) for k in
                                           ("stage1_s", "narration_s")), 3),
                "speech_s": round(res.get("speech_s", 0.0), 3)}

    def _narrate_action(self, job: dict) -> dict:
        out, s = _timed(lambda: self.narration.invoke(Action_Based_Narration_Input(
            driving_action=job["driving_action"],
            action_memory="\n".join(self._action_memory) or "(no readings)",
            narration_memory="\n".join(self._memory) or "(none yet)")))
        self.get_logger().info(f"narration inference: {s:.2f} s")
        return {"narration": out.narration.strip(), "narration_s": s}

    def _narrate_mobile_pole(self, job: dict) -> dict:
        """detections -> text -> context LLM narration (method='detections'), or
        pole image -> VLM caption -> text -> context LLM narration (method='vlm')."""
        if self._mp_method == "vlm":
            cap_out, stage1_s = _timed(lambda: self._mp_caption_bot.invoke(
                Scene_Caption_Input(), image=job.get("image")))
            scene = cap_out.scene_description.strip()
        else:
            scene, stage1_s = _timed(lambda: detections_to_text(job["mp_objects"]))
        ctx_out, ctx_s = _timed(lambda: self._mp_context_bot.invoke(Hazard_Narration_Input(
            driving_action=job["driving_action"], scene_description=scene,
            action_memory="\n".join(self._action_memory) or "(no readings)",
            narration_memory="\n".join(self._memory) or "(none yet)")))
        self.get_logger().info(f"mobile_pole stage1 ({self._mp_method}): {stage1_s:.2f}s "
                               f"context: {ctx_s:.2f}s")
        return {"narration": ctx_out.narration.strip(), "scene_description": scene,
                "stage1_s": stage1_s, "narration_s": ctx_s, "image": job.get("image")}

    def _log(self, job: dict, res: dict) -> None:
        """Log one narration event to MLflow (params, timings, outputs, image)."""
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
                       "total_infer_s": sum(res.get(k, 0.0) for k in
                                            ("stage1_s", "narration_s"))}
            for k in ("stage1_s", "narration_s"):
                if k in res:
                    metrics[k] = res[k]
            m.log_metrics(metrics)
            m.log_text(res["narration"], "narration.txt")
            if "scene_description" in res:
                m.log_text(res["scene_description"], "scene_description.txt")
            if res.get("image"):
                try:
                    m.log_image(_b64_to_pil(res["image"]), "mobile_pole_image.jpg")
                except Exception:  # pylint: disable=broad-exception-caught
                    pass

    def close(self) -> None:
        self._jobs.put(None)
        self._worker.join(timeout=5.0)
        self.tts.close()
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
    opts, ros_argv = parser.parse_known_args(argv)
    cfg = apply_model_override(load_config(opts.config), opts.model, opts.provider)

    rclpy.init(args=ros_argv)
    node = Narrator(cfg, use_tts=not opts.no_tts, eval_mode=opts.eval,
                    experiment=opts.experiment)
    executor = MultiThreadedExecutor()
    executor.add_node(node)
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
