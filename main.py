"""Entry point: the drive-narration orchestrator.

Narrator is the ROS node that wires everything together:

    /hud/decision --> VehicleActionsExtractor --(direction, phase)--> on_route_deviation
    /drone/reports ----------------------------------------------> on_drone_report
    /infrastructure/reports -------------------------------------> on_infrastructure_report
                                                                         |
                                                          OllamaBot (VLM) --> TTS

The HUD/overlay nodes (src/nodes/*) and the rosbag/RViz must already be running
on the same ROS network; this process only runs the narration side.

    python main.py            # default config + Kokoro TTS
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String

SRC = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC))

from bot.bot import Narration, OllamaBot                         # noqa: E402
from utils.tts import make_tts                                   # noqa: E402
from utils.vision import capture, rviz_bbox                      # noqa: E402
from utils.vehicle_actions_extraction import VehicleActionsExtractor  # noqa: E402

CONFIG = SRC / "configs" / "actions_promt.toml"


class Narrator(Node):
    """Owns the bot + TTS and narrates the events the extractor emits."""

    def __init__(self) -> None:
        super().__init__("narrator")
        self.bot = OllamaBot(config_path=str(CONFIG))
        self.action_extractor = VehicleActionsExtractor(
            on_route_deviation=self.on_route_deviation)
        self.tts = make_tts(enabled=True)

        # Latest external reports, fed into the prompt.
        self._drone_data = "none"
        self._infra_data = "none"
        # Previous narrations, fed back as context.
        self._narration_memory: list[str] = []

        # Resolve the RViz window bbox once so capture() grabs ONLY RViz (with
        # its planned-path overlay), not the whole screen. None -> full screen.
        self._rviz_bbox = rviz_bbox()
        self.get_logger().info(f"RViz capture region: {self._rviz_bbox or 'full screen'}")

        # A single background worker reasons + speaks one event at a time, so
        # lines never overlap and cut each other off (the reference project's
        # design). Keeps the ROS callbacks non-blocking too.
        self._jobs: queue.Queue = queue.Queue(maxsize=16)
        self._worker = threading.Thread(target=self._run_worker, name="narrator",
                                        daemon=True)
        self._worker.start()

        self.create_subscription(String, "/drone/reports", self.on_drone_report, 10)
        self.create_subscription(String, "/infrastructure/reports",
                                 self.on_infrastructure_report, 10)
        self.get_logger().info("narrator running: HUD + drone + infrastructure -> VLM -> TTS")

    # ------------------------------------------------------------ triggers (enqueue only)
    def on_route_deviation(self, direction: str, phase: str) -> None:
        """Queue an intersection turn detected by the extractor.

        Args:
            direction (str): 'left' or 'right'.
            phase (str): 'upcoming' or 'executing'.
        """
        self._enqueue({"kind": "turn", "direction": direction, "phase": phase})

    def on_drone_report(self, msg: String) -> None:
        """Queue a fresh drone report.

        Args:
            msg (String): the drone report text.
        """
        if not msg.data or msg.data == self._drone_data:
            return
        self._drone_data = msg.data
        self._enqueue({"kind": "drone", "drone_data": msg.data})

    def on_infrastructure_report(self, msg: String) -> None:
        """Queue a fresh infrastructure report.

        Args:
            msg (String): the infrastructure report text.
        """
        if not msg.data or msg.data == self._infra_data:
            return
        self._infra_data = msg.data
        self._enqueue({"kind": "infrastructure", "infrastructure_data": msg.data})

    # ------------------------------------------------------------ worker (serial)
    def _enqueue(self, job: dict) -> None:
        """Grab the current frame NOW (at trigger time) and queue the job.

        Capturing here rather than at narration time keeps the image aligned
        with the moment the event fired (before the queue + inference delay).

        Args:
            job (dict): the event to narrate.
        """
        job["image"] = capture(bbox=self._rviz_bbox)   # base64 JPEG, or None -> text-only
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            self.get_logger().warning(f"narration queue full; dropping a {job['kind']} event")

    def _run_worker(self) -> None:
        """Pull one job at a time and narrate it (runs on the worker thread)."""
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                self._handle(job)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self.get_logger().error(f"narration job failed: {exc}")

    def _handle(self, job: dict) -> None:
        """Compose the driving action for one job and narrate it.

        Args:
            job (dict): the queued event.
        """
        kind = job["kind"]
        image = job.get("image")
        if kind == "turn":
            direction, phase = job["direction"], job["phase"]
            when = "now taking" if phase == "executing" else "approaching"
            driving_action = (f"TURN {direction.upper()} (intersection) - we are "
                              f"{when} a {direction} turn at the oncoming intersection")
            self._narrate(driving_action, image=image)
        elif kind == "drone":
            self._narrate("SLOW DOWN (external report) - the drone reports a hazard ahead",
                          drone_data=job["drone_data"], image=image)
        elif kind == "infrastructure":
            self._narrate("SLOW DOWN (external report) - the infrastructure reports a hazard ahead",
                          infrastructure_data=job["infrastructure_data"], image=image)

    def _narrate(self, driving_action: str, drone_data: str = "none",
                 infrastructure_data: str = "none", image: str | None = None) -> None:
        """Invoke the VLM for one event, then speak and remember the line.

        Args:
            driving_action (str): the ground-truth action to narrate.
            drone_data (str, optional): the drone report. Defaults to "none".
            infrastructure_data (str, optional): the infrastructure report. Defaults to "none".
            image (str | None, optional): base64 RViz frame. Defaults to None.
        """
        started = time.perf_counter()
        try:
            result = self.invoke_narration(
                driving_action, drone_data=drone_data,
                infrastructure_data=infrastructure_data, image=image,
                action_memory="\n".join(self.action_extractor.get_actions()),
                narration_memory="\n".join(self._narration_memory) or "(none yet)")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.get_logger().error(f"VLM narration failed: {exc}")
            return
        elapsed = time.perf_counter() - started
        mode = "with image" if image else "text-only"
        self.get_logger().info(f"LLM inference time: {elapsed:.2f} s ({mode})")
        line = result.narration.strip()   # structured output: speak only this field
        if not line:
            self.get_logger().warning("empty narration; skipping.")
            return
        self.get_logger().info(f"SCENE: {result.scene.strip()}")
        self.get_logger().info(f"NARRATION: {line}")
        self._narration_memory.append(line)
        self.tts.speak(line)

    def invoke_narration(self, driving_action: str, drone_data: str = "none",
                         infrastructure_data: str = "none",
                         action_memory: str = "(no readings)",
                         narration_memory: str = "(none yet)",
                         image: str | None = None) -> Narration:
        """Render the prompt for one event and return the model's reply.

        Args:
            driving_action (str): the ground-truth action to narrate.
            drone_data (str, optional): latest drone report. Defaults to "none".
            infrastructure_data (str, optional): latest infrastructure report. Defaults to "none".
            action_memory (str, optional): recent HUD actions. Defaults to "(no readings)".
            narration_memory (str, optional): previous narrations. Defaults to "(none yet)".
            image (str | None, optional): base64 RViz frame. Defaults to None.

        Returns:
            Narration: the parsed reply with .scene, .action and .narration fields.
        """
        return self.bot.invoke(driving_action=driving_action,
                               drone_data=drone_data,
                               infrastructure_data=infrastructure_data,
                               action_memory=action_memory,
                               narration_memory=narration_memory,
                               image=image)

    def close(self) -> None:
        """Stop the worker + TTS and destroy both nodes."""
        self._jobs.put(None)               # signal the worker to exit
        self._worker.join(timeout=5.0)
        self.tts.close()
        self.action_extractor.destroy_node()
        self.destroy_node()


def main() -> None:
    """Spin the narrator + extractor nodes until shutdown."""
    rclpy.init()
    narrator = Narrator()
    executor = MultiThreadedExecutor()
    executor.add_node(narrator)
    executor.add_node(narrator.action_extractor)
    try:
        executor.spin()
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        narrator.close()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
