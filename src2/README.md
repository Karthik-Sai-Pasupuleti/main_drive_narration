# src2 — three-layer drive narration, without a rosbag

`src2` is a self-contained rebuild of the narration demo that needs **no rosbag, no
roscore and no rclpy**. Every event that used to come out of a recording now comes out
of a small JSON file, and each spoken line is written to disk as **text and audio side
by side**.

```
                         120 s scripted drive (src2/config/pipeline.toml)
   ┌──────────────────────────┐ ┌────────────────────────────┐ ┌──────────────────────────┐
   │ LAYER 1                  │ │ LAYER 2                    │ │ LAYER 3                  │
   │ turn detection (event)   │ │ EVENTS from a dummy file   │ │ single LLM subscriber    │
   │  ↓ captures the frame    │ │ ros_node_1 infrapole @30 s │ │ subscribes to the 3 nodes│
   │  ↓ LEFT/RIGHT + image    │ │ ros_node_2 drone     @60 s │ │ system prompt = the role │
   │  ↓ VLM describes it      │ │ ros_node_3 robot     @90 s │ │ user prompt  = 3 slots   │
   │  → refined commentary    │ │ each on its own topic ────────→ refined context         │
   │  → layer1/*.txt + *.wav  │ │                            │ │ → layer3/*.txt + *.wav   │
   └──────────────────────────┘ └────────────────────────────┘ └──────────────────────────┘
```

## Run it

```bash
python src2/run_demo.py                       # the full 120 s drive, speaking as it goes
python src2/run_demo.py --speed 10            # same script replayed in 12 s
python src2/run_demo.py --dry-run --speed 20  # timeline + files only, no model contacted
python src2/run_demo.py --model qwen3-vl:4b   # override both layers' model
python src2/run_demo.py --no-audio            # still writes the WAVs, plays nothing
```

Ollama must be up (`ollama serve`) for a real run; `--dry-run` needs nothing at all.
Only `provider = "openai"` needs `OPENAI_API_KEY` in the repo's `.env`.

## What each layer does

**Layer 1 — turn detection → VLM → commentary.** `config/dummy_turns.json` holds the
turn events (15 s left, 45 s right, 75 s left, 105 s right). When one fires, the RViz
window is grabbed *at that moment* and handed to the vision model together with the
direction as ground truth. The model describes the scene as it actually is and returns
one refined sentence, which is written to `layer1/turn_NN_<dir>.txt` and rendered to
`layer1/turn_NN_<dir>.wav` next to it (the frame is saved as `.jpg` too).

Without a reachable RViz window the frames come from `eval/turn_captures`, so layer 1
is still a real image-grounded call rather than a text-only stub.

**Layer 2 — dummy publishers.** `config/dummy_events.json` is the entire event source.
Each entry becomes a publisher node on its own topic of an in-process bus
(`core/bus.py`) that mirrors a ROS graph:

| node | source | topic | at |
| --- | --- | --- | --- |
| `ros_node_1` | infrapole | `/infrastructure/reports` | 30 s |
| `ros_node_2` | drone | `/drone/reports` | 60 s |
| `ros_node_3` | robot | `/robot/reports` | 90 s |

**Layer 3 — the single LLM subscriber.** One subscription per topic, one shared model.
The rules live in the system prompt (the role); the user prompt has exactly three
placeholders, one per node, and a node that has not published yet reads
`(not received yet)`. So the 30 s call sees one report, the 60 s call two and the 90 s
call all three — the model refines the newest report into context consistent with what
the passenger was already told, and the result is written to `layer3/report_NN_<source>.txt`
with its `.wav` beside it.

## What a run produces

```
output/src2/20260916-120157/
├── layer1/  turn_01_left.txt   turn_01_left.wav   turn_01_left.jpg   …
├── layer3/  report_01_infrapole.txt   report_01_infrapole.wav        …
├── transcript.jsonl   # one machine-readable record per event (timings, model, frame)
└── summary.md         # the run as a table plus the full spoken script
```

Each `.txt` carries a header (drive time, model, inference seconds, frame origin) and
the model's full reasoning surface — the scene for layer 1, the three subscriber slots
and the refined context for layer 3 — followed by the line that was spoken.

## Layout

```
src2/
├── run_demo.py                     # entry point: wires the 3 layers onto one clock
├── config/
│   ├── pipeline.toml               # timing, models, output dir, capture, voice
│   ├── dummy_turns.json            # layer 1 events
│   ├── dummy_events.json           # layer 2 events (the 3 publisher nodes)
│   └── prompts/
│       ├── layer1_turn_prompt.toml      # system role + user template (vision)
│       └── layer3_subscriber_prompt.toml# system role + the 3 node placeholders
├── core/
│   ├── bus.py          # in-process pub/sub with the rclpy method names
│   ├── scheduler.py    # the drive clock (replaces the rosbag), with --speed
│   ├── worker.py       # one thread so narrations never overlap or delay the clock
│   ├── frames.py       # RViz grab, falling back to eval/turn_captures
│   ├── speech.py       # Kokoro → espeak-ng → silent; always writes the WAV
│   ├── artifacts.py    # the run folder: text, audio and frame written together
│   ├── schemas.py      # the layers' Pydantic inputs/outputs
│   ├── llm.py          # builds an LLMBot from a config section
│   └── srcpath.py      # the one place that makes ../src importable
└── layers/
    ├── layer1_turn_narration.py
    ├── layer2_event_publishers.py
    └── layer3_subscriber.py
```

`src2` re-uses the ROS-free parts of `src/` rather than copying them: `bot/bot.py`
(LangChain + structured output), `utils/tts.py` (Kokoro / espeak-ng) and
`utils/vision.py` (the screen grab). Nothing in `src/` or `main.py` is modified.

## Editing the demo

* **Change a spoken event** — edit `message` / `at` in `config/dummy_events.json`.
* **Change the turns** — edit `config/dummy_turns.json`.
* **Change the voice or the rules** — the prompts under `config/prompts/`.
* **Change the models** — `[layer1]` / `[layer3]` in `config/pipeline.toml`, or `--model`.
* **Add a fourth node** — add the entry to `dummy_events.json`, add its slot to `SLOTS`
  in `layers/layer3_subscriber.py`, the field to `SubscriberInput` in `core/schemas.py`
  and the placeholder to the layer-3 user prompt.

## Going back to real ROS 2

Layer 2 and layer 3 only ever touch `Bus.create_publisher()` / `create_subscription()`,
which are the rclpy method names on purpose. Swapping the bus for an object backed by
real `rclpy` publishers and subscriptions (and dropping the layer-2 timeline cues, since
the messages would then arrive from the network) leaves both layers unchanged. Layer 1's
turn events likewise map back onto `src/utils/vehicle_actions_extraction.py`, whose
`on_route_deviation` callback has the same `(direction, phase)` signature as
`TurnCommentaryLayer.on_turn`.
