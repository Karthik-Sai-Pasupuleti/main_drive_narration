#!/bin/bash
# run_demo.sh - rosbag playback + RViz + HUD overlays (the recorded demo).
#
# Plays the planner bag LOOPING but PAUSED so RViz can subscribe before data
# flows, brings up the HUD overlay nodes + ego model + RViz, resumes on ENTER,
# then launches the timestamped audio cues (cue_publisher.py).
# Run ./launch/narration.sh in another terminal for the spoken narration.
#
# Usage (from anywhere; the script cd's to the project root):
#   ./launch/run_demo.sh          # terminal 1: bag + RViz + HUD overlays
#   ./launch/narration.sh         # terminal 2: narration inference
#
#   BAG=/path/to/other_bag ./launch/run_demo.sh
#   RVIZ_CONFIG=narration_bev.rviz ./launch/run_demo.sh
cd "$(dirname "$0")/../.."   # project root; all paths below are relative to it

# The planner bag has the planning factors the overlays need (the rosbags-HMI
# camera bags do NOT). Override with BAG=... for a different recording.
BAG="${BAG:-$HOME/Downloads/rosbag2_2026_08_06-16_20_58}"
RVIZ_CONFIG="${RVIZ_CONFIG:-narration_chase.rviz}"   # or narration_bev.rviz
PATH_TOPIC="/planning/scenario_planning/lane_driving/behavior_planning/path"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"   # match narration.sh

# --- clean environment (snap/VS Code env pollution otherwise crashes rviz2/Qt) ---
unset GTK_PATH LOCPATH GIO_MODULE_DIR GTK_EXE_PREFIX GTK_IM_MODULE_FILE
unset GSETTINGS_SCHEMA_DIR GIO_LAUNCHED_DESKTOP_FILE GTK_IM_MODULE
unset LD_LIBRARY_PATH LD_PRELOAD
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_DATA_DIRS=/usr/share/ubuntu:/usr/share/gnome:/usr/local/share:/usr/share:/var/lib/snapd/desktop
export DISPLAY="${DISPLAY:-:1}"

source /opt/ros/humble/setup.bash

if [ ! -e "$BAG/metadata.yaml" ]; then
  echo "ERROR: no bag at '$BAG' (expected a directory with metadata.yaml)."
  exit 1
fi
echo "Demo mode: ROS_DOMAIN_ID=$ROS_DOMAIN_ID | rviz=$RVIZ_CONFIG | bag=$BAG"

# --- kill any previous instances (no duplicates -> no flicker) ---
pkill -x rviz2 2>/dev/null
pkill -f "[r]os2 bag play" 2>/dev/null
pkill -f "robot_state_publisher .*ego_vehicle.urdf" 2>/dev/null
pkill -f "[f]actor_overlay.py" 2>/dev/null
pkill -f "[h]ud_relay.py" 2>/dev/null
pkill -f "[e]vent_publisher.py" 2>/dev/null
sleep 2

# --- play the bag LOOPING, but PAUSED until the user presses ENTER ---
ros2 bag play "$BAG" --clock 100 --loop --start-paused &
BAG_PID=$!

# --- HUD overlay nodes (wall time, so the looping bag can't stall them) ---
python3 src/nodes/hud_relay.py --ros-args -p use_sim_time:=false &
RELAY_PID=$!
python3 src/nodes/factor_overlay.py --ros-args -p use_sim_time:=false &
OVERLAY_PID=$!

# --- ego vehicle model (the bag carries /tf but not /robot_description) ---
ros2 run robot_state_publisher robot_state_publisher src/rviz/ego_vehicle.urdf \
  --ros-args -p use_sim_time:=true &
RSP_PID=$!

trap 'kill $BAG_PID $RELAY_PID $OVERLAY_PID $RSP_PID $RVIZ_PID $CUE_PID 2>/dev/null' EXIT

# --- WAIT until the paused player advertises the planning path topic ---
echo "Waiting for the bag player to advertise planning topics..."
for i in $(seq 1 90); do
  if ros2 topic info "$PATH_TOPIC" 2>/dev/null | grep -q "Publisher count: [1-9]"; then
    echo "Planning topics are live (after ${i}s). Starting RViz."
    break
  fi
  sleep 1
done
sleep 2   # settle so latched/transient-local topics are out

# --- start RViz (subscribes while the bag is still paused) ---
rviz2 -d "src/rviz/$RVIZ_CONFIG" --ros-args -p use_sim_time:=true >/tmp/rviz_demo.log 2>&1 &
RVIZ_PID=$!

# --- gate playback on ENTER, then resume via the player's Resume service ---
RESUME_SVC=$(ros2 service list 2>/dev/null | grep -m1 -E '/resume$')
read -r -p $'\n>>> RViz is up (paused). Press ENTER to start the drive... '
if [ -n "$RESUME_SVC" ] && ros2 service call "$RESUME_SVC" rosbag2_interfaces/srv/Resume >/dev/null 2>&1; then
  echo "Drive started. (Close RViz to stop.)"
else
  echo "WARN: could not resume via service; press SPACE in the bag terminal."
fi

# --- scripted audio cues, fired on rosbag /clock time (t=0 = drive start) ---
# Anchors live in src/configs/audio_config/*.json; build the WAVs first with
#   python src/utils/prerender.py
# main.py plays them, so it must be running (./launch/action_pipeline/narration.sh).
python3 src/nodes/cue_publisher.py --ros-args -p use_sim_time:=false &
CUE_PID=$!

# --- block until RViz is closed; the EXIT trap then stops everything ---
wait $RVIZ_PID
