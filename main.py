"""Hazard narration node: one ROS2 node, one vision model, one prompt.

    python main.py              # narrate and speak
    python main.py --no-tts     # print the narration instead of speaking it

Two sources feed it - the mobile pole's infrastructure detections and the
robot's own detector. Each is a pair of subscriptions (detections + camera)
whose callback calls _enqueue() once its hazard conditions are met; both feed
a single serial worker that narrates. Adding another source (a drone, say)
means adding one more pair and a `source` name the prompt knows.

The detections only decide WHEN to narrate. What gets said comes from the
reporting camera's own frame, captioned by MODEL via narration_prompt.toml.
"""
from __future__ import annotations

import argparse
import base64
import io
import math
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import rclpy
import tf2_ros

try:
    import requests   # optional: only used to pre-warm the ollama model at startup
except ImportError:
    requests = None
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                       ReliabilityPolicy)
from rclpy.time import Time

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from autoware_perception_msgs.msg import DetectedObjects          # noqa: E402
from sensor_msgs.msg import Image as RosImage                     # noqa: E402
from vision_msgs.msg import Detection2DArray                      # noqa: E402
from bot.bot import (AgentConfig, LLMBot,                         # noqa: E402
                     Narration_Input, Narration_Output)
from utils.detections_to_text import (parse_detected_objects,     # noqa: E402
                                      LABELS as OBJECT_LABELS)
from utils.tts import make_tts                                    # noqa: E402
from utils.utils import load_env                                  # noqa: E402

# --- the model ---------------------------------------------------------------
PROVIDER = "ollama"                 # "ollama" (local) or "openai" (needs OPENAI_API_KEY)
MODEL = "minicpm-v4.6:latest"
TEMPERATURE = 0.1
PROMPT = SRC / "configs" / "narration_prompt.toml"

# --- topics ------------------------------------------------------------------
POLE_OBJECTS_TOPIC = "/mobile_pole/axis_rgb_6_42/autoware_objects_3d"
POLE_IMAGE_TOPIC = "/mobile_pole/axis_rgb_6_42/image_visualization"
ROBOT_OBJECTS_TOPIC = "/robot/detections"
ROBOT_IMAGE_TOPIC = "/robot/image_annotated"

# --- when a detection is worth narrating -------------------------------------
POLE_HAZARD_CLASSES = {"pedestrian"}   # autoware_perception_msgs ObjectClassification
ROBOT_HAZARD_CLASSES = {"person"}      # vision_msgs ObjectHypothesis.class_id
MIN_CONFIDENCE = 0.5
# Pole only: narrate the moment a pedestrian is this close to the ego vehicle,
# whose position comes from the TF tree. The distance is passed to the model
# along with the image.
TRIGGER_DISTANCE_M = 10.0
MAP_FRAME = "map"
EGO_FRAME = "base_link"

load_env(str(ROOT / ".env"))

_PIL_MODE = {"rgb8": ("RGB", "RGB"), "bgr8": ("RGB", "BGR"), "mono8": ("L", "L")}


def _ros_image_to_b64_jpeg(msg: RosImage) -> str:
    """sensor_msgs/Image (rgb8/bgr8/mono8) -> base64 JPEG, for the vision call."""
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


def _clock(t: float) -> str:
    """Wall-clock time as HH:MM:SS.mmm, for the per-event latency breakdown."""
    return datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]


def _warm_ollama_model(logger, keep_alive: str | int = "30m") -> None:
    """Load MODEL into VRAM up front (one blocking call), so the first hazard
    doesn't pay Ollama's cold-load time. Best-effort: if it fails, the run
    continues and just pays that cost on the first narration."""
    if PROVIDER != "ollama":
        return
    if requests is None:
        logger.warning("'requests' not installed; skipping model warm-up")
        return
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if not host.startswith("http"):
        host = f"http://{host}"
    start = time.perf_counter()
    try:
        requests.post(f"{host}/api/generate",
                      json={"model": MODEL, "prompt": "", "keep_alive": keep_alive},
                      timeout=180)
        logger.info(f"warmed ollama model '{MODEL}' in {time.perf_counter() - start:.1f}s")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(f"failed to warm ollama model '{MODEL}': {exc}")


class Narrator(Node):
    """Subscribes to each hazard source, narrates what its camera saw."""

    def __init__(self, use_tts: bool = True) -> None:
        super().__init__("narrator")
        self.tts = make_tts(use_tts)

        # Warm the model before any subscription exists, so nothing is dropped
        # while we wait.
        _warm_ollama_model(self.get_logger())

        self.bot = LLMBot(AgentConfig(provider=PROVIDER, model=MODEL,
                                      temperature=TEMPERATURE, vision=True,
                                      prompt=str(PROMPT)), Narration_Output)

        self._jobs: queue.Queue = queue.Queue(maxsize=16)
        self._worker = threading.Thread(target=self._run_worker, name="narrator", daemon=True)
        self._worker.start()
        
        # Latest camera frame per source, with the wall-clock time it arrived
        # so the narration can report end-to-end latency. `_done` latches on
        # the first narration: each source speaks exactly once per run.
        self._pole_image: str | None = None
        self._pole_image_t = 0.0
        self._pole_done = False
        self._robot_image: str | None = None
        self._robot_image_t = 0.0
        self._robot_done = False

        self._tf_buffer = tf2_ros.Buffer()
        self._tf_listener = tf2_ros.TransformListener(self._tf_buffer, self)

        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT,
                         durability=DurabilityPolicy.VOLATILE, history=HistoryPolicy.KEEP_LAST)
        self.create_subscription(RosImage, POLE_IMAGE_TOPIC, self.on_pole_image, qos)
        self.create_subscription(DetectedObjects, POLE_OBJECTS_TOPIC,
                                 self.on_pole_objects, qos)
        self.create_subscription(RosImage, ROBOT_IMAGE_TOPIC, self.on_robot_image, qos)
        self.create_subscription(Detection2DArray, ROBOT_OBJECTS_TOPIC,
                                 self.on_robot_detections, qos)
        self.get_logger().info(f"narrator running on {MODEL} | "
                               f"pole={POLE_OBJECTS_TOPIC} robot={ROBOT_OBJECTS_TOPIC}")

    # ------------------------------------------------------------ mobile pole
    def on_pole_image(self, msg: RosImage) -> None:
        try:
            self._pole_image, self._pole_image_t = _ros_image_to_b64_jpeg(msg), time.time()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.get_logger().warning(f"pole image decode failed: {exc}")

    def _ego_position(self):
        """Ego (x, y) in the map frame via TF, or None if the transform isn't
        available yet (e.g. right at bag start)."""
        try:
            tf = self._tf_buffer.lookup_transform(MAP_FRAME, EGO_FRAME,
                                                  Time())   # Time() = latest available
        except tf2_ros.TransformException as exc:
            self.get_logger().warning(f"no {MAP_FRAME}->{EGO_FRAME} transform yet ({exc})",
                                      throttle_duration_sec=5.0)
            return None
        t = tf.transform.translation
        return t.x, t.y

    def on_pole_objects(self, msg: DetectedObjects) -> None:
        """Narrate once, the first time a pedestrian is within
        TRIGGER_DISTANCE_M of the ego vehicle. The model gets the pole's
        camera frame plus that distance."""
        if self._pole_done or self._pole_image is None:
            return
        hazards = [o for o in parse_detected_objects(msg)
                   if OBJECT_LABELS.get(o["label"]) in POLE_HAZARD_CLASSES
                   and o["confidence"] >= MIN_CONFIDENCE]
        if not hazards:
            return
        ego = self._ego_position()
        if ego is None:
            return                             # can't gate on distance without an ego pose
        ex, ey = ego
        nearest = min(hazards, key=lambda o: math.hypot(o["position"][0] - ex,
                                                        o["position"][1] - ey))
        distance_m = math.hypot(nearest["position"][0] - ex, nearest["position"][1] - ey)
        if distance_m > TRIGGER_DISTANCE_M:
            return                             # not close enough yet
        self._pole_done = True
        label = OBJECT_LABELS.get(nearest["label"], "object")
        self.get_logger().info(f"pole trigger fired (once): {label} at {distance_m:.1f}m")
        self._enqueue("mobile_pole", f"A {label} is {distance_m:.1f} m from the vehicle.",
                      self._pole_image, self._pole_image_t)

    # ------------------------------------------------------------ robot
    def on_robot_image(self, msg: RosImage) -> None:
        try:
            self._robot_image, self._robot_image_t = _ros_image_to_b64_jpeg(msg), time.time()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.get_logger().warning(f"robot image decode failed: {exc}")

    def on_robot_detections(self, msg: Detection2DArray) -> None:
        """Narrate once, the first time the detector sees a hazard. Its boxes
        are 2D pixel space with no real-world position, so unlike the pole
        there is no distance to pass - the model gets the frame alone."""
        if self._robot_done or self._robot_image is None:
            return
        hazards = [r for det in msg.detections for r in det.results
                   if r.hypothesis.class_id in ROBOT_HAZARD_CLASSES
                   and r.hypothesis.score >= MIN_CONFIDENCE]
        if not hazards:
            return
        best = max(hazards, key=lambda r: r.hypothesis.score)
        self._robot_done = True
        self.get_logger().info(f"robot trigger fired (once): {best.hypothesis.class_id} "
                               f"(confidence={best.hypothesis.score:.2f})")
        self._enqueue("robot", "", self._robot_image, self._robot_image_t)

    # ------------------------------------------------------------ narration
    def _enqueue(self, source: str, detections: str, image: str, image_t: float) -> None:
        try:
            self._jobs.put_nowait({"source": source, "detections": detections,
                                   "image": image, "image_t": image_t})
        except queue.Full:
            self.get_logger().warning("narration queue full; dropping an event")

    def _run_worker(self) -> None:
        """Serial worker: one hazard narrated and spoken at a time."""
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                self._narrate(job)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self.get_logger().error(f"narration job failed: {exc}")

    def _narrate(self, job: dict) -> None:
        source, received_at = job["source"], job["image_t"]
        queued_s = time.time() - received_at      # frame age when the model starts
        out, infer_s = _timed(lambda: self.bot.invoke(Narration_Input(
            image=job["image"], source=source, detections=job["detections"])))
        line = out.narration.strip()
        if not line:
            self.get_logger().warning("empty narration; skipping")
            return
        self.get_logger().info(f"NARRATION [{source}]: {line}")
        # tts.speak() only queues; wait() blocks until the line has finished
        # playing, so this covers Kokoro's synthesis AND the audio playback.
        _, speech_s = _timed(lambda: (self.tts.speak(line), self.tts.wait(60.0)))
        self.get_logger().info(
            f"timing [{source}]: image {_clock(received_at)} "
            f"-> trigger +{queued_s:.2f}s -> {MODEL} {infer_s:.2f}s "
            f"-> speech {speech_s:.2f}s | total {time.time() - received_at:.2f}s")

    def close(self) -> None:
        self._jobs.put(None)
        self._worker.join(timeout=5.0)
        self.tts.close()
        self.destroy_node()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Hazard narrator.")
    parser.add_argument("--no-tts", action="store_true", help="log only, no audio")
    opts, ros_argv = parser.parse_known_args(argv)

    rclpy.init(args=ros_argv)
    node = Narrator(use_tts=not opts.no_tts)
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.close()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
