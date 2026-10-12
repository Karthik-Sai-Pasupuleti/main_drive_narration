#!/bin/bash
# view_rviz.sh - optional 3rd-person "chase cam" visualization for the rosbag.
#
# main.py itself needs no display (see narration.sh) - this is purely for a
# human to watch the drive while the bag plays. Opens RViz2 with a
# ThirdPersonFollower camera locked to base_link, plus the ego vehicle model.
#
# The chase camera needs /tf (map->base_link) and /robot_description. Some
# bags carry both - the POLE-INFRA bag does - and for those we must NOT start
# our own robot_state_publisher, or /robot_description gets two publishers and
# RViz shows whichever arrived last. Others (the planner bag) carry only /tf,
# and need rviz/ego_vehicle.urdf published for the car to appear at all.
# EGO_MODEL=auto detects which case this is; force it with on/off.
#
# Usage (run in its own terminal, alongside run_demo.sh/narration.sh):
#   ./launch/action_pipeline/view_rviz.sh
#   EGO_MODEL=on ./launch/action_pipeline/view_rviz.sh    # bag has no model
cd "$(dirname "$0")/../.."   # project root

RVIZ_CONFIG="${RVIZ_CONFIG:-rviz/narration_chase.rviz}"
EGO_MODEL="${EGO_MODEL:-auto}"   # auto | on | off

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"   # match run_demo.sh/narration.sh
export DISPLAY="${DISPLAY:-:1}"

# --- clean environment: VS Code's snap sandbox injects LD_LIBRARY_PATH plus a
#     pile of GTK/glib/locale vars pointing into /snap/code/.../usr/lib, which
#     make rviz2 (Qt) load a mismatched libpthread and die with a symbol
#     lookup error ("undefined symbol: __libc_pthread_init"). Strip them. ---
unset LD_LIBRARY_PATH LD_PRELOAD GTK_PATH GTK_EXE_PREFIX GDK_PIXBUF_MODULEDIR \
      GDK_PIXBUF_MODULE_FILE GIO_MODULE_DIR GIO_LAUNCHED_DESKTOP_FILE LOCPATH \
      GTK_IM_MODULE_FILE GSETTINGS_SCHEMA_DIR XDG_DATA_DIRS
for v in $(compgen -e | grep -i '^SNAP'); do unset "$v"; done

source /opt/ros/humble/setup.bash

# --- ego vehicle model: only publish one if nothing else already does ---
pkill -f "robot_state_publisher .*ego_vehicle.urdf" 2>/dev/null
if [ "$EGO_MODEL" = "auto" ]; then
  if ros2 topic info /robot_description 2>/dev/null | grep -q "Publisher count: [1-9]"; then
    EGO_MODEL=off
    echo "ego model: /robot_description is already published (bag or live stack)"
  else
    EGO_MODEL=on
  fi
fi

RSP_PID=""
if [ "$EGO_MODEL" = "on" ]; then
  ros2 run robot_state_publisher robot_state_publisher rviz/ego_vehicle.urdf \
    --ros-args -p use_sim_time:=true >/tmp/ego_model.log 2>&1 &
  RSP_PID=$!
  trap 'kill $RSP_PID 2>/dev/null' EXIT
  echo "ego model: publishing rviz/ego_vehicle.urdf (pid $RSP_PID)"
fi

echo "Chase view: $RVIZ_CONFIG"
rviz2 -d "$RVIZ_CONFIG" --ros-args -p use_sim_time:=true
