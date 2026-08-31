#!/bin/bash
# Save BEV + front-view RViz frames on each intersection-turn trigger, for model
# evaluation. Run run_demo.sh (BEV + chase RViz + HUD) in another terminal first,
# then run this. Writes JPEG pairs to eval/turn_captures/ (override with --out).
#
#   ./launch/multiple_agent_pipleine/capture_turns.sh                 # terminal 3
#   ./launch/multiple_agent_pipleine/capture_turns.sh --events 8
#   ./launch/multiple_agent_pipleine/capture_turns.sh --out eval/run1
#
# It only grabs the screen + reads the HUD (no VLM, no TTS), so it is light and
# can run alongside narration.sh (ROS will warn about a duplicate node name -
# harmless). Arrange the two RViz windows side by side so neither covers the
# other - each frame is captured by its window region.
cd "$(dirname "$0")/../.."   # project root

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
export DISPLAY="${DISPLAY:-:1}"        # needed to grab the RViz frames

# clean snap/VS Code env pollution that breaks ROS/Qt
unset GTK_PATH LOCPATH GIO_MODULE_DIR GTK_EXE_PREFIX GTK_IM_MODULE_FILE
unset GSETTINGS_SCHEMA_DIR GIO_LAUNCHED_DESKTOP_FILE GTK_IM_MODULE
unset LD_LIBRARY_PATH LD_PRELOAD
export XDG_DATA_HOME="$HOME/.local/share"

source /opt/ros/humble/setup.bash

pkill -f "[e]val_turn_capture.py" 2>/dev/null
sleep 1

# prefer the speech_agent venv (matches narration.sh); fall back to project/py3
PY="../speech_agent/.venv/bin/python"
[ -x "$PY" ] || PY=".venv/bin/python"
[ -x "$PY" ] || PY=python3
exec "$PY" eval_turn_capture.py "$@"
