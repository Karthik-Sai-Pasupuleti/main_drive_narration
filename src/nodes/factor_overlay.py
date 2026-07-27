#!/usr/bin/env python3
"""
Decision overlay: turns Autoware's planning factors into a compact, always-on
2D text HUD so the *reason* the vehicle is acting (stopping / slowing / turning
and WHY) is legible to a downstream VLM — WITHOUT the virtual-wall markers that
otherwise stand across the lane and occlude the trajectory in the 3D view.

Source topics (the aggregated ADAPI output — Autoware already prioritises them):

  /api/planning/velocity_factors (autoware_adapi_v1_msgs/VelocityFactorArray)
        -> STOPPING / SLOWING + reason + distance
  /api/planning/steering_factors (autoware_adapi_v1_msgs/SteeringFactorArray)
        -> TURN / LANE CHANGE LEFT|RIGHT + reason

Output:

  /hud/decision (rviz_2d_overlay_msgs/OverlayText)  -> rendered by the
        rviz_2d_overlay_plugins/TextOverlay display ("DecisionOverlay").

Like hud_relay.py this runs on WALL time (use_sim_time:=false): it only needs
its own staleness clock, and staying off /clock means the looping bag (whose sim
time jumps backwards) can't make the overlay freeze or vanish on the 2nd loop.
"""
import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from std_msgs.msg import ColorRGBA
from nav_msgs.msg import Odometry
from autoware_adapi_v1_msgs.msg import VelocityFactorArray, SteeringFactorArray
from rviz_2d_overlay_msgs.msg import OverlayText

# VelocityFactor.status
APPROACHING, STOPPED = 1, 2
# SteeringFactor.direction / status
DIR_LEFT, DIR_RIGHT = 1, 2
STEER_APPROACHING, STEER_TURNING = 1, 3

# A factor older than this (wall seconds) is treated as gone -> "DRIVING".
STALE_SEC = 1.5

COLORS = {                       # r, g, b
    'STOP':  (1.0, 0.27, 0.27),  # red
    'SLOW':  (1.0, 0.76, 0.10),  # amber
    'TURN':  (0.35, 0.80, 1.0),  # cyan
    'GO':    (0.40, 1.0, 0.55),  # green
}


def pretty(s: str) -> str:
    """'obstacle_stop' -> 'obstacle stop'; '' -> ''."""
    return (s or '').replace('_', ' ').strip()


class FactorOverlay(Node):
    def __init__(self):
        super().__init__('factor_overlay')
        self._wall = Clock(clock_type=ClockType.SYSTEM_TIME)
        self._vel = []           # latest list of VelocityFactor
        self._steer = []         # latest list of SteeringFactor
        self._vel_t = 0.0        # wall time of last velocity msg
        self._steer_t = 0.0      # wall time of last steering msg
        self._speed = 0.0        # latest longitudinal speed [m/s]
        self._speed_t = 0.0      # wall time of last odom msg

        # Permissive subscriber QoS: best-effort + volatile matches whatever the
        # bag offers for these ADAPI topics (reliable or best-effort).
        be = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT,
                        durability=DurabilityPolicy.VOLATILE, history=HistoryPolicy.KEEP_LAST)
        self.create_subscription(VelocityFactorArray, '/api/planning/velocity_factors',
                                 self.on_velocity, be)
        self.create_subscription(SteeringFactorArray, '/api/planning/steering_factors',
                                 self.on_steering, be)
        self.create_subscription(Odometry, '/localization/kinematic_state',
                                 self.on_odom, be)

        # Reliable publisher to match the TextOverlay display's QoS.
        rel = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.VOLATILE, history=HistoryPolicy.KEEP_LAST)
        self.pub = self.create_publisher(OverlayText, '/hud/decision', rel)

        # Render at a steady rate regardless of input cadence / loop jumps.
        self.create_timer(0.2, self.render)
        self.get_logger().info(
            'factor_overlay running: velocity_factors + steering_factors -> /hud/decision')

    def _now(self):
        t = self._wall.now().to_msg()
        return t.sec + t.nanosec * 1e-9

    def on_velocity(self, msg):
        self._vel = list(msg.factors)
        self._vel_t = self._now()

    def on_steering(self, msg):
        self._steer = list(msg.factors)
        self._steer_t = self._now()

    def on_odom(self, msg):
        self._speed = float(msg.twist.twist.linear.x)
        self._speed_t = self._now()

    def _velocity_decision(self):
        """-> (key, headline, detail_line) or None."""
        if self._now() - self._vel_t > STALE_SEC:
            return None
        stopped = [f for f in self._vel if f.status == STOPPED]
        approaching = [f for f in self._vel if f.status == APPROACHING]
        if stopped:
            f = min(stopped, key=lambda f: f.distance)
            why = pretty(f.behavior) or pretty(f.detail) or 'obstacle'
            return 'STOP', 'STOPPING', f'why: {why}'
        if approaching:
            f = min(approaching, key=lambda f: f.distance)
            why = pretty(f.behavior) or pretty(f.detail) or 'ahead'
            return 'SLOW', 'SLOWING', f'why: {why}  ({f.distance:.0f} m)'
        return None

    def _steering_decision(self):
        """-> detail_line or None."""
        if self._now() - self._steer_t > STALE_SEC:
            return None
        active = [f for f in self._steer
                  if f.direction in (DIR_LEFT, DIR_RIGHT)
                  and f.status in (STEER_APPROACHING, STEER_TURNING)]
        if not active:
            return None
        f = min(active, key=lambda f: f.distance[0] if len(f.distance) else 0.0)
        side = 'LEFT' if f.direction == DIR_LEFT else 'RIGHT'
        why = pretty(f.behavior)
        verb = 'TURNING' if f.status == STEER_TURNING else 'TURN'
        line = f'{verb} {side}'
        if why:
            line += f' ({why})'
        return line

    def render(self):
        vel = self._velocity_decision()
        steer = self._steering_decision()

        if vel:
            key, headline, detail = vel
            lines = [headline, detail]
            if steer:
                lines.append(steer)
        elif steer:
            key, lines = 'TURN', ['TURNING', steer]
        else:
            key, lines = 'GO', ['DRIVING']

        # Numeric speed line on top, so the VLM can read it as text (the
        # graphical SignalDisplay speedometer is a separate, image-only HUD).
        if self._now() - self._speed_t <= STALE_SEC:
            lines.insert(0, f'SPEED: {abs(self._speed) * 3.6:.0f} km/h')

        r, g, b = COLORS[key]
        msg = OverlayText()
        msg.action = OverlayText.ADD
        msg.horizontal_alignment = OverlayText.LEFT
        msg.vertical_alignment = OverlayText.TOP
        msg.horizontal_distance = 20
        msg.vertical_distance = 140
        msg.width = 460
        msg.height = 140
        msg.text_size = 16.0
        msg.line_width = 2
        msg.font = 'DejaVu Sans Mono'
        msg.bg_color = ColorRGBA(r=0.0, g=0.0, b=0.0, a=0.55)
        msg.fg_color = ColorRGBA(r=r, g=g, b=b, a=1.0)
        msg.text = '\n'.join(lines)
        self.pub.publish(msg)


def main():
    rclpy.init()
    node = FactorOverlay()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
