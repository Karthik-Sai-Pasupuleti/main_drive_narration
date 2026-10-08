#!/bin/bash
# run_demo.sh - rosbag playback (the recorded demo). No HUD-relay node, no
# RViz: main.py subscribes to the raw planning/mobile-pole topics directly, so
# this script's only job is getting a bag's topics flowing.
#
# Plays the planner bag LOOPING but PAUSED, resumes on ENTER. Run
# ./launch/narration.sh in another terminal for the spoken narration.
#
# Usage (from anywhere; the script cd's to the project root):
#   ./launch/action_pipeline/run_demo.sh          # terminal 1: bag playback
#   ./launch/action_pipeline/narration.sh         # terminal 2: narration inference
#
#   BAG=/path/to/other_bag ./launch/action_pipeline/run_demo.sh
cd "$(dirname "$0")/../.."   # project root; all paths below are relative to it

# The planner bag has the planning factors main.py needs (the rosbags-HMI
# camera bags do NOT). Override with BAG=... for a different recording.
BAG="${BAG:-../rosbags-HMI/Rosbag_with_planner/rosbag2_2026_05_28-13_40_16}"
PATH_TOPIC="/planning/scenario_planning/lane_driving/behavior_planning/path"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"   # match narration.sh

# --- clean environment (VS Code injects an LD_LIBRARY_PATH that can break
#     rclpy's compiled extensions) ---
unset LD_LIBRARY_PATH LD_PRELOAD

source /opt/ros/humble/setup.bash

if [ ! -e "$BAG/metadata.yaml" ]; then
  echo "ERROR: no bag at '$BAG' (expected a directory with metadata.yaml)."
  exit 1
fi
echo "Demo mode: ROS_DOMAIN_ID=$ROS_DOMAIN_ID | bag=$BAG"

# --- kill any previous instance (no duplicates) ---
pkill -f "[r]os2 bag play" 2>/dev/null
sleep 2

# --- play the bag LOOPING, but PAUSED until the user presses ENTER ---
ros2 bag play "$BAG" --clock 100 --loop --start-paused &
BAG_PID=$!

trap 'kill $BAG_PID 2>/dev/null' EXIT

# --- WAIT until the paused player advertises the planning path topic ---
echo "Waiting for the bag player to advertise planning topics..."
for i in $(seq 1 90); do
  if ros2 topic info "$PATH_TOPIC" 2>/dev/null | grep -q "Publisher count: [1-9]"; then
    echo "Planning topics are live (after ${i}s)."
    break
  fi
  sleep 1
done
sleep 2   # settle so latched/transient-local topics are out

# --- gate playback on ENTER, then resume via the player's Resume service ---
RESUME_SVC=$(ros2 service list 2>/dev/null | grep -m1 -E '/resume$')
read -r -p $'\n>>> Ready (paused). Press ENTER to start the drive... '
if [ -n "$RESUME_SVC" ] && ros2 service call "$RESUME_SVC" rosbag2_interfaces/srv/Resume >/dev/null 2>&1; then
  echo "Drive started. (Ctrl+C to stop.)"
else
  echo "WARN: could not resume via service; press SPACE in the bag terminal."
fi

# --- block until the (looping) bag is interrupted; the EXIT trap then stops it ---
wait $BAG_PID
