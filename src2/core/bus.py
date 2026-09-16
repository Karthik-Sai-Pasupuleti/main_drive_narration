"""A tiny in-process publish/subscribe bus with the shape of a ROS 2 graph.

Layer 2 owns three publisher nodes and layer 3 is the single subscriber; keeping
a bus between them means the two layers never reference each other, exactly as
they would not on a real ROS graph. The difference is that nothing here needs
rclpy, a roscore or a rosbag - the whole demo is one plain Python process.

Swapping in real ROS 2 is a drop-in: give a Bus-shaped object create_publisher()
and create_subscription() backed by rclpy and the layers are unchanged.

Callbacks run synchronously on the publishing thread (the timeline thread), so a
subscriber must not block: layer 3 hands its LLM work to core.worker.SerialWorker
and returns immediately.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Message:
    """One published report, carrying enough provenance for layer 3 and the log."""

    topic: str      # ROS-style topic name, e.g. "/infrastructure/reports"
    node: str       # publishing node, e.g. "ros_node_1"
    source: str     # who observed it: "infrapole" / "drone" / "robot"
    data: str       # the report text itself
    stamp: float    # seconds since the drive started


class Publisher:
    """Handle returned by :meth:`Bus.create_publisher`; publishes on one topic."""

    def __init__(self, bus: "Bus", topic: str, node: str, source: str) -> None:
        """Bind a publisher to its topic and the node identity it publishes as.

        Args:
            bus (Bus): the bus the message is dispatched on.
            topic (str): topic name to publish on.
            node (str): name of the publishing node.
            source (str): observer the report comes from.
        """
        self._bus, self.topic, self.node, self.source = bus, topic, node, source

    def publish(self, data: str, stamp: float) -> Message:
        """Publish `data` on this topic and return the dispatched message.

        Args:
            data (str): the report text.
            stamp (float): seconds since the drive started.

        Returns:
            Message: the message handed to every subscriber.
        """
        msg = Message(self.topic, self.node, self.source, data, stamp)
        self._bus.publish(msg)
        return msg


class Bus:
    """Topic -> subscriber callbacks, with the rclpy method names."""

    def __init__(self) -> None:
        """Create an empty bus (no topics, no subscribers)."""
        self._subscribers: dict[str, list[Callable[[Message], None]]] = {}
        self._lock = threading.Lock()

    def create_publisher(self, topic: str, node: str, source: str) -> Publisher:
        """Return a :class:`Publisher` for `topic` publishing as `node`/`source`."""
        return Publisher(self, topic, node, source)

    def create_subscription(self, topic: str, callback: Callable[[Message], None]) -> None:
        """Register `callback` to receive every message published on `topic`."""
        with self._lock:
            self._subscribers.setdefault(topic, []).append(callback)

    def publish(self, msg: Message) -> None:
        """Deliver `msg` to each subscriber of its topic (one failing sub can't stop the rest)."""
        with self._lock:
            callbacks = list(self._subscribers.get(msg.topic, ()))
        if not callbacks:
            logger.warning("no subscriber on %s; report dropped", msg.topic)
        for callback in callbacks:
            try:
                callback(msg)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.error("subscriber on %s failed: %s", msg.topic, exc)
