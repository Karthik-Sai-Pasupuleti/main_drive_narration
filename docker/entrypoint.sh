#!/bin/bash
# entrypoint.sh - runs on every container start, before anything else.
#
# Brings up the virtual display RViz renders on, serves it to a browser,
# makes sure the scripted cue WAVs exist, and waits for the model server.
# Then hands over to the container's CMD (sleep infinity), which keeps the
# container alive so you can `docker compose exec` into it.
set -e

source /opt/ros/humble/setup.bash

# Files written here land in the ./output bind mount as root:root 644. The host
# user can still read/play them, and can delete them because ./output itself is
# host-owned. NOTE: this umask only affects children of THIS script - a
# `docker compose exec` session bypasses the entrypoint and gets the default.
umask 000

# --- 1. the virtual display -------------------------------------------------
# RViz must genuinely render: vision.py screenshots its window for the VLM.
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
for _ in $(seq 1 40); do
  xdpyinfo -display :99 >/dev/null 2>&1 && break
  sleep 0.5
done
if ! xdpyinfo -display :99 >/dev/null 2>&1; then
  echo "FATAL: Xvfb :99 never came up" >&2
  exit 1
fi

# --- 2. window manager ------------------------------------------------------
# Without one, the RViz window has no proper geometry and xwininfo's bbox is
# unreliable - which silently degrades the captured frame.
fluxbox >/tmp/fluxbox.log 2>&1 &
# fluxbox shells out to fbsetbg for the desktop wallpaper; with no image-setter
# installed it pops an xmessage dialog that sits on the virtual screen. It does
# not overlap the RViz crop, but dismiss it so captures stay clean.
( sleep 4; pkill -f "xmessage" 2>/dev/null ) &

# --- 3. serve that screen to a web browser on :6080 -------------------------
x11vnc -display :99 -forever -shared -nopw -quiet >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &

mkdir -p /app/output/audio

# --- 4. scripted audio cues -------------------------------------------------
# cue_publisher.py and main.py both read cache/audio/manifest.json. cache/ is
# excluded from the image, so render it here on first start.
if [ ! -f /app/cache/audio/manifest.json ]; then
  echo "building the scripted cue WAVs (first run only)..."
  python3 /app/src/utils/prerender.py || \
    echo "WARN: prerender failed; scripted cues will be unavailable" >&2
fi

# --- 5. wait for the model server ------------------------------------------
# depends_on only orders startup, it does not wait for readiness.
echo "waiting for ollama at ${OLLAMA_URL} ..."
for _ in $(seq 1 150); do
  curl -sf "${OLLAMA_URL}/api/tags" >/dev/null 2>&1 && break
  sleep 2
done
if curl -sf "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
  echo "ollama is up."
else
  echo "WARN: ollama not reachable at ${OLLAMA_URL} after 5 min." >&2
  echo "      narration.sh will fail until it is. Check: docker compose logs ollama" >&2
fi

cat <<BANNER

  ================================================================
   Ready.

   Watch RViz:   http://localhost:6080/vnc.html
   WAV output:   ./output/audio/  (on the host)

   Terminal 1:
     docker compose exec narration bash -lc \\
       'printf "\\n" | ./launch/action_pipeline/run_demo.sh'

   Terminal 2:
     docker compose exec narration ./launch/action_pipeline/narration.sh
  ================================================================

BANNER

exec "$@"
