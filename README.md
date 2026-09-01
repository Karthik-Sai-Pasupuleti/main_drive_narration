# Autonomous Drive Narration System — Codebase Overview & System Design

Welcome to the **`main_drive_narration`** codebase! This document provides a complete, detailed walkthrough of the entire system architecture, tech stacks, input/processing/output flows, and end-to-end execution paths.

---

## 1. Executive Summary & Purpose

`main_drive_narration` is a real-time, config-driven, autonomous vehicle passenger narration system. It converts live ROS 2 autonomous driving telemetry (planning factors, vehicle odometry, route maneuvers) and visual camera perspectives (rendered via RViz) into human-friendly, grounded spoken voice narrations for in-car passengers.



---

## 2. File Structure & Codebase Map

```
main_drive_narration/
├── pyproject.toml              # Project dependencies (langchain, ollama, mlflow, kokoro)
├── main.py                     # Primary entry point & ROS 2 Narrator Node orchestrator
├── launch/                     # Shell scripts to launch ROS nodes and benchmarks
│   ├── action_pipeline/        # Single-agent launch scripts (run_demo.sh, narration.sh)
│   ├── multiple_agent_pipleine/# Multi-agent launch scripts (run_demo.sh, narration.sh)
│   └── evaluation.sh           # Automated per-model benchmarking loop
├── src/
│   ├── bot/
│   │   └── bot.py              # LLMBot engine, LangChain integration, Pydantic schemas
│   ├── configs/                # TOML pipeline configurations & prompt templates
│   │   ├── action_pipeline.toml
│   │   ├── multi_agent_pipeline.toml
│   │   ├── events.json         # Demo timeline for external V2X reports
│   │   ├── action_narration/
│   │   └── road_obstacles_action_narration/
│   ├── nodes/                  # ROS 2 Python nodes
│   │   ├── hud_relay.py        # Odometry/ADAPI -> ROS 2 status report relay
│   │   ├── factor_overlay.py   # Planning factors -> /hud/decision OverlayText node
│   │   ├── event_publisher.py  # Scripted V2X report timer node
│   │   └── dummy_events.py     # Quick test event publisher
│   ├── utils/                  # Helper modules
│   │   ├── tts.py              # Kokoro & espeak-ng TTS implementation
│   │   ├── vehicle_actions_extraction.py # HUD parser & turn detection regex logic
│   │   ├── vision.py           # RViz window capture & downscaling
│   │   └── utils.py            # TOML config & .env loader
│   └── rviz/                   # RViz configurations & ego vehicle URDF
│       ├── ego_vehicle.urdf
│       ├── narration_chase.rviz
│       └── narration_bev.rviz
```

---

## 2. End-to-End Execution Flow (Step-by-Step)

Here is exactly what happens when you launch the system:

```
[Command execution: ./launch/action_pipeline/run_demo.sh]
 ├── 1. Kills any old RViz or ROS nodes to prevent topic conflict.
 ├── 2. Launches 'ros2 bag play' (paused on frame 0, looping mode).
 ├── 3. Starts 'hud_relay.py' and 'factor_overlay.py' on wall-clock time.
 ├── 4. Starts 'robot_state_publisher' with ego_vehicle.urdf.
 ├── 5. Waits for planning topics to become active, then launches RViz with narration_chase.rviz.
 └── 6. Waits for user to press ENTER -> Calls ROS 2 service to resume bag playback -> Launches 'event_publisher.py'.

[Command execution: ./launch/action_pipeline/narration.sh (Terminal 2)]
 ├── 1. Checks if Ollama is running at localhost:11434 (starts 'ollama serve' if down).
 ├── 2. Executes 'python main.py --config src/configs/action_pipeline.toml'.
 └── 3. main.py initializes rclpy & instantiates Narrator node:
      ├── Loads TOML config & .env file.
      ├── Instantiates VehicleActionsExtractor (subscribes to /hud/decision).
      ├── Instantiates LLMBot chains with Pydantic output validation models.
      ├── Starts background worker thread '_run_worker'.
      └── Enters ROS MultiThreadedExecutor spin loop.

[Driving Event Cycle]
 ├── A. Car approaches an intersection -> Autoware publishes Steering/Velocity factors.
 ├── B. 'factor_overlay.py' updates /hud/decision with "TURN LEFT (intersection)".
 ├── C. 'VehicleActionsExtractor' detects the new action string, fires 'on_action' callback.
 ├── D. 'Narrator._enqueue()' captures RViz window via 'vision.capture()' (Base64 JPEG) and pushes job to Queue.
 ├── E. Worker thread dequeues job -> Calls 'LLMBot.invoke()'.
 ├── F. LangChain formats prompt + Base64 image -> Sends to Ollama/OpenAI -> Receives structured JSON output.
 ├── G. 'Narrator' prints narration log & sends text to 'tts.speak()'.
 ├── H. 'TextToSpeech' synthesizes speech with Kokoro on CPU and plays via 'aplay'.
 └── I. (If --eval enabled) Logs timings, images, and text outputs to MLflow & appends row to CSV file.
```

---

## 3. How to Run & Common Command Examples

### 1. Run Single-Agent Demo (Live)
```bash
# Terminal 1: Start Bag, RViz, and HUD Overlays
./launch/action_pipeline/run_demo.sh

# Terminal 2: Start Narration Engine
./launch/action_pipeline/narration.sh
```

### 2. Run Multi-Agent Demo
```bash
# Terminal 1
./launch/multiple_agent_pipleine/run_demo.sh

# Terminal 2
./launch/multiple_agent_pipleine/narration.sh
```


---

## 4. Run in Docker (no local ROS 2, RViz, Ollama or Python setup)

Everything the pipeline needs is in the image: ROS 2 Humble, RViz, the Autoware
message packages, a virtual X display (RViz has to render - `vision.py`
screenshots its window), and Kokoro TTS with its weights baked in.

**The rosbag is not in the image.** It stays on the host and is bind-mounted
read-only, so you need your own copy.

### First-time setup

```bash
cp .env.example .env                  # empty is fine; action pipeline is ollama-only
export BAG_PATH=/path/to/rosbag2_2026_08_06-16_20_58   # folder with metadata.yaml

docker compose build                  # ~40 min, once
docker compose up -d
docker compose exec ollama ollama pull gemma4:latest   # 9.6 GB, once
```

### Running the demo

Same two terminals as the host workflow, just prefixed:

```bash
# Terminal 1: bag + RViz + HUD overlays
docker compose exec narration bash -lc \
  'printf "\n" | ./launch/action_pipeline/run_demo.sh'

# Terminal 2: narration engine
docker compose exec narration ./launch/action_pipeline/narration.sh
```

- **Watch RViz:** <http://localhost:6080/vnc.html> (the virtual display, in a browser)
- **Audio out:** `./output/audio/*.wav` - one WAV per narration line, plus the
  scripted cues in `./cache/audio/`

### Notes

- `BAG_PATH` must be the directory containing `metadata.yaml`, not the `.mcap`
  file and not its parent. It must be the **planner** bag (the one with the
  `/api/planning/*` factor topics) - camera-only HMI bags leave the HUD blank
  and nothing triggers a narration.
- Pull the model your config actually names. `src/configs/action_pipeline.toml`
  currently uses `gemma4:latest` (9.6 GB); the multi-agent config needs
  `gemma4:12b` and `gemma4:31b` as well. A missing model fails at the first
  event with `model '...' not found (status code: 404)`.
- **CPU inference is slow.** For a quick smoke test on a smaller model,
  override it without editing the config:
  `docker compose exec narration ./launch/action_pipeline/narration.sh --model qwen2.5vl:3b`
  (verified in-container: 6.4 s per event, vs 20.3 s for `gemma4:latest`).
  For a demo that keeps up with
  the drive, uncomment the `deploy:` GPU block in `docker-compose.yml` and
  install the NVIDIA Container Toolkit on the host.
- WAV saving is driven by `NARRATION_WAV_DIR`, set only in the container. On a
  normal host `tts.py` behaves exactly as before and keeps nothing.
- `uv` is deliberately absent from the image: `narration.sh` prefers
  `uv run python`, which would build a venv without `rclpy`.

### Useful commands

| Command | Does what |
| --- | --- |
| `docker compose logs -f narration` | Follow the container output |
| `docker compose exec narration bash` | Shell inside the container |
| `docker compose down` | Stop; keeps the image and the model volume |
| `docker compose build --no-cache` | Rebuild ignoring cached layers |

---

## 5. Summary & Key Takeaways.
- **Config-Driven:** Everything (models, providers, prompts, capture windows) is defined in TOML files under `src/configs/`. You can change models without touching Python code.
- **Robust Overlay Clocking:** HUD nodes use `ClockType.SYSTEM_TIME` (wall time) instead of sim time so looping rosbags don't cause RViz text overlays to freeze.
- **Verification & Grounding:** Ground-truth driving actions are enforced via strict system prompts so the VLM can never hallucinate a different maneuver than what the vehicle is actually doing.
- **Fail-Safe Capture:** If screen capture fails (e.g. headless environment or missing display), `vision.py` safely returns `None` and falls back to a transparent 1x1 image so text-only narration continues seamlessly.
