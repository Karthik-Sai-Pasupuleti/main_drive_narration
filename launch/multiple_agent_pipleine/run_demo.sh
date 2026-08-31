#!/bin/bash
# run_demo.sh - rosbag playback + BEV & perspective RViz + HUD for the multi-agent
# pipeline. Use THIS (not run_live.sh) with a looping bag: RViz runs on sim time
# (use_sim_time:=true + the bag's --clock) so it resets its TF buffer when the bag
# loops - otherwise the ego freezes after the first loop.
#
# Opens TWO RViz windows (narration_bev + narration_chase) - place them side by
# side so neither covers the other (frames are captured by window region).
#
#   ./launch/multiple_agent_pipleine/run_demo.sh   # terminal 1
#   ./launch/multiple_agent_pipleine/narration.sh  # terminal 2
#
#   BAG=/path/to/other_bag ./launch/multiple_agent_pipleine/run_demo.sh
#   DUMMY_EVENTS=0 ./launch/multiple_agent_pipleine/run_demo.sh   # no test reports
cd "$(dirname "$0")/../.."   # project root

# BAG="${BAG:-../rosbags-HMI/Rosbag_with_planner/rosbag2_2026_05_28-13_40_16}"
BAG="${BAG:-$HOME/Downloads/rosbag2_2026_08_06-16_20_58}"
PATH_TOPIC="/planning/scenario_planning/lane_driving/behavior_planning/path"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"

# clean snap/VS Code env pollution that crashes rviz2/Qt
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
echo "Demo mode (multi-agent): ROS_DOMAIN_ID=$ROS_DOMAIN_ID | bag=$BAG"

pkill -x rviz2 2>/dev/null
pkill -f "[r]os2 bag play" 2>/dev/null
pkill -f "robot_state_publisher .*ego_vehicle.urdf" 2>/dev/null
pkill -f "[f]actor_overlay.py" 2>/dev/null
pkill -f "[h]ud_relay.py" 2>/dev/null
pkill -f "[d]ummy_events.py" 2>/dev/null
sleep 2

# Clear stale FastDDS shared-memory segments left by prior (often killed) runs.
# They pile up in /dev/shm and make DDS discovery crawl, which stalls the
# "wait for planning topics" loop below -> RViz takes ~90s to appear. Safe here:
# the stack was just pkilled, so nothing of ours is using them.
ros2 daemon stop >/dev/null 2>&1
rm -f /dev/shm/fastrtps_* /dev/shm/sem.fastrtps_* /dev/shm/fastdds_* 2>/dev/null

# --- play the bag LOOPING, PAUSED until ENTER, publishing /clock ---
ros2 bag play "$BAG" --clock 100 --loop --start-paused &
BAG_PID=$!

# --- HUD overlay nodes: WALL time, so the looping bag can't stall them ---
python3 src/nodes/hud_relay.py --ros-args -p use_sim_time:=false &
RELAY_PID=$!
python3 src/nodes/factor_overlay.py --ros-args -p use_sim_time:=false &
OVERLAY_PID=$!

# --- ego model: SIM time (the bag carries /tf but not /robot_description) ---
ros2 run robot_state_publisher robot_state_publisher src/rviz/ego_vehicle.urdf \
  --ros-args -p use_sim_time:=true &
RSP_PID=$!

# # --- dummy drone/infra reports for testing (DUMMY_EVENTS=0 to disable) ---
# DUMMY_PID=""
# if [ "${DUMMY_EVENTS:-1}" = "1" ]; then
#   python3 src/nodes/dummy_events.py &
#   DUMMY_PID=$!
# fi

trap 'kill $BAG_PID $RELAY_PID $OVERLAY_PID $RSP_PID $DUMMY_PID $BEV_PID $PERSP_PID 2>/dev/null' EXIT

# --- wait until the paused player advertises the planning path topic ---
echo "Waiting for the bag player to advertise planning topics..."
for i in $(seq 1 90); do
  if ros2 topic info "$PATH_TOPIC" 2>/dev/null | grep -q "Publisher count: [1-9]"; then
    echo "Planning topics are live (after ${i}s). Starting RViz."
    break
  fi
  sleep 1
done
sleep 2

# --- start BOTH RViz views on SIM time (resets TF on loop -> ego keeps moving) ---
echo ">>> Starting RViz (BEV + perspective). Place them side by side."
rviz2 -d src/rviz/narration_bev.rviz --ros-args -p use_sim_time:=true >/tmp/rviz_bev.log 2>&1 &
BEV_PID=$!
rviz2 -d src/rviz/narration_chase.rviz --ros-args -p use_sim_time:=true >/tmp/rviz_chase.log 2>&1 &
PERSP_PID=$!

# --- gate playback on ENTER, then resume the player ---
RESUME_SVC=$(ros2 service list 2>/dev/null | grep -m1 -E '/resume$')
read -r -p $'\n>>> RViz is up (paused). Press ENTER to start the drive... '
if [ -n "$RESUME_SVC" ] && ros2 service call "$RESUME_SVC" rosbag2_interfaces/srv/Resume >/dev/null 2>&1; then
  echo "Drive started. (Close both RViz windows to stop.)"
else
  echo "WARN: could not resume via service; press SPACE in the bag terminal."
fi

wait $BEV_PID $PERSP_PID
