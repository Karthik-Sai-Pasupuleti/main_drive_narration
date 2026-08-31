#!/bin/bash
# Multi-agent narration: road-geometry (BEV) + obstacles (perspective) + narration.
# Run run_live.sh (BEV + perspective RViz) in another terminal first.
cd "$(dirname "$0")/../.."   # project root

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
export DISPLAY="${DISPLAY:-:1}"        # needed to grab the RViz frames
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"

# clean snap/VS Code env pollution that breaks ROS/Qt
unset GTK_PATH LOCPATH GIO_MODULE_DIR GTK_EXE_PREFIX GTK_IM_MODULE_FILE
unset GSETTINGS_SCHEMA_DIR GIO_LAUNCHED_DESKTOP_FILE GTK_IM_MODULE
unset LD_LIBRARY_PATH LD_PRELOAD

source /opt/ros/humble/setup.bash

# start ollama if it isn't reachable
if ! curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  echo "Starting 'ollama serve'..."
  ollama serve >/tmp/ollama_serve.log 2>&1 &
  for _ in $(seq 1 30); do curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1 && break; sleep 1; done
fi
export OLLAMA_HOST="$OLLAMA_URL"

pkill -f "[m]ain.py" 2>/dev/null
sleep 1

# prefer the speech_agent venv (has kokoro for the neural voice)
PY="../speech_agent/.venv/bin/python"
[ -x "$PY" ] || PY=python3
exec "$PY" main.py --config src/configs/multi_agent_pipeline.toml "$@"
