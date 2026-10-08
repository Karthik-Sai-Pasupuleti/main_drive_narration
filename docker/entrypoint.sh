#!/bin/bash
# entrypoint.sh - runs on every container start, before anything else.
#
# Sources ROS 2, waits for the model server to be reachable, then hands over
# to the container's CMD (sleep infinity), which keeps the container alive so
# you can `docker compose exec` into it.
set -e

source /opt/ros/humble/setup.bash

# Files written under ./output or ./cache land in the bind mount as root:root
# 644. The host user can still read/play/delete them because the mounted
# directory itself is host-owned.
umask 000

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

   Terminal 1 (bag playback):
     docker compose exec narration bash -lc \\
       'printf "\\n" | ./launch/action_pipeline/run_demo.sh'

   Terminal 2 (narration engine):
     docker compose exec narration ./launch/action_pipeline/narration.sh
  ================================================================

BANNER

exec "$@"
