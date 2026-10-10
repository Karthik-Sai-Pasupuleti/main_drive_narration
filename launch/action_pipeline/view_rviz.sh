#!/bin/bash
# view_rviz.sh - optional 3rd-person "chase cam" visualization for the rosbag.
#
# main.py itself needs no display (see narration.sh) - this is purely for a
# human to watch the drive while run_demo.sh plays the bag. Opens RViz2 with
# a third-person follower view behind the ego vehicle (base_link), showing
# the robot model, lidar/objects and the planned path.
#
# Usage (run in its own terminal, alongside run_demo.sh/narration.sh):
#   ./launch/action_pipeline/view_rviz.sh
cd "$(dirname "$0")/../.."   # project root

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

exec rviz2 -d rviz/front_chase_view.rviz
