#!/bin/bash
# play.sh - bag + RViz only. No HUD overlays, no narration pipeline.
#
# Supersedes play_bag.sh. Three things it handles that play_bag.sh does not:
#   1. Isolates DDS to this host, so another machine replaying a bag on the
#      same ROS_DOMAIN_ID cannot inject its /clock into our session.
#   2. Skips --clock when the bag already recorded a /clock topic (the
#      2026_08_20 bag does). Publishing both makes sim time jump backwards
#      ~70x/sec, which clears the TF buffer and nothing ever renders.
#   3. Waits on a topic the bag actually contains, instead of a hardcoded
#      planning path that neither bag records.
#
#   ./launch/play.sh                        # BEV view, default bag
#   VIEW=chase ./launch/play.sh             # perspective/chase view
#   VIEW=both  ./launch/play.sh             # both windows, side by side
#   BAG=/path/to/other_bag ./launch/play.sh
#   LOOP=0  ./launch/play.sh                # single pass instead of looping
#   PAUSE=0 ./launch/play.sh                # start playing immediately
#   RATE=0.5 ./launch/play.sh               # half speed
cd "$(dirname "$0")/.."   # project root

BAG="${BAG:-$HOME/Downloads/rosbag2_2026_08_06-16_20_58}"
VIEW="${VIEW:-bev}"
LOOP="${LOOP:-1}"
PAUSE="${PAUSE:-1}"
RATE="${RATE:-1.0}"
PATH_TOPIC="/planning/scenario_planning/lane_driving/behavior_planning/path"
FALLBACK_TOPIC="/tf"

# Isolate from the LAN. Domain 0 is everyone's default; a bag player on another
# machine will otherwise be discovered and fight us for /clock.
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}"
export ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-1}"

# clean snap/VS Code env pollution that crashes rviz2/Qt
unset GTK_PATH LOCPATH GIO_MODULE_DIR GTK_EXE_PREFIX GTK_IM_MODULE_FILE
unset GSETTINGS_SCHEMA_DIR GIO_LAUNCHED_DESKTOP_FILE GTK_IM_MODULE
unset LD_LIBRARY_PATH LD_PRELOAD
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_DATA_DIRS=/usr/share/ubuntu:/usr/share/gnome:/usr/local/share:/usr/share:/var/lib/snapd/desktop
export DISPLAY="${DISPLAY:-:1}"

source /opt/ros/humble/setup.bash

META="$BAG/metadata.yaml"
if [ ! -e "$META" ]; then
  echo "ERROR: no bag at '$BAG' (expected a directory with metadata.yaml, not the .mcap)."
  exit 1
fi

# --- decide whether to synthesize a clock -----------------------------------
# A bag that recorded /clock already carries sim time. Adding --clock publishes
# a second, unrelated time source from the same process.
CLOCK_ARGS=(--clock 100)
if grep -q "name: /clock$" "$META"; then
  CLOCK_ARGS=()
  echo "NOTE: bag records /clock; omitting --clock so sim time has one source."
fi

# --- decide what to wait on -------------------------------------------------
WAIT_TOPIC="$PATH_TOPIC"
if ! grep -q "name: ${PATH_TOPIC}$" "$META"; then
  WAIT_TOPIC="$FALLBACK_TOPIC"
  echo "NOTE: bag does not record '$PATH_TOPIC'; waiting on '$WAIT_TOPIC' instead."
fi

echo "Bag playback only: domain=$ROS_DOMAIN_ID localhost_only=$ROS_LOCALHOST_ONLY"
echo "  bag=$BAG"
echo "  view=$VIEW loop=$LOOP rate=$RATE paused_start=$PAUSE"

# --- tear down anything stale ----------------------------------------------
pkill -x rviz2 2>/dev/null
pkill -f "[r]os2 bag play" 2>/dev/null
pkill -f "robot_state_publisher .*ego_vehicle.urdf" 2>/dev/null
sleep 2

# Stale FastDDS shared-memory segments from killed players keep advertising
# dead publishers, which shows up as phantom extra /clock sources.
ros2 daemon stop >/dev/null 2>&1
rm -f /dev/shm/fastrtps_* /dev/shm/sem.fastrtps_* /dev/shm/fastdds_* 2>/dev/null

# --- launch -----------------------------------------------------------------
PLAY_ARGS=("${CLOCK_ARGS[@]}" --rate "$RATE")
[ "$LOOP" = "1" ]  && PLAY_ARGS+=(--loop)
[ "$PAUSE" = "1" ] && PLAY_ARGS+=(--start-paused)

ros2 bag play "$BAG" "${PLAY_ARGS[@]}" &
BAG_PID=$!

# ego model: SIM time (the bag carries /tf but not /robot_description)
ros2 run robot_state_publisher robot_state_publisher src/rviz/ego_vehicle.urdf \
  --ros-args -p use_sim_time:=true &
RSP_PID=$!

BEV_PID=""; PERSP_PID=""
trap 'kill $BAG_PID $RSP_PID $BEV_PID $PERSP_PID 2>/dev/null' EXIT

echo "Waiting for the bag player to advertise '$WAIT_TOPIC'..."
READY=0
for i in $(seq 1 90); do
  if ros2 topic info "$WAIT_TOPIC" 2>/dev/null | grep -q "Publisher count: [1-9]"; then
    echo "Topics are live (after ${i}s). Starting RViz."
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" = "0" ]; then
  echo "WARN: '$WAIT_TOPIC' never appeared after 90s - starting RViz anyway."
fi
sleep 2

if [ "$VIEW" = "bev" ] || [ "$VIEW" = "both" ]; then
  rviz2 -d src/rviz/narration_bev.rviz --ros-args -p use_sim_time:=true >/tmp/rviz_bev.log 2>&1 &
  BEV_PID=$!
fi
if [ "$VIEW" = "chase" ] || [ "$VIEW" = "both" ]; then
  rviz2 -d src/rviz/narration_chase.rviz --ros-args -p use_sim_time:=true >/tmp/rviz_chase.log 2>&1 &
  PERSP_PID=$!
fi

# --- sanity check: exactly one clock source ---------------------------------
sleep 3
NCLK=$(ros2 topic info /clock 2>/dev/null | grep -oP 'Publisher count: \K[0-9]+')
if [ -n "$NCLK" ] && [ "$NCLK" -gt 1 ]; then
  echo "WARN: /clock has $NCLK publishers (expected 1). Expect 'jump back in time'"
  echo "      spam and a frozen view. Another player is alive on this domain."
fi

if [ "$PAUSE" = "1" ]; then
  RESUME_SVC=$(ros2 service list 2>/dev/null | grep -m1 -E '/resume$')
  read -r -p $'\n>>> RViz is up (paused). Press ENTER to start playback... '
  if [ -n "$RESUME_SVC" ] && ros2 service call "$RESUME_SVC" rosbag2_interfaces/srv/Resume >/dev/null 2>&1; then
    echo "Playing. (Close RViz to stop.)"
  else
    echo "WARN: could not resume via service; press SPACE in this terminal."
  fi
fi

wait $BEV_PID $PERSP_PID
