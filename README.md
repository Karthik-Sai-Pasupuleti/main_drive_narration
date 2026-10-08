# main_drive_narration

Config-driven drive narration: one ROS2 node (`main.py`) subscribes directly to
Autoware's planning topics (turn detection) and a mobile-pole infrastructure
camera's detections (hazard warnings), and narrates each event to passengers
via an LLM + TTS.

## Testing the pipeline

**Terminal 1** (always the same — the narrator):
```bash
cd /home/karthik/Desktop/rviz_to_speech/main_drive_narration
./launch/action_pipeline/narration.sh
```

**Terminal 2** — pick based on what you want to exercise:

**Turns only:**
```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=0
ros2 bag play "/home/karthik/Desktop/rviz_to_speech/rosbags-HMI/Rosbag_with_planner/rosbag2_2026_05_28-13_40_16" --clock 100
```

**Mobile-pole hazard only:**
```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=0
ros2 bag play /home/karthik/Downloads/DEMO-ROS-BAGS/POLE-INFRA/rosbag2_2026_08_20-17_09_42 \
  --topics /mobile_pole/axis_rgb_6_42/autoware_objects_3d
```

**Both triggers from one bag:**
```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=0
ros2 bag play /home/karthik/Downloads/DEMO-ROS-BAGS/rosbag2_2026_08_06-16_20_58 --clock 100
```

Add `--no-tts` to `narration.sh` for console-only output instead of spoken
audio. Start Terminal 1 first and give it a few seconds to finish loading
(Kokoro/model warm-up) before starting the bag in Terminal 2, so you don't
miss the earliest events.

## Live usage (no bag)

`main.py` subscribes to the raw topics directly, so against a real live stack
you only need:
```bash
./launch/action_pipeline/narration.sh
```
`run_live.sh` is an optional pre-flight check (confirms the live stack is
actually publishing) — it starts no node of its own and isn't required.
