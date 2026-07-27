#!/bin/bash
# run_live.sh - RViz + HUD overlays against the LIVE autonomous stack (real time),
# instead of playing back a recorded rosbag (that's run_demo.sh).
#
# Use this when the real Autoware/driving stack is running and publishing topics
# on the ROS network right now. This script does NOT play a bag: it just brings
# up the same HUD overlay nodes + RViz and subscribes to whatever the live stack
# is publishing, on WALL time (use_sim_time:=false, no /clock).
#
# Differences vs run_demo.sh (bag playback):
#   - no `ros2 bag play`, no --start-paused / Resume gating
#   - everything runs on wall time (use_sim_time:=false); there is no /clock
#   - joins the LIVE domain (default: ROS_DOMAIN_ID=0) where the real stack lives
#   - publishes the ego model ONLY if /robot_description isn't already live
#   - no scripted event_publisher (its timings are tied to the fixed bag);
#     inject drone/infrastructure reports manually (see bottom of this file)
#
# Usage (from anywhere; the script cd's to the project root):
#   ./scripts/run_live.sh                 # terminal 1: RViz + HUD on the live stack
#   ./scripts/narration.sh                # terminal 2: narration inference
#
#   ROS_DOMAIN_ID=5 ./scripts/run_live.sh                 # join a different live domain
#   PATH_TOPIC=/my/path/topic ./scripts/run_live.sh       # wait on a different topic
#   WAIT=0 ./scripts/run_live.sh                          # start RViz immediately, don't wait
cd "$(dirname "$0")/.."   # project root (rviz_interface); all paths below are relative to it

# The LIVE stack publishes on the default domain (0). run_demo.sh deliberately
# isolates itself on domain 42 to avoid this rig; here we WANT to join it.
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
# Topic we wait for before starting RViz, so the planning displays actually
# subscribe (same reason as run_demo.sh). Override for a different stack.
PATH_TOPIC="${PATH_TOPIC:-/planning/scenario_planning/lane_driving/behavior_planning/path}"
WAIT="${WAIT:-1}"   # WAIT=0 -> skip the topic wait and start RViz immediately

# --- clean environment (snap/VS Code env pollution otherwise crashes rviz2/Qt) ---
unset GTK_PATH LOCPATH GIO_MODULE_DIR GTK_EXE_PREFIX GTK_IM_MODULE_FILE
unset GSETTINGS_SCHEMA_DIR GIO_LAUNCHED_DESKTOP_FILE GTK_IM_MODULE
unset LD_LIBRARY_PATH LD_PRELOAD
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_DATA_DIRS=/usr/share/ubuntu:/usr/share/gnome:/usr/local/share:/usr/share:/var/lib/snapd/desktop
export DISPLAY="${DISPLAY:-:1}"

source /opt/ros/humble/setup.bash

echo "Live mode: ROS_DOMAIN_ID=$ROS_DOMAIN_ID | path topic=$PATH_TOPIC"

# --- kill any previous instances of OUR nodes (leave the live stack alone!) ---
pkill -x rviz2 2>/dev/null
pkill -f "[f]actor_overlay.py" 2>/dev/null
pkill -f "[h]ud_relay.py" 2>/dev/null
sleep 1

# --- HUD relay: live vehicle state -> the *Report topics the speed/steering
#     overlay (SignalDisplay) reads. Wall time. ---
python3 src/nodes/hud_relay.py --ros-args -p use_sim_time:=false &
RELAY_PID=$!

# --- decision overlay: live Autoware planning factors -> 2D text HUD on
#     /hud/decision (what the narration node reads). Wall time. ---
python3 src/nodes/factor_overlay.py --ros-args -p use_sim_time:=false &
OVERLAY_PID=$!

# --- ego vehicle model: only publish it ourselves if the live stack isn't
#     already advertising /robot_description (otherwise we'd double-publish). ---
RSP_PID=""
if ros2 topic info /robot_description 2>/dev/null | grep -q "Publisher count: [1-9]"; then
  echo "/robot_description already live - not starting robot_state_publisher."
else
  echo "/robot_description not present - publishing ego model from src/rviz/ego_vehicle.urdf."
  ros2 run robot_state_publisher robot_state_publisher src/rviz/ego_vehicle.urdf \
    --ros-args -p use_sim_time:=false &
  RSP_PID=$!
fi

trap 'kill $RELAY_PID $OVERLAY_PID $RSP_PID $RVIZ_PID 2>/dev/null' EXIT

# --- WAIT until the live planning path topic is publishing, so RViz's planning
#     displays subscribe on startup. Skip with WAIT=0. ---
if [ "$WAIT" = "1" ]; then
  echo "Waiting for the live stack to publish $PATH_TOPIC (Ctrl+C to start RViz now)..."
  for i in $(seq 1 90); do
    if ros2 topic info "$PATH_TOPIC" 2>/dev/null | grep -q "Publisher count: [1-9]"; then
      echo "Planning topic is live (after ${i}s). Starting RViz."
      break
    fi
    sleep 1
  done
  sleep 1   # small settle so latched/transient-local topics are out
else
  echo "WAIT=0 - starting RViz immediately."
fi

# --- start RViz against the live topics ---
echo ">>> Starting RViz on the live stack. (Close RViz to stop this script.)"
rviz2 -d src/rviz/narration_chase.rviz --ros-args -p use_sim_time:=false >/tmp/rviz_live.log 2>&1 &
RVIZ_PID=$!

# --- block until RViz is closed; the EXIT trap then stops our overlay nodes ---
wait $RVIZ_PID

# --- Inject drone / infrastructure reports manually while this is running
#     (in another terminal, same ROS_DOMAIN_ID):
#   export ROS_DOMAIN_ID=0 && source /opt/ros/humble/setup.bash
#   ros2 topic pub --once /drone/reports std_msgs/String \
#     "data: 'an occluded pedestrian crossing behind the parked van on the right'"
#   ros2 topic pub --once /infrastructure/reports std_msgs/String \
#     "data: 'a construction zone 80 metres ahead, right lane closed'"
