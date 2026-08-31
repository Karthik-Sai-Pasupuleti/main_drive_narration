#!/usr/bin/env python3
"""cue_publisher.py - fires the pre-rendered audio cues at fixed drive timestamps.

Reads cache/audio/manifest.json (built by src/utils/prerender.py) and publishes
each cue's KEY on /narration/cue when the drive reaches its `at` time. main.py
resolves the key to a WAV and plays it, so exactly one process owns the speaker -
two `aplay`s from two nodes would just mix into noise.

Clock (default sim):

  sim  - elapsed = /clock minus the first /clock sample, i.e. rosbag play time,
         which is what the `at` values in src/configs/audio_config/*.json are
         authored against. A backward jump (the looping bag restarting) re-arms
         every cue, so the cues repeat on each lap.
  wall - elapsed = wall seconds since this node started, like event_publisher.py.
         Cues fire once and a looping bag stays silent on later laps.

Launch it right after the bag is resumed, so t=0 lines up with the drive start.

    python3 src/nodes/cue_publisher.py
    python3 src/nodes/cue_publisher.py --clock wall --manifest cache/audio/manifest.json

Publications:

    /narration/cue   std_msgs/String   (cue key, e.g. "drone/occluded_pedestrian")
"""
import argparse
import json
from pathlib import Path

import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                       ReliabilityPolicy)
from rosgraph_msgs.msg import Clock as ClockMsg
from std_msgs.msg import String

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MANIFEST = ROOT / "cache" / "audio" / "manifest.json"
CUE_TOPIC = "/narration/cue"
LOOP_JUMP_S = 0.5   # a /clock step back by more than this means the bag looped


def load_manifest(path: str) -> list[tuple[float, str, float]]:
    """Read the cue manifest into a sorted list of (at, key, duration_s).

    Args:
        path (str): manifest.json written by src/utils/prerender.py.

    Returns:
        list[tuple[float, str, float]]: timed cues, earliest first.

    Raises:
        FileNotFoundError: if the manifest is missing (run prerender.py first).
    """
    manifest = Path(path)
    if not manifest.exists():
        raise FileNotFoundError(
            f"no cue manifest at {manifest}; run 'python src/utils/prerender.py' first")
    cues = json.loads(manifest.read_text(encoding="utf-8")).get("cues", {})
    timed = [(float(c["at"]), key, float(c.get("duration_s", 0.0)))
             for key, c in cues.items() if c.get("at") is not None]
    return sorted(timed)


class CuePublisher(Node):
    """Publishes cue keys on a sim-time (bag) or wall-time drive timeline."""

    def __init__(self, cues: list, clock_source: str = "sim") -> None:
        """Set up the cue publisher and its clock source.

        Args:
            cues (list): (at, key, duration_s) tuples from load_manifest().
            clock_source (str): "sim" (follow /clock) or "wall".
        """
        super().__init__("cue_publisher")
        self._cues = cues
        self._fired: set[str] = set()
        self._t0: float | None = None
        self._last: float | None = None

        # Volatile, NOT transient_local: a narrator that joins late must never be
        # handed a cue whose moment has already passed.
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.VOLATILE,
                         history=HistoryPolicy.KEEP_LAST)
        self._pub = self.create_publisher(String, CUE_TOPIC, qos)

        if clock_source == "sim":
            # Best-effort subscriber: compatible with whatever QoS the bag's
            # /clock publisher offers.
            be = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT,
                            durability=DurabilityPolicy.VOLATILE,
                            history=HistoryPolicy.KEEP_LAST)
            self.create_subscription(ClockMsg, "/clock", self._on_clock, be)
        else:
            self._wall = Clock(clock_type=ClockType.SYSTEM_TIME)
            self._t0 = self._wall_now()
            self.create_timer(0.05, self._on_wall)

        plan = ", ".join(f"{key}@{at:.2f}s({dur:.1f}s)" for at, key, dur in cues)
        self.get_logger().info(f"cue_publisher running: clock={clock_source} | "
                               f"{len(cues)} cues -> {CUE_TOPIC} | {plan or 'none'}")

    def _wall_now(self) -> float:
        """Current SYSTEM_TIME clock value in seconds."""
        stamp = self._wall.now().to_msg()
        return stamp.sec + stamp.nanosec * 1e-9

    def _on_wall(self) -> None:
        """Wall-clock tick: advance the timeline from this node's start."""
        self._advance(self._wall_now() - self._t0)

    def _on_clock(self, msg: ClockMsg) -> None:
        """Sim-clock tick: advance the timeline from the first /clock sample."""
        now = msg.clock.sec + msg.clock.nanosec * 1e-9
        if self._t0 is None:
            self._t0 = now
        elif self._last is not None and now < self._last - LOOP_JUMP_S:
            self.get_logger().info(
                f"bag looped (/clock jumped back {self._last - now:.1f}s); "
                f"re-arming {len(self._cues)} cues")
            self._t0, self._fired = now, set()
        self._last = now
        self._advance(now - self._t0)

    def _advance(self, elapsed: float) -> None:
        """Publish every cue whose `at` has passed and that has not fired yet.

        Args:
            elapsed (float): seconds since the drive started.
        """
        for at, key, dur in self._cues:
            if elapsed < at:
                break                      # cues are sorted; nothing later is due
            if key not in self._fired:
                self._fired.add(key)
                self._pub.publish(String(data=key))
                self.get_logger().info(f"[{elapsed:6.2f}s] cue -> {key} ({dur:.1f}s)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fires pre-rendered audio cues at fixed drive timestamps.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST),
                        help="cue manifest from src/utils/prerender.py")
    parser.add_argument("--clock", choices=("sim", "wall"), default="sim",
                        help="sim = rosbag /clock time (default), wall = seconds since start")
    opts, ros_argv = parser.parse_known_args()

    rclpy.init(args=ros_argv)
    node = CuePublisher(load_manifest(opts.manifest), clock_source=opts.clock)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
