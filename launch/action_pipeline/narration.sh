#!/bin/bash
# narration.sh - run the drive-narration node (main.py).
#
# Subscribes to the RViz HUD (/hud/decision) plus the drone/infrastructure report
# topics, reasons each event with the Ollama model configured in
# src/configs/actions_promt.toml, speaks it (Kokoro/espeak TTS) and logs it.
# Run run_live.sh (RViz + HUD overlays) in another terminal first.
#
# Usage (from anywhere; the script cd's to the project root):
#   ./launch/run_live.sh          # terminal 1: RViz + HUD overlays
#   ./launch/narration.sh         # terminal 2: this script
#
#   ./launch/narration.sh --no-tts                 # extra flags -> main.py
#   ROS_DOMAIN_ID=5 ./launch/narration.sh          # match run_live.sh's domain
cd "$(dirname "$0")/../.."   # project root

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"   # same domain as run_live.sh
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
# The narrator grabs the RViz frame for the VLM, so it needs the same X display
# RViz renders on (run_live.sh / run_demo.sh use :1).
export DISPLAY="${DISPLAY:-:1}"

# --- clean environment (snap/VS Code env pollution breaks ROS/Qt) ---
unset GTK_PATH LOCPATH GIO_MODULE_DIR GTK_EXE_PREFIX GTK_IM_MODULE_FILE
unset GSETTINGS_SCHEMA_DIR GIO_LAUNCHED_DESKTOP_FILE GTK_IM_MODULE
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

echo "Narration: config=src/configs/action_pipeline.toml | hud=/hud/decision | drone=/drone/reports | infra=/infrastructure/reports"
export OLLAMA_HOST="$OLLAMA_URL"

# --- python: prefer the demo_speech_agent venv (has kokoro for the neural voice,
#     + langchain/ollama); this project's own env has no kokoro, so uv run would
#     fall back to the robotic espeak-ng voice. ---
PY="../demo_speech_agent/.venv/bin/python"
if [ -x "$PY" ]; then
  echo "Using demo_speech_agent venv python (kokoro TTS)."
elif command -v uv >/dev/null 2>&1; then
  echo "WARN: demo_speech_agent venv not found; using 'uv run' (no kokoro -> espeak voice)."
  PY="uv run python"
else
  PY="python3"
fi
exec $PY main.py --config src/configs/action_pipeline.toml "$@"
