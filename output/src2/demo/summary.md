# Narration run demo

- **config**: /home/karthik/Desktop/rviz_to_speech/main_drive_narration/src2/config/pipeline.toml
- **duration_s**: 120
- **clock speed**: 20.0x
- **layer 1 model**: ollama/gemma4:latest
- **layer 3 model**: ollama/gemma4:latest
- **frames**: narration_chase
- **audio**: written + played

7 narration event(s).

| t (s) | layer | event | model | infer (s) | speech (s) | files |
| ---: | --- | --- | --- | ---: | ---: | --- |
| 15 | layer1 | turn_left | ollama/gemma4:latest | 2.88 | 3.62 | `layer1/turn_01_left.txt` + `.wav` |
| 30 | layer3 | infrapole | ollama/gemma4:latest | 2.20 | 6.20 | `layer3/report_01_infrapole.txt` + `.wav` |
| 45 | layer1 | turn_right | ollama/gemma4:latest | 2.88 | 4.03 | `layer1/turn_02_right.txt` + `.wav` |
| 60 | layer3 | drone | ollama/gemma4:latest | 2.42 | 3.86 | `layer3/report_02_drone.txt` + `.wav` |
| 75 | layer1 | turn_left | ollama/gemma4:latest | 3.36 | 4.50 | `layer1/turn_03_left.txt` + `.wav` |
| 90 | layer3 | robot | ollama/gemma4:latest | 2.80 | 6.15 | `layer3/report_03_robot.txt` + `.wav` |
| 105 | layer1 | turn_right | ollama/gemma4:latest | 2.63 | 4.02 | `layer1/turn_04_right.txt` + `.wav` |

## Spoken script

- **t=15s [turn_left]** We are turning left now as we enter the wide intersection ahead.
- **t=30s [infrapole]** We are slowing down - the infrastructure pole reports a hidden pedestrian crossing ahead of the parked van.
- **t=45s [turn_right]** We are turning right at this upcoming junction into the available lane.
- **t=60s [drone]** We are easing off again - the drone reports a crash on the road ahead.
- **t=75s [turn_left]** We are turning left through the wide intersection, following the clear lane ahead.
- **t=90s [robot]** We are shifting left - the robot reports a construction zone eighty metres ahead with the right lane closed.
- **t=105s [turn_right]** We are turning right as we follow the curving path through this intersection.
