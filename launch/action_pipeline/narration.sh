#!/bin/bash
# narration.sh - run the drive-narration node (main.py).
#
# Subscribes to /hud/decision (for turns) plus the mobile-pole infrastructure
# hazard detections topic, reasons each event with the Ollama model configured
# in src/configs/action_pipeline.toml, speaks it (Kokoro/espeak TTS) and logs
# it. Run run_live.sh (HUD overlays) in another terminal first. No display/RViz
# needed - turn narration is text-only, and mobile-pole's vlm method (if
# enabled) reads the pole's own camera topic directly, not a screenshot.
#
# Usage (from anywhere; the script cd's to the project root):
#   ./launch/action_pipeline/run_live.sh          # terminal 1: HUD overlays
#   ./launch/action_pipeline/narration.sh         # terminal 2: this script
#
#   ./launch/action_pipeline/narration.sh --no-tts        # extra flags -> main.py
#   ROS_DOMAIN_ID=5 ./launch/action_pipeline/narration.sh # match run_live.sh's domain
cd "$(dirname "$0")/../.."   # project root

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"   # same domain as run_live.sh
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
# Models + provider (ollama/openai) are set per agent in the pipeline config;
# main.py surfaces a clear error if a model is missing. For openai agents,
# export OPENAI_API_KEY before running.

pkill -f "[p]ython.*main.py" 2>/dev/null   # bracket avoids matching this pkill
sleep 1

echo "Narration: config=src/configs/action_pipeline.toml | hud=/hud/decision | mobile_pole=/mobile_pole/axis_rgb_6_42/autoware_objects_3d"
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
exec $PY main.py --config src/configs/action_pipeline.toml "$@"
