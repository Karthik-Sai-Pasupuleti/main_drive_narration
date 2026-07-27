#!/usr/bin/env python3
"""
HUD relay: the bag records vehicle state as ADAPI / Odometry messages, but the
autoware_overlay_rviz_plugin SignalDisplay (the speed + steering-wheel HUD) reads
autoware_vehicle_msgs/*Report topics. This node converts:

  /localization/kinematic_state (nav_msgs/Odometry)        -> /vehicle/status/velocity_status
  /api/vehicle/status (autoware_adapi_v1_msgs/VehicleStatus) -> /vehicle/status/steering_status
                                                              -> /vehicle/status/gear_status
                                                              -> /vehicle/status/turn_indicators_status
                                                              -> /vehicle/status/hazard_lights_status

Output messages are stamped from a steady SYSTEM_TIME clock (NOT the bag's sim
time). This is deliberate: with ``ros2 bag play --loop`` the sim clock jumps
backwards at every loop boundary, and the SignalDisplay overlay then treats the
"older" stamps as stale and stops refreshing — so the HUD vanishes from the 2nd
loop onward. A monotonic wall-clock stamp keeps the overlay updating across
loops. The displayed values come straight from the bag, so they stay correct.
"""
import math
import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from nav_msgs.msg import Odometry
from autoware_adapi_v1_msgs.msg import VehicleStatus
from autoware_vehicle_msgs.msg import (
    VelocityReport, SteeringReport, GearReport,
    TurnIndicatorsReport, HazardLightsReport,
)

# ADAPI Gear.status -> autoware_vehicle_msgs/GearReport.report
GEAR_MAP = {0: 0, 1: 1, 2: 2, 3: 20, 4: 22, 5: 23}

WHEELBASE = 2.99      # Audi Q8 wheelbase [m]
MAX_STEER = 0.61      # clamp [rad] (~35 deg)
MIN_SPEED = 0.5       # below this, steering estimate is unreliable [m/s]


class HudRelay(Node):
    def __init__(self):
        super().__init__('hud_relay')
        # Steady wall clock for output stamps — independent of /clock, so it
        # never jumps backwards when the bag loops (see module docstring).
        self._wall = Clock(clock_type=ClockType.SYSTEM_TIME)
        self.vel = self.create_publisher(VelocityReport, '/vehicle/status/velocity_status', 10)
        self.steer = self.create_publisher(SteeringReport, '/vehicle/status/steering_status', 10)
        self.gear = self.create_publisher(GearReport, '/vehicle/status/gear_status', 10)
        self.turn = self.create_publisher(TurnIndicatorsReport, '/vehicle/status/turn_indicators_status', 10)
        self.haz = self.create_publisher(HazardLightsReport, '/vehicle/status/hazard_lights_status', 10)
        # Permissive best-effort QoS: a best-effort subscriber can receive from
        # both reliable and best-effort publishers, so it matches whatever the
        # bag offers (kinematic_state is reliable, /api/vehicle/status best-effort).
        be = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT,
                        durability=DurabilityPolicy.VOLATILE, history=HistoryPolicy.KEEP_LAST)
        self.create_subscription(Odometry, '/localization/kinematic_state', self.on_odom, be)
        self.create_subscription(VehicleStatus, '/api/vehicle/status', self.on_status, be)
        self.get_logger().info('hud_relay running: kinematic_state + /api/vehicle/status -> /vehicle/status/*')

    def on_odom(self, msg):
        vx = float(msg.twist.twist.linear.x)
        wz = float(msg.twist.twist.angular.z)
        stamp = self._wall.now().to_msg()

        v = VelocityReport()
        v.header = msg.header
        v.header.stamp = stamp
        v.longitudinal_velocity = vx
        v.lateral_velocity = float(msg.twist.twist.linear.y)
        v.heading_rate = wz
        self.vel.publish(v)

        # Steering angle isn't recorded in this bag, so derive it from the
        # bicycle model using the recorded yaw-rate and speed:
        #   omega = v * tan(delta) / L   ->   delta = atan(omega * L / v)
        if abs(vx) > MIN_SPEED:
            delta = math.atan(wz * WHEELBASE / vx)
            delta = max(-MAX_STEER, min(MAX_STEER, delta))
        else:
            delta = 0.0
        s = SteeringReport()
        s.stamp = self._wall.now().to_msg()
        s.steering_tire_angle = float(delta)
        self.steer.publish(s)

    def on_status(self, msg):
        stamp = self._wall.now().to_msg()

        s = SteeringReport()
        s.stamp = stamp
        s.steering_tire_angle = float(msg.steering_tire_angle)
        self.steer.publish(s)

        g = GearReport()
        g.stamp = stamp
        g.report = GEAR_MAP.get(msg.gear.status, 0)
        self.gear.publish(g)

        t = TurnIndicatorsReport()
        t.stamp = stamp
        t.report = msg.turn_indicators.status if msg.turn_indicators.status in (1, 2, 3) else 1
        self.turn.publish(t)

        h = HazardLightsReport()
        h.stamp = stamp
        h.report = msg.hazard_lights.status if msg.hazard_lights.status in (1, 2) else 1
        self.haz.publish(h)


def main():
    rclpy.init()
    node = HudRelay()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
