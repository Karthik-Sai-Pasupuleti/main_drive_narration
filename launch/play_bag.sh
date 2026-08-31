#!/bin/bash
# play_bag.sh - just the rosbag + RViz. No HUD overlays, no narration pipeline.
# Use this when you only want to LOOK at the bag (infra, objects, path, ego).
#
#   ./launch/play_bag.sh                       # BEV view
#   VIEW=chase ./launch/play_bag.sh            # perspective/chase view
#   VIEW=both  ./launch/play_bag.sh            # both windows, side by side
#   BAG=/path/to/other_bag ./launch/play_bag.sh
#   LOOP=0 ./launch/play_bag.sh                # single pass instead of looping
#
# Like run_demo.sh, playback starts PAUSED and RViz runs on sim time (the bag's
# --clock), so the TF buffer resets on loop and the ego keeps moving.
cd "$(dirname "$0")/.."   # project root

BAG="${BAG:-$HOME/Downloads/rosbag2_2026_08_06-16_20_58}"
VIEW="${VIEW:-bev}"
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
echo "Bag playback only: ROS_DOMAIN_ID=$ROS_DOMAIN_ID | bag=$BAG | view=$VIEW"

pkill -x rviz2 2>/dev/null
pkill -f "[r]os2 bag play" 2>/dev/null
pkill -f "robot_state_publisher .*ego_vehicle.urdf" 2>/dev/null
sleep 2

# Clear stale FastDDS shared-memory segments left by prior (often killed) runs -
# they make DDS discovery crawl and stall the topic wait below.
ros2 daemon stop >/dev/null 2>&1
rm -f /dev/shm/fastrtps_* /dev/shm/sem.fastrtps_* /dev/shm/fastdds_* 2>/dev/null

PLAY_ARGS=(--clock 100 --start-paused)
[ "${LOOP:-1}" = "1" ] && PLAY_ARGS+=(--loop)
ros2 bag play "$BAG" "${PLAY_ARGS[@]}" &
BAG_PID=$!

# ego model: SIM time (the bag carries /tf but not /robot_description)
ros2 run robot_state_publisher robot_state_publisher src/rviz/ego_vehicle.urdf \
  --ros-args -p use_sim_time:=true &
RSP_PID=$!

trap 'kill $BAG_PID $RSP_PID $BEV_PID $PERSP_PID 2>/dev/null' EXIT

echo "Waiting for the bag player to advertise planning topics..."
for i in $(seq 1 90); do
  if ros2 topic info "$PATH_TOPIC" 2>/dev/null | grep -q "Publisher count: [1-9]"; then
    echo "Planning topics are live (after ${i}s). Starting RViz."
    break
  fi
  sleep 1
done
sleep 2

BEV_PID=""; PERSP_PID=""
if [ "$VIEW" = "bev" ] || [ "$VIEW" = "both" ]; then
  rviz2 -d src/rviz/narration_bev.rviz --ros-args -p use_sim_time:=true >/tmp/rviz_bev.log 2>&1 &
  BEV_PID=$!
fi
if [ "$VIEW" = "chase" ] || [ "$VIEW" = "both" ]; then
  rviz2 -d src/rviz/narration_chase.rviz --ros-args -p use_sim_time:=true >/tmp/rviz_chase.log 2>&1 &
  PERSP_PID=$!
fi

RESUME_SVC=$(ros2 service list 2>/dev/null | grep -m1 -E '/resume$')
read -r -p $'\n>>> RViz is up (paused). Press ENTER to start playback... '
if [ -n "$RESUME_SVC" ] && ros2 service call "$RESUME_SVC" rosbag2_interfaces/srv/Resume >/dev/null 2>&1; then
  echo "Playing. (Close RViz to stop.)"
else
  echo "WARN: could not resume via service; press SPACE in this terminal."
fi

wait $BEV_PID $PERSP_PID
