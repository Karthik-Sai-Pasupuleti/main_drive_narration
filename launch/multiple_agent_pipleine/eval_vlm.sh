#!/bin/bash
# Offline VLM sweep of the multi-agent pipeline over the saved turn frames,
# logged to MLflow. No ROS / RViz needed - it reads eval/turn_captures/*.jpg and
# calls Ollama directly. Ensures Ollama is up, then runs eval_vlm_models.py.
#
#   ./launch/multiple_agent_pipleine/eval_vlm.sh                    # all vision models
#   ./launch/multiple_agent_pipleine/eval_vlm.sh --models qwen2.5vl:3b,gemma4:12b
#   ./launch/multiple_agent_pipleine/eval_vlm.sh --images-dir eval/run1
#
# View results:  mlflow ui --backend-store-uri sqlite:///mlflow.db
cd "$(dirname "$0")/../.."   # project root

OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
export OLLAMA_HOST="$OLLAMA_URL" OLLAMA_URL

# clean snap/VS Code env pollution that can break things
unset GTK_PATH LOCPATH GIO_MODULE_DIR GTK_EXE_PREFIX GTK_IM_MODULE_FILE
unset GSETTINGS_SCHEMA_DIR GIO_LAUNCHED_DESKTOP_FILE GTK_IM_MODULE
unset LD_LIBRARY_PATH LD_PRELOAD

# start ollama if it isn't reachable
if ! curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  echo "Starting 'ollama serve'..."
  ollama serve >/tmp/ollama_serve.log 2>&1 &
  for _ in $(seq 1 30); do curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1 && break; sleep 1; done
fi

# needs mlflow + langchain_ollama: prefer the speech_agent venv (project .venv lacks mlflow)
PY="../speech_agent/.venv/bin/python"
[ -x "$PY" ] || PY=".venv/bin/python"
[ -x "$PY" ] || PY=python3
exec "$PY" eval_vlm_models.py "$@"
