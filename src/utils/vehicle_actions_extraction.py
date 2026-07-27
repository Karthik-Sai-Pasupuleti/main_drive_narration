"""Reads the decision HUD and emits route-deviation events.

Subscribes to /hud/decision (published by nodes/factor_overlay.py), keeps a
short memory of the vehicle's actions, and when the car takes a left/right turn
at an intersection, fires the on_route_deviation callback. It does NOT know
about the VLM or TTS - the orchestrator in main.py wires those to the callback.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Callable

from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                       ReliabilityPolicy)
from rviz_2d_overlay_msgs.msg import OverlayText

# "TURN LEFT (intersection)" upcoming, "TURNING LEFT (intersection)" executing.
TURN_TAG = re.compile(r"\b(turn|turning)\s+(left|right)\s*\(([^)]*)\)", re.IGNORECASE)
# Speed and distance change every frame; strip them to tell a new action apart.
VOLATILE = re.compile(r"SPEED:\s*[-\d.]+\s*km/?h|[-\d.]+\s*m\b", re.IGNORECASE)


class VehicleActionsExtractor(Node):
    """Detects intersection turns on the decision HUD and emits them."""

    def __init__(self, on_route_deviation: Callable[[str, str], None] | None = None,
                 hud_topic: str = "/hud/decision", action_memory: int = 40) -> None:
        """Subscribe to the HUD and register the deviation callback.

        Args:
            on_route_deviation (Callable[[str, str], None], optional): called with
                (direction, phase) at the start of each intersection turn.
            hud_topic (str, optional): the OverlayText topic to read.
            action_memory (int, optional): max distinct actions kept as context.
        """
        super().__init__("vehicle_actions_extractor")
        self.on_route_deviation = on_route_deviation
        self.actions: deque[str] = deque(maxlen=action_memory)

        self._last_action: str | None = None
        self._turn_dir: str | None = None
        self._turn_exec = False

        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT,
                         durability=DurabilityPolicy.VOLATILE,
                         history=HistoryPolicy.KEEP_LAST)
        self.create_subscription(OverlayText, hud_topic, self._on_hud, qos)
        self.get_logger().info(
            f"vehicle_actions_extractor running: {hud_topic} -> route-deviation "
            f"events ({'callback attached' if on_route_deviation else 'detection-only'})")

    def _on_hud(self, msg: OverlayText) -> None:
        text = re.sub(r"\s*\n\s*", " | ", msg.text.strip())
        if not text:
            return
        # The HUD repeats at ~5 Hz; only act when the action itself changes.
        action = self._action_key(text)
        if action == self._last_action:
            return
        self._last_action = action
        self.actions.append(text)
        self.process_actions(text)

    def process_actions(self, text: str) -> None:
        """Emit a route-deviation event if the newest action is an intersection turn."""
        turn = self._detect_turn(text)
        if turn:
            direction, phase = turn
            self.get_logger().info(f"route deviation: {direction} turn ({phase})")
            if self.on_route_deviation is not None:
                self.on_route_deviation(direction, phase)

    def get_actions(self) -> list[str]:
        """Return the rolling list of distinct actions, oldest first."""
        return list(self.actions)

    @staticmethod
    def _action_key(text: str) -> str:
        """The action without its speed/distance, so repeats collapse to one key."""
        key = re.sub(r"\(\s*\)", "", VOLATILE.sub("", text))
        return re.sub(r"\s{2,}", " ", key).strip(" |")

    def _detect_turn(self, text: str):
        """Return (direction, phase) once per intersection turn, else None."""
        match = TURN_TAG.search(text)
        if match and "intersection" in match.group(3).lower():
            direction = match.group(2).lower()
            executing = match.group(1).lower() == "turning"
            if direction != self._turn_dir:          # a different turn
                self._turn_dir, self._turn_exec = direction, executing
                return direction, ("executing" if executing else "upcoming")
            if executing:                            # same turn, now executing
                self._turn_exec = True
                return None
            if self._turn_exec:                      # TURN after TURNING = next turn
                self._turn_exec = False
                return direction, "upcoming"
            return None                              # still approaching the same turn
        self._turn_dir, self._turn_exec = None, False
        return None

