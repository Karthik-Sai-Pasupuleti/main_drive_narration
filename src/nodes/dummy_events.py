#!/usr/bin/env python3
"""Dummy drone / infrastructure report publisher for testing the pipeline.

Publishes a drone report and an infrastructure report a few seconds after start,
then RE-PUBLISHES them every --period seconds so the narrator receives them
whenever it starts (it dedupes by text, so each is narrated once). Unlike
event_publisher.py this fires quickly - no 28s/58s timeline.

    python3 src/nodes/dummy_events.py            # default messages
    python3 src/nodes/dummy_events.py --drone-at 2 --infra-at 5 --period 15

Publications: /drone/reports, /infrastructure/reports (std_msgs/String)
"""
import argparse

import rclpy
from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                       ReliabilityPolicy)
from std_msgs.msg import String

DRONE = "an occluded pedestrian crossing behind the parked van on the right"
INFRA = "a construction zone 80 metres ahead, right lane closed"


class DummyEvents(Node):
    """Publishes drone + infra reports, re-publishing every `period` seconds."""

    def __init__(self, drone_at: float, infra_at: float, period: float) -> None:
        super().__init__("dummy_events")
        latched = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             history=HistoryPolicy.KEEP_LAST)
        self._drone_pub = self.create_publisher(String, "/drone/reports", latched)
        self._infra_pub = self.create_publisher(String, "/infrastructure/reports", latched)
        self._period = period
        self._next_drone, self._next_infra = drone_at, infra_at
        self._logged = False
        self._t = 0.0
        self.create_timer(0.5, self._tick)
        self.get_logger().info(
            f"dummy_events: drone@{drone_at}s, infrastructure@{infra_at}s, every {period}s")

    def _tick(self) -> None:
        self._t += 0.5
        if self._t >= self._next_drone:
            self._drone_pub.publish(String(data=DRONE))
            self._next_drone += self._period
        if self._t >= self._next_infra:
            self._infra_pub.publish(String(data=INFRA))
            self._next_infra += self._period
        if not self._logged and self._t >= max(self._next_drone, self._next_infra) - self._period:
            self.get_logger().info(f"publishing drone={DRONE!r} | infra={INFRA!r} (repeating)")
            self._logged = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Dummy drone/infra report publisher.")
    parser.add_argument("--drone-at", type=float, default=3.0, help="publish drone at [s]")
    parser.add_argument("--infra-at", type=float, default=6.0, help="publish infra at [s]")
    parser.add_argument("--period", type=float, default=15.0, help="re-publish interval [s]")
    opts, ros_argv = parser.parse_known_args()

    rclpy.init(args=ros_argv)
    node = DummyEvents(opts.drone_at, opts.infra_at, opts.period)
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
