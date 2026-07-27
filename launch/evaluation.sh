#!/bin/bash
# evaluation.sh - per-model evaluation of the MULTI-AGENT pipeline.
#
# For each model in the list, one at a time:
#   1. bring up run_demo.sh (bag + BEV & perspective RViz), paused;
#   2. wait for both RViz windows, then settle ~10s;
#   3. press ENTER (resume the drive) via a FIFO on run_demo's stdin;
#   4. run narration.sh (main.py) on this model until EVENTS events or RUN_TIMEOUT,
#      logging each event's 3 bot outputs + input images to MLflow
#      (experiment 'multiagent', run 'multiagent:<model>:<event>') and to a CSV;
#   5. tear the stack down, next model.
#
# NOTE: dummy drone/infra events are OFF in run_demo.sh, so narration triggers on
# INTERSECTION TURNS from the drive. A model may collect fewer than EVENTS if the
# drive shows fewer turns within RUN_TIMEOUT (whatever it got is still logged).
#
# Over SSH, wrap in tmux or `nohup ... &` so a disconnect can't orphan a stack.
#
#   ./launch/evaluation.sh
#   MODELS="gemma4:12b gpt-4o-mini" EVENTS=3 ./launch/evaluation.sh
#   SPEECH=1 SETTLE=15 ./launch/evaluation.sh
set -u
cd "$(dirname "$0")/.."   # project root
ROOTDIR="$(pwd)"

MODELS="${MODELS:-minicpm-v4.6:latest minicpm-v4.5:latest qwen3.5:4b gemma4:31b qwen3.6:latest gemma4:12b gemma4:latest gpt-4o-mini}"
EVENTS="${EVENTS:-3}"                   # events (turns) to collect per model
RUN_TIMEOUT="${RUN_TIMEOUT:-240}"       # max seconds to collect events per model
READY_TIMEOUT="${READY_TIMEOUT:-120}"   # max seconds to wait for both RViz windows
SETTLE="${SETTLE:-10}"                   # seconds to let RViz/TF settle before ENTER
SPEECH="${SPEECH:-0}"                    # 1 -> speak (and time speech); 0 -> silent
EXPERIMENT="${EXPERIMENT:-multiagent}"
CSV="${CSV:-$ROOTDIR/eval_results/multiagent.csv}"

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
export DISPLAY="${DISPLAY:-:1}"

RUN_DEMO="launch/multiple_agent_pipleine/run_demo.sh"
NARRATE="launch/multiple_agent_pipleine/narration.sh"
DEMO_PID=""
FIFO=""

mkdir -p "$(dirname "$CSV")"
# start a clean CSV; back up any previous one so runs don't concatenate
[ -f "$CSV" ] && mv "$CSV" "$CSV.$(date +%Y%m%d_%H%M%S).bak"

provider_for() { case "$1" in gpt-*|o1-*|o3-*) echo openai ;; *) echo ollama ;; esac; }

teardown() {
  # kill the whole run_demo process group, then sweep any stragglers by name
  [ -n "$DEMO_PID" ] && kill -TERM -"$DEMO_PID" 2>/dev/null
  pkill -x rviz2 2>/dev/null
  pkill -f "[r]os2 bag play" 2>/dev/null
  pkill -f "robot_state_publisher .*ego_vehicle.urdf" 2>/dev/null
  pkill -f "[f]actor_overlay.py" 2>/dev/null
  pkill -f "[h]ud_relay.py" 2>/dev/null
  pkill -f "[d]ummy_events.py" 2>/dev/null
  pkill -f "[m]ain.py" 2>/dev/null
  [ -n "$FIFO" ] && { exec 3>&- 2>/dev/null; rm -f "$FIFO"; FIFO=""; }
  DEMO_PID=""
  sleep 2
}

_CLEANED=0
cleanup() { [ "$_CLEANED" = 1 ] && return; _CLEANED=1; teardown; }
trap 'echo; echo ">>> interrupted - cleaning up"; cleanup; exit 130' INT TERM HUP
trap cleanup EXIT

wait_for_rviz() {
  # both windows must exist so BEV + perspective frames can be grabbed
  local i
  for i in $(seq 1 "$READY_TIMEOUT"); do
    if xwininfo -root -tree 2>/dev/null | grep -qi "narration_bev" \
       && xwininfo -root -tree 2>/dev/null | grep -qi "narration_chase"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

[ "$SPEECH" = "1" ] && SPEECH_FLAG="" || SPEECH_FLAG="--no-tts"

echo "Multi-agent evaluation (per model, live)"
echo "  models      : $MODELS"
echo "  events/model: $EVENTS   run_timeout: ${RUN_TIMEOUT}s   settle: ${SETTLE}s"
echo "  csv         : $CSV"
echo "  mlflow      : experiment '$EXPERIMENT'"

i=0
for MODEL in $MODELS; do
  i=$((i + 1))
  PROV="$(provider_for "$MODEL")"
  echo
  echo "============================================================"
  echo "[$i] MODEL: $MODEL   (provider: $PROV)"
  echo "============================================================"
  teardown   # ensure nothing from a previous model is left running

  # 1) bring up run_demo with a FIFO on stdin so WE control its ENTER gate
  SAFE=$(echo "$MODEL" | tr -c 'A-Za-z0-9' '_')
  LOG="/tmp/eval_run_demo_${SAFE}.log"
  FIFO="$(mktemp -u)"; mkfifo "$FIFO"
  echo ">>> bringing up run_demo.sh (log: $LOG)"
  setsid bash "$RUN_DEMO" <"$FIFO" >"$LOG" 2>&1 &
  DEMO_PID=$!
  exec 3>"$FIFO"   # open the write end (rendezvous unblocks run_demo's stdin)

  # 2) wait for both RViz windows, then let the drive/TF settle
  echo ">>> waiting for both RViz windows (up to ${READY_TIMEOUT}s)..."
  if wait_for_rviz; then
    echo ">>> RViz up; settling ${SETTLE}s..."
    sleep "$SETTLE"
  else
    echo ">>> WARN: both RViz windows not up; SKIPPING $MODEL"
    teardown
    continue
  fi

  # 3) press ENTER -> run_demo resumes the paused bag (the drive starts)
  echo ">>> ENTER -> starting the drive"
  echo >&3

  # 4) run the narrator on this model; log each event to MLflow + CSV
  echo ">>> narrating: up to $EVENTS events or ${RUN_TIMEOUT}s"
  bash "$NARRATE" --model "$MODEL" --provider "$PROV" --eval \
      --events "$EVENTS" --timeout "$RUN_TIMEOUT" \
      --experiment "$EXPERIMENT" --csv "$CSV" $SPEECH_FLAG \
      || echo ">>> WARN: narration exited nonzero for $MODEL"

  # 5) tear the stack down before the next model
  echo ">>> tearing down stack for $MODEL"
  teardown
done

echo
echo "ALL DONE."
echo "  consolidated outputs -> $CSV"
echo "  compare in MLflow     -> mlflow ui   (experiment '$EXPERIMENT')"
