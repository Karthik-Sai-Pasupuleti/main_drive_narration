"""Layer 2 - the three publisher nodes, driven by a dummy file instead of a rosbag.

config/dummy_events.json is the whole event source: node 1 (the infrastructure
pole) publishes at 30 s, node 2 (the drone) at 60 s and node 3 (the robot) at
90 s. Each entry becomes a publisher on its own topic, scheduled on the drive
clock; layer 3 subscribes and never learns where the messages came from, so
replacing this file with real ROS 2 publishers changes nothing downstream.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from core.bus import Bus, Publisher
from core.scheduler import Timeline

logger = logging.getLogger(__name__)

REQUIRED = ("node", "source", "topic", "at", "message")


def load_events(path: str | Path) -> list[dict]:
    """Read the dummy event file.

    Args:
        path (str | Path): JSON with {"events": [{"node", "source", "topic", "at",
            "message"}]}.

    Returns:
        list[dict]: the events, earliest first.

    Raises:
        ValueError: if an entry is missing one of the required keys.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    events = data["events"] if isinstance(data, dict) else data
    for event in events:
        missing = [key for key in REQUIRED if key not in event]
        if missing:
            raise ValueError(f"event entry is missing {missing}: {event}")
    return sorted(events, key=lambda e: float(e["at"]))


class EventPublishers:
    """One publisher node per entry in the dummy event file."""

    def __init__(self, bus: Bus, events: list[dict]) -> None:
        """Create a publisher for every event.

        Args:
            bus (Bus): the bus layer 3 subscribes on.
            events (list[dict]): entries from :func:`load_events`.
        """
        self._events = events
        self._publishers: dict[str, Publisher] = {
            event["node"]: bus.create_publisher(event["topic"], event["node"], event["source"])
            for event in events}

    @property
    def topics(self) -> list[str]:
        """The distinct topics these nodes publish on, in file order."""
        return list(dict.fromkeys(event["topic"] for event in self._events))

    def register(self, timeline: Timeline) -> None:
        """Schedule each node's publication on `timeline`."""
        for event in self._events:
            node, source, message = event["node"], event["source"], event["message"]
            timeline.add(float(event["at"]),
                         f"LAYER 2  {node} ({source}) -> {event['topic']}",
                         lambda drive_time, n=node, m=message: self._publish(n, m, drive_time))

    def _publish(self, node: str, message: str, drive_time: float) -> None:
        """Publish one node's report on its topic."""
        publisher = self._publishers[node]
        logger.info("LAYER 2 %s publishes on %s: %s", node, publisher.topic, message)
        publisher.publish(message, drive_time)
