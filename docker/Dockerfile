# main_drive_narration - self-contained narration container.
#
# This pipeline has no RViz and no screen capture: main.py subscribes directly
# to the planning/mobile-pole ROS topics (including the mobile pole's own
# camera image topic for vision), so the image only needs ROS 2 Humble's base
# install (no "-desktop"/RViz/Qt), the two Autoware message packages those
# topics use, rosbag2's mcap storage plugin to play the recording, and Kokoro
# TTS with its weights baked in.
#
# The rosbag is NOT in here. It is bind-mounted from the host at run time;
# see docker-compose.yml.
#
#   docker compose build
#
# ---------------------------------------------------------------- piece A ---
FROM ros:humble-ros-base

ENV DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------- piece B ---
# rosbag2-storage-mcap is REQUIRED - the bag is a .mcap file and the failure
# without it does not mention mcap. The two autoware_* packages are what
# main.py actually imports (SteeringFactorArray, DetectedObjects); apt pulls
# their message-package dependencies (autoware_common_msgs etc.) transitively.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ros-humble-rosbag2-storage-mcap \
      ros-humble-autoware-adapi-v1-msgs \
      ros-humble-autoware-perception-msgs \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------- piece C ---
#   alsa-utils  - provides aplay, called by tts.py
#   espeak-ng   - Kokoro needs it for out-of-dictionary words, and it is also
#                 tts.py's fallback voice when Kokoro itself is unavailable
#   libsndfile1 - misaki/kokoro's native audio dependency
#   curl        - entrypoint.sh's Ollama readiness check
#   python3-pip - this image installs deps with pip, not uv (see piece D)
RUN apt-get update && apt-get install -y --no-install-recommends \
      alsa-utils espeak-ng libsndfile1 curl ca-certificates python3-pip \
    && rm -rf /var/lib/apt/lists/*

# A container has no sound card. Give ALSA a null sink so aplay exits 0
# instead of erroring on every line.
RUN printf 'pcm.!default { type null }\nctl.!default { type null }\n' \
      > /etc/asound.conf

# ---------------------------------------------------------------- piece D ---
# uv is deliberately NOT used here: this project's uv.lock is a cross-platform
# lock (it also resolves macOS wheels for local dev) and pins torch straight
# from PyPI, which on Linux means the CUDA build - several GB of GPU libraries
# this CPU-only container never uses. Plain pip, installed in the order below,
# sidesteps that: narration.sh already falls back to system python3 when it
# finds no project .venv, so there is nothing else to wire up.
#
# Ubuntu 22.04 ships setuptools 59.6, which cannot generate metadata for
# legacy sdists (docopt, pulled in transitively by misaki) once a modern
# `packaging` is present:
#   TypeError: canonicalize_version() got an unexpected keyword 'strip_trailing_zero'
RUN pip3 install --no-cache-dir --upgrade pip setuptools wheel

# CPU-only torch FIRST. Installing kokoro on its own resolves a CUDA build
# from PyPI's default index.
RUN pip3 install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      torch

# The rest of pyproject.toml's [project.dependencies] plus the `tts` extra.
# mcap-ros2-support (the `eval` extra) is intentionally left out - it is for
# reading .mcap files offline (eval_hazard_pipeline.py); here ROS plays the
# bag directly. tomli is required on this image's python3.10 (the project
# only needs it for python < 3.11).
RUN pip3 install --no-cache-dir \
      kokoro \
      langchain-core langchain-ollama langchain-openai \
      mlflow ollama \
      "numpy>=1.26" "pillow>=10.4" tomli

ENV HF_HOME=/opt/hf

# Bake the Kokoro model + the af_heart voice pack into the image, so the
# container needs no network for TTS at run time. Passing voice= is what
# pulls the voice file; without it the model is cached but the voice is not.
# The assert matters: a broken Kokoro otherwise exits 0, the image builds
# clean, and at run time make_tts() quietly falls back to the robotic
# espeak-ng voice with the reason buried in a log line.
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
RUN chmod +x docker/entrypoint.sh launch/*/*.sh

# These are what let the existing launch scripts run unmodified: they already
# read ROS_DOMAIN_ID, OLLAMA_URL/OLLAMA_HOST and BAG from the environment.
ENV ROS_DOMAIN_ID=0 \
    OLLAMA_URL=http://ollama:11434 \
    OLLAMA_HOST=http://ollama:11434 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["sleep", "infinity"]
