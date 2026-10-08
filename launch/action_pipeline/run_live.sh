#!/bin/bash
# run_live.sh - OPTIONAL pre-flight check for live-stack usage.
#
# main.py subscribes directly to the raw planning/mobile-pole topics now (no
# relay node to start), so for a real live stack you genuinely only need ONE
# command:
#   ./launch/action_pipeline/narration.sh
# This script starts no background process and isn't required - it just
# confirms the live stack is actually publishing before you bother starting
# the narrator.
#
# Usage:
#   ./launch/action_pipeline/run_live.sh                  # check, then exit
#   ROS_DOMAIN_ID=5 ./launch/action_pipeline/run_live.sh   # check a different domain
#   PATH_TOPIC=/my/path/topic ./launch/action_pipeline/run_live.sh
cd "$(dirname "$0")/../.."   # project root

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
PATH_TOPIC="${PATH_TOPIC:-/planning/scenario_planning/lane_driving/behavior_planning/path}"

# --- clean environment (VS Code injects an LD_LIBRARY_PATH that can break
#     rclpy's compiled extensions) ---
unset LD_LIBRARY_PATH LD_PRELOAD

source /opt/ros/humble/setup.bash

echo "Checking for a live stack on ROS_DOMAIN_ID=$ROS_DOMAIN_ID ..."
for i in $(seq 1 10); do
  if ros2 topic info "$PATH_TOPIC" 2>/dev/null | grep -q "Publisher count: [1-9]"; then
    echo "OK: $PATH_TOPIC is live (after ${i}s)."
    echo "Run ./launch/action_pipeline/narration.sh whenever you're ready."
    exit 0
  fi
  sleep 1
done
echo "WARN: no publisher seen on $PATH_TOPIC after 10s - is the live stack actually running on ROS_DOMAIN_ID=$ROS_DOMAIN_ID?"
exit 1
