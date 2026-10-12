# main_drive_narration

Spoken hazard warnings for an autonomous vehicle. One ROS2 node (`main.py`)
watches two hazard sources, and when one of them reports something worth
slowing down for, a vision model looks at that source's camera frame and
writes a sentence for the passengers, which is then spoken aloud.

| Source | Decides *when* to narrate | Decides *what* is said |
|---|---|---|
| Mobile pole (roadside infrastructure) | `/mobile_pole/axis_rgb_6_42/autoware_objects_3d` — a pedestrian crossing into range of the ego vehicle | `/mobile_pole/axis_rgb_6_42/image_visualization` |
| Robot | `/robot/detections` — a person above the confidence threshold | `/robot/image_annotated` |

The detections only act as the trigger. The narration itself always comes from
captioning the reporting camera's own frame, so the vehicle never announces a
hazard that isn't visible. Both sources share one model and one prompt
(`src/configs/narration_prompt.toml`); the `source` field tells the model which
camera it is looking through and whom to credit for the report.

## Requirements

- ROS 2 Humble, with the `autoware_perception_msgs` and `vision_msgs` packages
- [Ollama](https://ollama.com) running locally, with the model pulled:
  `ollama pull gemma4:latest`
- Python deps: `uv sync --extra tts` (the `tts` extra installs Kokoro; without
  it the node falls back to espeak-ng, then to silence)

For the optional chase-cam viewer only: `rviz2`, `robot_state_publisher`, and
Autoware's `autoware_perception_rviz_plugin` (the ego model reuses its
`car.dae` mesh). The narrator itself needs none of these.

## Running it

**Terminal 1 — the narrator:**
```bash
./launch/action_pipeline/narration.sh
```
It starts Ollama if it isn't already up, loads the model into VRAM, then waits
for hazards. Add `--no-tts` to print the narration instead of speaking it.
Start this first and let it finish loading before playing a bag, otherwise you
will miss the earliest events.

**Terminal 2 — a recorded drive:**
```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=0
ros2 bag play ~/Downloads/DEMO-ROS-BAGS/POLE-INFRA/rosbag2_2026_08_20-17_09_42
```
Or use `./launch/action_pipeline/run_demo.sh`, which plays the same bag looping
and paused, and starts it on ENTER.

The mobile-pole source needs its detections topic, its camera topic, **and** a
`map -> base_link` transform — the distance gate can't fire without an ego
pose. The robot source needs only its two topics.

Note the POLE-INFRA bag contains no `/robot/detections` or
`/robot/image_annotated`, so only the mobile-pole source fires with it. It does
carry its own `/clock` and `/robot_description`, which the scripts detect (see
below).

Against a live stack, Terminal 1 is all you need; the node subscribes to the
raw topics directly.

**Terminal 3 (optional) — watch the drive:**
```bash
./launch/action_pipeline/view_rviz.sh
```
A chase camera (`rviz/narration_chase.rviz`) locked to `base_link`, so it rides
behind the ego vehicle for the whole drive. The script also starts
`robot_state_publisher` with `rviz/ego_vehicle.urdf`, because the bag carries
`/tf` but not `/robot_description` — without it the camera follows an invisible
car. Purely a human viewer; `main.py` needs no display at all.

RViz runs on sim time, so exactly one thing must publish `/clock` and one thing
must publish `/robot_description`. Both scripts detect this rather than
assuming:

- `run_demo.sh` greps the bag's `metadata.yaml` and only passes `--clock` when
  the bag has no recorded clock of its own. Two clock publishers make sim time
  jump between them, which stalls the chase camera.
- `view_rviz.sh` starts `robot_state_publisher` with `rviz/ego_vehicle.urdf`
  only when nothing is already publishing `/robot_description`. Force it with
  `EGO_MODEL=on` (bag has no model) or `EGO_MODEL=off` (never publish one).

Override the config with `RVIZ_CONFIG=path/to/other.rviz`. Note this is the
full Autoware config: most of its displays are for planning and localization
topics this pipeline never publishes, so they render nothing, and there are no
displays for the hazard sources themselves.

## Changing settings

There is no config file. Everything lives in labelled constants at the top of
`main.py`:

| Constant | Purpose |
|---|---|
| `PROVIDER`, `MODEL`, `TEMPERATURE` | the vision model (`ollama`, or `openai` with `OPENAI_API_KEY` in `.env`) |
| `PROMPT` | path to the prompt file |
| `POLE_*_TOPIC`, `ROBOT_*_TOPIC` | the four topics subscribed to |
| `POLE_HAZARD_CLASSES`, `ROBOT_HAZARD_CLASSES` | which detected classes count as a hazard |
| `MIN_CONFIDENCE` | detector confidence floor |
| `TRIGGER_DISTANCE_M` | pole only: narrate as a hazard crosses into this range |
| `COOLDOWN_S` | never re-fire faster than this |
| `CLEAR_GRACE_S` | seconds with no detection before a hazard counts as gone |

Wording changes go in `src/configs/narration_prompt.toml`, not in the code.

`CLEAR_GRACE_S` is worth understanding before tuning it: a detector dropping
the hazard for a single frame does not mean the pedestrian left, so a sighting
is only considered over after this many seconds with nothing seen. Until then
the node will not re-narrate the same person.

## Adding a third source (e.g. a drone)

1. Add its two topic constants and any thresholds at the top of `main.py`.
2. Add an image callback that caches the latest frame, and a detection
   callback that calls `self._enqueue("drone", detections, image)` once its
   conditions are met.
3. Add `"drone"` to the source list in the prompt so the model knows whom to
   credit.

The prompt and the schemas already take an arbitrary `source` name, so nothing
else needs to change.

## Docker

```bash
export BAG_PATH=/path/to/rosbag2_folder      # the folder containing metadata.yaml
docker compose build
docker compose up -d
docker compose exec ollama ollama pull gemma4:latest
```
Then run the two terminals as the startup banner prints them. The bag is
bind-mounted from the host, not baked into the image.

## Layout

```
main.py                            the whole ROS2 node; settings are constants at the top
src/bot/bot.py                     model wrapper + the two pydantic schemas
src/configs/narration_prompt.toml  the one prompt
src/utils/detections_to_text.py    detections -> the text hint sent with the image
src/utils/tts.py                   Kokoro -> espeak-ng -> silent, latest line wins
src/utils/utils.py                 TOML + .env loading
launch/action_pipeline/            narration.sh, run_demo.sh, view_rviz.sh
rviz/narration_chase.rviz          chase-cam viewer config
rviz/ego_vehicle.urdf              the ego car mesh, published on /robot_description
```

## Troubleshooting

**`[RTPS_TRANSPORT_SHM Error] Failed init_port fastrtps_portNNNN`**
A previous ROS process still holds that shared-memory port. DDS falls back to
UDP, so the pipeline still works, but the cause is usually a suspended node:
`Ctrl+Z` stops a process without killing it, and `narration.sh`'s `pkill` then
cannot reach it, because a stopped process never runs its signal handler. Use
`Ctrl+C` to stop the narrator, not `Ctrl+Z`. To clear a stuck one:

```bash
ps -o pid,stat,cmd -C python3 | grep main.py    # look for STAT = T (stopped)
kill -9 <pid>
rm -f /dev/shm/fastrtps_* /dev/shm/sem.fastrtps_*   # only with no ROS running
```

**`TF_OLD_DATA ignoring data from the past for frame base_link`, repeating.**
The bag is looping. On restart every `/tf` stamp jumps backwards by the bag's
whole duration, and tf2 only keeps a 10s window, so it drops everything that
follows and keeps serving the stale end-of-run pose. The console floods *and*
the mobile-pole distance gate silently stops firing, because the ego vehicle
appears frozen. Replay without `--loop` (the default in `run_demo.sh`); if you
looped deliberately, restart the narrator too, since its TF buffer is already
poisoned.

**The chase camera follows an invisible car.** Nothing is publishing
`/robot_description`. Check that `robot_state_publisher` started —
`view_rviz.sh` logs it to `/tmp/ego_model.log`.

**No narration, no trigger logs.** Check the sources are actually publishing
(`ros2 topic hz <topic>`). The mobile pole additionally needs a
`map -> base_link` transform; without it the node logs a throttled warning
every 5s and never fires.

**The first narration is slow.** Expected if the model warm-up failed — look
for `failed to warm ollama model` at startup.

## Notes on timing

Every narration prints a full latency breakdown in the `narration.sh` terminal:

```
[INFO] [narrator]: pole trigger fired: pedestrian at 8.3m
[INFO] [narrator]: NARRATION [mobile_pole]: We are slowing down because ...
[INFO] [narrator]: timing [mobile_pole]: image 14:53:28.412 -> trigger +0.21s
                   -> gemma4:latest 3.84s -> speech 5.12s | total 9.17s
```

- **image** — wall-clock time the frame used for this narration arrived
- **trigger** — how stale that frame was when the model call started
- **model** — the vision call: image in, narration text out
- **speech** — Kokoro synthesis *and* playback, i.e. until the line finishes
- **total** — frame arrival through to the end of speech

Camera frames arrive far too fast to log individually, so there is no
per-frame line; the timestamp of the frame that actually got narrated is in
the `timing` line. Apart from that, the node only logs when a trigger fires.

The model is loaded into VRAM at startup, before any subscription exists —
otherwise that cold load (measured at ~4s, more on a cold page cache) would be
paid by the first hazard, which is the one where latency matters most.

Narration is serial: one hazard is spoken fully before the next starts, and
events are dropped if more than 16 queue up.
