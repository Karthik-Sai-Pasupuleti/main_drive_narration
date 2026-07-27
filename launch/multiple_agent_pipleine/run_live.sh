#!/bin/bash
# RViz BEV + perspective views + HUD overlays for the multi-agent pipeline.
# Opens TWO RViz windows: narration_bev (BEV, feeds road-geometry) and
# narration_chase (perspective, feeds obstacles). Place them side by side so
# neither covers the other - each is captured by its window region.
cd "$(dirname "$0")/../.."   # project root

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
PATH_TOPIC="${PATH_TOPIC:-/planning/scenario_planning/lane_driving/behavior_planning/path}"
WAIT="${WAIT:-1}"

# clean snap/VS Code env pollution that crashes rviz2/Qt
unset GTK_PATH LOCPATH GIO_MODULE_DIR GTK_EXE_PREFIX GTK_IM_MODULE_FILE
unset GSETTINGS_SCHEMA_DIR GIO_LAUNCHED_DESKTOP_FILE GTK_IM_MODULE
unset LD_LIBRARY_PATH LD_PRELOAD
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_DATA_DIRS=/usr/share/ubuntu:/usr/share/gnome:/usr/local/share:/usr/share:/var/lib/snapd/desktop
export DISPLAY="${DISPLAY:-:1}"

source /opt/ros/humble/setup.bash

pkill -x rviz2 2>/dev/null
pkill -f "[f]actor_overlay.py" 2>/dev/null
pkill -f "[h]ud_relay.py" 2>/dev/null
sleep 1

python3 src/nodes/hud_relay.py --ros-args -p use_sim_time:=false &
RELAY_PID=$!
python3 src/nodes/factor_overlay.py --ros-args -p use_sim_time:=false &
OVERLAY_PID=$!

RSP_PID=""
if ! ros2 topic info /robot_description 2>/dev/null | grep -q "Publisher count: [1-9]"; then
  ros2 run robot_state_publisher robot_state_publisher src/rviz/ego_vehicle.urdf \
    --ros-args -p use_sim_time:=false &
  RSP_PID=$!
fi

# dummy drone/infra reports for testing (re-published, so the narrator catches
# them whenever it starts). Set DUMMY_EVENTS=0 to disable for a real run.
DUMMY_PID=""
if [ "${DUMMY_EVENTS:-1}" = "1" ]; then
  python3 src/nodes/dummy_events.py &
  DUMMY_PID=$!
fi

trap 'kill $RELAY_PID $OVERLAY_PID $RSP_PID $DUMMY_PID $BEV_PID $PERSP_PID 2>/dev/null' EXIT

if [ "$WAIT" = "1" ]; then
  echo "Waiting for $PATH_TOPIC ..."
  for _ in $(seq 1 90); do
    ros2 topic info "$PATH_TOPIC" 2>/dev/null | grep -q "Publisher count: [1-9]" && break
    sleep 1
  done
  sleep 1
fi

echo ">>> Starting RViz (BEV + perspective). Close both windows to stop."
rviz2 -d src/rviz/narration_bev.rviz --ros-args -p use_sim_time:=false >/tmp/rviz_bev.log 2>&1 &
BEV_PID=$!
rviz2 -d src/rviz/narration_chase.rviz --ros-args -p use_sim_time:=false >/tmp/rviz_chase.log 2>&1 &
PERSP_PID=$!

wait $BEV_PID $PERSP_PID
