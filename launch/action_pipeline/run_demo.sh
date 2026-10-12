#!/bin/bash
# run_demo.sh - rosbag playback (the recorded demo). No relay node, no RViz:
# main.py subscribes to the raw mobile-pole/robot topics directly, so this
# script's only job is getting a bag's topics flowing.
#
# Plays the bag LOOPING but PAUSED, resumes on ENTER. Run
# ./launch/narration.sh in another terminal for the spoken narration.
#
# Usage (from anywhere; the script cd's to the project root):
#   ./launch/action_pipeline/run_demo.sh          # terminal 1: bag playback
#   ./launch/action_pipeline/narration.sh         # terminal 2: narration inference
#
#   BAG=/path/to/other_bag ./launch/action_pipeline/run_demo.sh
cd "$(dirname "$0")/../.."   # project root; all paths below are relative to it

# The pole-infra bag has the mobile-pole detections + camera feed main.py
# needs. Override with BAG=... for a different recording.
BAG="${BAG:-$HOME/Downloads/DEMO-ROS-BAGS/POLE-INFRA/rosbag2_2026_08_20-17_09_42}"
GATE_TOPIC="/mobile_pole/axis_rgb_6_42/autoware_objects_3d"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"   # match narration.sh

# --- clean environment (VS Code injects an LD_LIBRARY_PATH that can break
#     rclpy's compiled extensions) ---
unset LD_LIBRARY_PATH LD_PRELOAD

source /opt/ros/humble/setup.bash

if [ ! -e "$BAG/metadata.yaml" ]; then
  echo "ERROR: no bag at '$BAG' (expected a directory with metadata.yaml)."
  exit 1
fi
# RViz runs on sim time, so /clock has to come from exactly one place. Bags
# that recorded /clock (the POLE-INFRA one did) replay it themselves; adding
# --clock on top gives two publishers and time jumps between them. Only
# synthesize a clock for bags that lack one.
if grep -q "name: /clock" "$BAG/metadata.yaml"; then
  CLOCK_ARG=""
  echo "Demo mode: ROS_DOMAIN_ID=$ROS_DOMAIN_ID | bag=$BAG | /clock from the bag"
else
  CLOCK_ARG="--clock 100"
  echo "Demo mode: ROS_DOMAIN_ID=$ROS_DOMAIN_ID | bag=$BAG | /clock synthesized"
fi

# --- kill any previous instance (no duplicates) ---
pkill -f "[r]os2 bag play" 2>/dev/null
sleep 2

# Looping is OFF by default. When the bag restarts, every /tf stamp jumps
# backwards by the bag's whole duration; tf2's buffer only keeps a 10s window,
# so it drops all of it as TF_OLD_DATA and keeps serving the stale
# end-of-run pose. The narrator's distance gate then compares hazards against
# an ego vehicle that never moves again - it floods the console AND silently
# stops triggering. Set LOOP=1 only if you don't need TF.
LOOP_ARG=""
[ "${LOOP:-0}" = "1" ] && LOOP_ARG="--loop" && echo "WARNING: --loop breaks TF after the first lap"

# --- play the bag PAUSED until the user presses ENTER ---
ros2 bag play "$BAG" $CLOCK_ARG $LOOP_ARG --start-paused &
BAG_PID=$!

trap 'kill $BAG_PID 2>/dev/null' EXIT

# --- WAIT until the paused player advertises the hazard detections topic ---
echo "Waiting for the bag player to advertise the hazard topics..."
for i in $(seq 1 90); do
  if ros2 topic info "$GATE_TOPIC" 2>/dev/null | grep -q "Publisher count: [1-9]"; then
    echo "Hazard topics are live (after ${i}s)."
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
