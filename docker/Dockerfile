# main_drive_narration - self-contained drive-narration demo.
#
# Contains ROS 2 Humble, RViz, the Autoware message packages the planner bag
# needs, a virtual X display (RViz has to actually render - vision.py
# screenshots its window), and Kokoro TTS with its weights baked in.
#
# The rosbag is NOT in here. It is bind-mounted from the host at run time;
# see docker-compose.yml.
#
#   docker compose build
#
# ---------------------------------------------------------------- piece A ---
# Start from an image that already has ROS 2 Humble + RViz. The "-desktop"
# variant is the one that includes rviz2.
FROM osrf/ros:humble-desktop

ENV DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------- piece B ---
# Message definitions the bag needs to replay, plus the RViz plugins that draw
# the HUD. rosbag2-storage-mcap is REQUIRED - the bag is a .mcap file and the
# failure without it does not mention mcap.
# NOT INSTALLED: ros-humble-autoware-perception-rviz-plugin
#   The binary was dropped from the ROS Humble apt repo (only the source
#   package remains, and it no longer configures against this image's
#   tinyxml2_vendor). Consequence: the 6 DetectedObjects / TrackedObjects /
#   PredictedObjects displays in src/rviz/*.rviz show as errors and the 3D
#   object boxes are not drawn. Everything the narration depends on - the HUD
#   text, the planned path, the ego model, the map - is unaffected.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ros-humble-rosbag2-storage-mcap \
      ros-humble-robot-state-publisher \
      ros-humble-rviz-2d-overlay-msgs \
      ros-humble-rviz-2d-overlay-plugins \
      ros-humble-autoware-adapi-v1-msgs \
      ros-humble-autoware-common-msgs \
      ros-humble-autoware-internal-msgs \
      ros-humble-autoware-internal-debug-msgs \
      ros-humble-autoware-internal-planning-msgs \
      ros-humble-autoware-internal-perception-msgs \
      ros-humble-autoware-internal-metric-msgs \
      ros-humble-autoware-perception-msgs \
      ros-humble-autoware-planning-msgs \
      ros-humble-autoware-vehicle-msgs \
      ros-humble-autoware-map-msgs \
      ros-humble-autoware-lanelet2-extension \
      ros-humble-autoware-vehicle-info-utils \
      ros-humble-autoware-overlay-rviz-plugin \
      ros-humble-autoware-planning-rviz-plugin \
      ros-humble-autoware-localization-rviz-plugin \
      ros-humble-autoware-mission-details-overlay-rviz-plugin \
      ros-humble-autoware-topic-text-overlay-rviz-plugin \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------- piece C ---
# The virtual display and the tooling around it.
#   xvfb        - the screen that exists only in memory
#   fluxbox     - window manager, so the RViz window has a real geometry
#   x11vnc      - serves that screen over VNC
#   novnc       - serves VNC to a web browser on :6080
#   x11-utils   - provides xwininfo, which vision.py uses to find the RViz
#                 window. Without it capture silently grabs the whole screen.
#   alsa-utils  - provides aplay, called by tts.py
#   espeak-ng   - Kokoro needs it for out-of-dictionary words
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc x11-utils fluxbox novnc websockify \
      alsa-utils espeak-ng libsndfile1 \
      curl python3-pip \
    && rm -rf /var/lib/apt/lists/*

# A container has no sound card. Give ALSA a null sink so aplay exits 0
# instead of erroring on every line - we save WAVs, we don't need playback.
RUN printf 'pcm.!default { type null }\nctl.!default { type null }\n' \
      > /etc/asound.conf

# ---------------------------------------------------------------- piece D ---
ENV HF_HOME=/opt/hf

# Ubuntu 22.04 ships setuptools 59.6, which cannot generate metadata for
# legacy sdists (docopt, pulled in transitively by misaki) once a modern
# `packaging` is present:
#   TypeError: canonicalize_version() got an unexpected keyword 'strip_trailing_zero'
RUN pip3 install --no-cache-dir --upgrade pip setuptools wheel

# numpy and pillow MUST be given explicit floors. Bare `numpy`/`pillow` are
# satisfied by the base image's system versions (1.21.5 / 9.0.1), and Pillow
# 9.0.1 lacks PIL.Image.Resampling, which the transformers version kokoro
# pulls in requires:
#   AttributeError: module 'PIL.Image' has no attribute 'Resampling'
# Verified after the upgrade: ImageGrab still grabs under Xvfb, and rclpy plus
# std_msgs still import against numpy 2.x.

# CPU-only torch FIRST. Installing kokoro on its own pulls a CUDA build
# (~3 GB of GPU libraries this container never uses).
# --ignore-installed: torch wants to upgrade sympy, but the Debian-packaged
# sympy 1.9 is distutils-installed and modern pip refuses to uninstall it
#   error: uninstall-distutils-installed-package
RUN pip3 install --no-cache-dir --ignore-installed \
      --index-url https://download.pytorch.org/whl/cpu \
      torch

# Pinned: 0.9.4 is the version verified working against this codebase.
RUN pip3 install --no-cache-dir \
      kokoro==0.9.4 \
      soundfile \
      langchain-core \
      langchain-ollama \
      langchain-openai \
      mlflow \
      ollama \
      "numpy>=1.26" \
      "pillow>=10.4"

# Bake the 313 MB Kokoro model + the af_heart voice pack into the image, so
# the container needs no network at run time. Passing voice= is what pulls the
# voice file; without it the model is cached but the voice is not.
#
# The assert matters: a broken Kokoro otherwise exits 0, the image builds
# clean, and at run time make_tts() quietly falls back to the robotic espeak
# voice with the reason buried in a log line.
RUN python3 -c "\
from kokoro import KPipeline; \
p = KPipeline(lang_code='a', device='cpu', repo_id='hexgrad/Kokoro-82M'); \
out = list(p('warmup', voice='af_heart')); \
assert out and len(out[0].audio) > 1000, 'kokoro produced no audio'; \
print('kokoro OK:', len(out[0].audio), 'samples')" \
    && du -sh "$HF_HOME"

# ---------------------------------------------------------------- piece E ---
WORKDIR /app
COPY . /app
RUN chmod +x docker/entrypoint.sh launch/*.sh launch/*/*.sh

# These are what let the existing launch scripts run unmodified: they already
# read DISPLAY, BAG, OLLAMA_URL and OLLAMA_HOST from the environment.
#
# NOTE: uv is deliberately NOT installed. narration.sh prefers "uv run python"
# when it finds uv, which would build a venv with no rclpy. Without uv the
# script falls through to system python3 - the one that has ROS 2.
ENV DISPLAY=:99 \
    NARRATION_WAV_DIR=/app/output/audio \
    ROS_DOMAIN_ID=0 \
    OLLAMA_URL=http://ollama:11434 \
    OLLAMA_HOST=http://ollama:11434 \
    PYTHONUNBUFFERED=1

EXPOSE 6080 5900

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["sleep", "infinity"]
