#!/bin/bash
# narration.sh - run the hazard-narration node (main.py).
#
# Subscribes to the mobile-pole infrastructure detections and the robot's own
# detector, narrates each hazard with the vision model configured in
# main.py (topics and model are constants at the top of that file), speaks
# it (Kokoro/espeak TTS) and logs it.
# No display/RViz needed - the node reads each source's camera topic directly,
# never a screenshot.
#
# Usage (from anywhere; the script cd's to the project root):
#   ./launch/action_pipeline/run_demo.sh          # terminal 1: bag playback
#   ./launch/action_pipeline/narration.sh         # terminal 2: this script
#
#   ./launch/action_pipeline/narration.sh --no-tts        # extra flags -> main.py
#   ROS_DOMAIN_ID=5 ./launch/action_pipeline/narration.sh # match run_demo.sh's domain
cd "$(dirname "$0")/../.."   # project root

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"   # same domain as run_demo.sh
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"

# --- clean environment (VS Code injects an LD_LIBRARY_PATH that can break
#     rclpy's compiled extensions) ---
unset LD_LIBRARY_PATH LD_PRELOAD

source /opt/ros/humble/setup.bash

# --- Ollama server: start it if it isn't reachable ---
if ! curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  echo "Ollama not reachable at $OLLAMA_URL - starting 'ollama serve'..."
  ollama serve >/tmp/ollama_serve.log 2>&1 &
  for _ in $(seq 1 30); do
    curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1 && break
    sleep 1
  done
fi
if ! curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  echo "ERROR: cannot reach Ollama at $OLLAMA_URL (see /tmp/ollama_serve.log)"
  exit 1
fi
# MODEL/PROVIDER are constants at the top of main.py. For PROVIDER="openai",
# export OPENAI_API_KEY before running.

pkill -f "[p]ython.*main.py" 2>/dev/null   # bracket avoids matching this pkill
sleep 1

echo "Narration: mobile_pole=/mobile_pole/axis_rgb_6_42/autoware_objects_3d | robot=/robot/detections"
export OLLAMA_HOST="$OLLAMA_URL"

# --- python: this project's own .venv has kokoro + langchain/ollama installed
#     (`uv sync --extra tts`); fall back to uv run / system python3 if absent. ---
PY=".venv/bin/python3"
if [ -x "$PY" ]; then
  echo "Using project .venv python (kokoro TTS if installed, else espeak-ng)."
elif command -v uv >/dev/null 2>&1; then
  echo "WARN: .venv not found; using 'uv run' instead."
  PY="uv run python"
else
  PY="python3"
fi
exec $PY main.py "$@"
