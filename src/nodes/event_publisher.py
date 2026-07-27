#!/usr/bin/env python3
"""
event_publisher.py - injects the demo's scripted drone / infrastructure reports
onto the narration input topics at fixed times in the drive.

These are the "external reports" the narration pipeline (narration/node.py)
subscribes to and feeds straight into the {drone_data} / {infrastructure_data}
prompt placeholders. In the real system an aerial drone and a roadside pole would
emit them; for the RViz demo we replay a fixed timeline instead of the manual
`ros2 topic pub` commands documented in scripts/narration.sh.

Timeline (WALL seconds counted from this node's start - launch it when the drive
begins, i.e. right after the bag is resumed in scripts/run_demo.sh):

    28-38 s  /drone/reports          "an occluded pedestrian ..."
    58-68 s  /infrastructure/reports "a construction zone ..."

Events are read from narration/events.json (override with --events-file). Each
event is (re)published at a few Hz while wall time is inside its [start, end)
window - the narration node dedupes repeats, so exactly one narration fires per
event; the repeats only guard against a dropped best-effort message. Runs on WALL
time (use_sim_time:=false) like the other overlay nodes, so the looping bag's
sim-time jumps can't disturb the schedule.

Publications:

    /drone/reports          std_msgs/String
    /infrastructure/reports std_msgs/String
"""
import argparse
import json
from pathlib import Path

import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                       ReliabilityPolicy)
from std_msgs.msg import String

# source name -> topic the narration node subscribes to.
TOPICS = {"drone": "/drone/reports", "infrastructure": "/infrastructure/reports"}
DEFAULT_EVENTS = Path(__file__).resolve().parent.parent / "narration" / "events.json"


def load_events(path: str) -> list:
    """Read + validate the events file, sorted by start time."""
    events = json.loads(Path(path).read_text(encoding="utf-8"))
    for e in events:
        if e.get("source") not in TOPICS:
            raise ValueError(
                f"event source must be one of {list(TOPICS)}, got {e.get('source')!r}")
        for key in ("start", "end", "message"):
            if key not in e:
                raise ValueError(f"event is missing '{key}': {e}")
    return sorted(events, key=lambda e: e["start"])


class EventPublisher(Node):
    """Publishes scripted drone / infrastructure reports on a wall-clock timeline."""

    def __init__(self, events: list, rate_hz: float = 2.0) -> None:
        super().__init__("event_publisher")
        self._wall = Clock(clock_type=ClockType.SYSTEM_TIME)
        self._t0 = self._now()
        self._events = events
        self._announced = set()   # indices already logged once (dedupe the info log)

        # Latched + reliable: compatible with the narration node's volatile
        # best-effort subscription AND replays the last report to a late joiner.
        latched = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             history=HistoryPolicy.KEEP_LAST)
        self._pubs = {src: self.create_publisher(String, topic, latched)
                      for src, topic in TOPICS.items()}

        self.create_timer(1.0 / rate_hz, self._tick)
        plan = ", ".join(f"{e['source']}@{e['start']:.0f}-{e['end']:.0f}s" for e in events)
        self.get_logger().info(
            f"event_publisher running: {plan or 'no events'} -> "
            f"{TOPICS['drone']} + {TOPICS['infrastructure']}")

    def _now(self) -> float:
        t = self._wall.now().to_msg()
        return t.sec + t.nanosec * 1e-9

    def _tick(self) -> None:
        elapsed = self._now() - self._t0
        for i, ev in enumerate(self._events):
            if ev["start"] <= elapsed < ev["end"]:
                self._pubs[ev["source"]].publish(String(data=ev["message"]))
                if i not in self._announced:
                    self._announced.add(i)
                    self.get_logger().info(
                        f"[{elapsed:6.1f}s] {ev['source']} report -> {ev['message']!r}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Timed drone/infrastructure report injector for the narration demo.")
    parser.add_argument("--events-file", default=str(DEFAULT_EVENTS))
    parser.add_argument("--rate", type=float, default=2.0,
                        help="re-publish rate within an event window [Hz]")
    opts, ros_argv = parser.parse_known_args()

    rclpy.init(args=ros_argv)
    node = EventPublisher(load_events(opts.events_file), rate_hz=opts.rate)
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
