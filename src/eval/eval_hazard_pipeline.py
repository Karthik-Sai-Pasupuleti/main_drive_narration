"""Compare two hazard-narration architectures on one fixed test case, end-to-end
including TTS, so their LATENCY can be measured (not a correctness eval).

Both parts share the same stage-2 "context" LLM call (hazard_context_prompt.toml):
given a scene_description (plain text) + the driving action + memory, produce
one spoken narration. They differ only in how that scene_description text is
produced:

  Part A (vlm):        image --[VLM caption call]--> text --> context LLM --> TTS
  Part B (detections):  3D detections --[detections_to_text(), no model]--> text --> context LLM --> TTS

So Part A pays for an extra model call (the caption); Part B's stage-1 is free
(pure code). That's the latency gap this script is meant to surface.

Test case: one frame of /mobile_pole/axis_rgb_6_42/image_visualization from a
rosbag, plus the /mobile_pole/axis_rgb_6_42/autoware_objects_3d detection
closest to it in time.

    python src/eval/eval_hazard_pipeline.py --bag /path/to/rosbag2_xxx_0.mcap
    python src/eval/eval_hazard_pipeline.py --method vlm --repeat 3
    python src/eval/eval_hazard_pipeline.py --no-tts --csv eval/hazard_pipeline.csv
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # src/eval/<this file> -> repo root
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from bot.bot import AgentConfig, LLMBot, Action_Based_Narration_Output  # noqa: E402
from pydantic import BaseModel, Field                                  # noqa: E402
from utils.detections_to_text import detections_to_text                # noqa: E402
from utils.tts import make_tts                                         # noqa: E402
from utils.utils import load_env                                       # noqa: E402

CONFIGS_DIR = SRC / "configs" / "action_narration"
DEFAULT_BAG = ("/home/karthik/Downloads/DEMO-ROS-BAGS/POLE-INFRA/"
               "rosbag2_2026_08_20-17_09_42/rosbag2_2026_08_20-17_09_42_0.mcap")
IMAGE_TOPIC = "/mobile_pole/axis_rgb_6_42/image_visualization"
OBJECTS_TOPIC = "/mobile_pole/axis_rgb_6_42/autoware_objects_3d"
CSV_FIELDS = ["part", "rep", "caption_model", "context_model", "stage1_s", "context_s",
              "speech_s", "total_s", "scene_description", "narration"]

# Known models to play with, keyed by a short --context-model name.
# vision=False models can only run the shared context LLM (both Part A and
# Part B call it) - they can NOT run Part A's image-captioning stage.
MODEL_REGISTRY: dict[str, dict] = {
    "gemma4":          {"provider": "ollama", "model": "gemma4:latest",  "vision": True},
    "gemma4-12b":      {"provider": "ollama", "model": "gemma4:12b",     "vision": True},
    "gemma4-31b":      {"provider": "ollama", "model": "gemma4:31b",     "vision": True},
    "minicpm-v":       {"provider": "ollama", "model": "minicpm-v:8b",         "vision": True},
    "minicpm-v4.5":    {"provider": "ollama", "model": "minicpm-v4.5:latest",  "vision": True},
    "minicpm-v4.6":    {"provider": "ollama", "model": "minicpm-v4.6:latest",  "vision": True},
    "qwen2.5vl":       {"provider": "ollama", "model": "qwen2.5vl:3b",   "vision": True},
    "qwen3-vl":        {"provider": "ollama", "model": "qwen3-vl:4b",   "vision": True},
    "llava-phi3":      {"provider": "ollama", "model": "llava-phi3:latest", "vision": True},
    "qwen3.5":         {"provider": "ollama", "model": "qwen3.5:4b",    "vision": False},
    "qwen2.5":         {"provider": "ollama", "model": "qwen2.5:3b",    "vision": False},
    "phi4-mini":       {"provider": "ollama", "model": "phi4-mini",     "vision": False},
    "gpt-4o-mini":     {"provider": "openai", "model": "gpt-4o-mini",   "vision": True},
}

load_env(str(ROOT / ".env"))


# -------------------------------------------------------------- stage-1 models
class Scene_Caption_Input(BaseModel):
    """No text fields - the image is passed via LLMBot.invoke(image=...)."""


class Scene_Caption_Output(BaseModel):
    scene_description: str = Field(description="factual description of the image")


class Hazard_Narration_Input(BaseModel):
    """Shared stage-2 input: a scene_description (from either part) + context.
    Fields match hazard_context_prompt.toml's placeholders."""

    driving_action: str
    scene_description: str
    drone_data: str = "none"
    infrastructure_data: str = "none"
    action_memory: str = "(no readings)"
    narration_memory: str = "(none yet)"


def _timed(fn):
    start = time.perf_counter()
    return fn(), time.perf_counter() - start


# -------------------------------------------------------------- test-case loading
_PIL_MODE = {"rgb8": ("RGB", "RGB"), "bgr8": ("RGB", "BGR"), "mono8": ("L", "L")}


def _decode_ros_image(ros_msg):
    """sensor_msgs/Image -> PIL.Image (rgb8 / bgr8 / mono8 only)."""
    from PIL import Image
    mode, raw_mode = _PIL_MODE[ros_msg.encoding]
    return Image.frombytes(mode, (ros_msg.width, ros_msg.height), bytes(ros_msg.data),
                           "raw", raw_mode, ros_msg.step)


def load_test_case(bag_path: str, frame_index: int = 20, window_s: float = 0.6):
    """Return (image_b64, detections) for one frame of the mobile-pole bag.

    detections is a list[dict] (label, confidence, position) from the
    /mobile_pole/.../autoware_objects_3d message closest in time to the
    frame_index-th /mobile_pole/.../image_visualization message.
    """
    from mcap_ros2.reader import read_ros2_messages

    count = 0
    img_stamp = None
    img_png = None
    for msg in read_ros2_messages(bag_path, topics=[IMAGE_TOPIC]):
        count += 1
        if count == frame_index:
            img_stamp = msg.log_time_ns
            img_png = _decode_ros_image(msg.ros_msg)
            break
    if img_stamp is None:
        raise RuntimeError(f"bag has fewer than {frame_index} messages on {IMAGE_TOPIC}")

    window_ns = int(window_s * 1e9)
    best = None
    for msg in read_ros2_messages(bag_path, topics=[OBJECTS_TOPIC]):
        d = abs(msg.log_time_ns - img_stamp)
        if d > window_ns:
            continue
        if best is None or d < best[0]:
            best = (d, msg)
    objects = []
    if best is not None:
        for obj in best[1].ros_msg.objects:
            cls = max(obj.classification, key=lambda c: c.probability)
            p = obj.kinematics.pose_with_covariance.pose.position
            objects.append({"label": cls.label, "confidence": cls.probability,
                            "position": (p.x, p.y, p.z)})

    buf = io.BytesIO()
    img_png.save(buf, format="JPEG", quality=85)
    image_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return image_b64, objects


# -------------------------------------------------------------- the two parts
def run_part_a(caption_bot: LLMBot, context_bot: LLMBot, image_b64: str,
               driving_action: str, tts) -> dict:
    """image -> VLM caption -> text -> context LLM -> narration -> TTS."""
    cap_out, stage1_s = _timed(lambda: caption_bot.invoke(Scene_Caption_Input(),
                                                          image=image_b64))
    scene = cap_out.scene_description.strip()
    ctx_out, context_s = _timed(lambda: context_bot.invoke(
        Hazard_Narration_Input(driving_action=driving_action, scene_description=scene)))
    narration = ctx_out.narration.strip()
    _, speech_s = _timed(lambda: (tts.speak(narration), tts.wait(60.0)))
    return {"stage1_s": stage1_s, "context_s": context_s, "speech_s": speech_s,
            "scene_description": scene, "narration": narration}


def run_part_b(context_bot: LLMBot, objects: list[dict], driving_action: str,
               tts) -> dict:
    """detections -> text (no model) -> context LLM -> narration -> TTS."""
    scene, stage1_s = _timed(lambda: detections_to_text(objects))
    ctx_out, context_s = _timed(lambda: context_bot.invoke(
        Hazard_Narration_Input(driving_action=driving_action, scene_description=scene)))
    narration = ctx_out.narration.strip()
    _, speech_s = _timed(lambda: (tts.speak(narration), tts.wait(60.0)))
    return {"stage1_s": stage1_s, "context_s": context_s, "speech_s": speech_s,
            "scene_description": scene, "narration": narration}


def append_csv(path: str, records: list[dict]) -> None:
    if not records:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    new = not target.exists()
    with target.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new:
            writer.writeheader()
        for row in records:
            writer.writerow(row)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bag", default=DEFAULT_BAG, help="path to the .mcap rosbag")
    parser.add_argument("--frame", type=int, default=20,
                        help="which image_visualization message to use (1-based)")
    parser.add_argument("--method", choices=["vlm", "detections", "both"], default="both",
                        help="which part(s) to run")
    parser.add_argument("--model", default="gemma4:latest", help="model for both LLM calls")
    parser.add_argument("--provider", default="ollama", help="'ollama' (default, local/free) or 'openai'")
    parser.add_argument("--context-model", choices=sorted(MODEL_REGISTRY), default=None,
                        help="override ONLY the shared context LLM (used by both Part A and "
                        "Part B) with a known model from the registry, e.g. phi4-mini. "
                        "--model/--provider keep controlling Part A's image-captioning stage.")
    parser.add_argument("--repeat", type=int, default=1, help="repetitions per part")
    parser.add_argument("--no-tts", action="store_true", help="skip speech (0s speech_s)")
    parser.add_argument("--csv", default=str(ROOT / "eval" / "hazard_pipeline.csv"),
                        help="append per-rep rows here")
    parser.add_argument("--driving-action", default=(
        "SLOW DOWN (external report) - the infrastructure reports a possible "
        "pedestrian hazard ahead"), help="ground-truth driving action for both parts")
    args = parser.parse_args(argv)

    print(f"loading test case: bag={args.bag} frame={args.frame}")
    image_b64, objects = load_test_case(args.bag, args.frame)
    print(f"  detections near this frame: {len(objects)} object(s)")

    if args.method in ("vlm", "both"):
        caption_entry = next((e for e in MODEL_REGISTRY.values() if e["model"] == args.model), None)
        if caption_entry is not None and not caption_entry["vision"]:
            print(f"WARNING: {args.model!r} is text-only in the registry; Part A's "
                  f"image-captioning stage will likely fail with it.")

    context_provider, context_model = args.provider, args.model
    if args.context_model:
        entry = MODEL_REGISTRY[args.context_model]
        context_provider, context_model = entry["provider"], entry["model"]

    caption_cfg = AgentConfig(provider=args.provider, model=args.model, vision=True,
                              prompt="vlm_caption_prompt.toml")
    context_cfg = AgentConfig(provider=context_provider, model=context_model, vision=False,
                              prompt="hazard_context_prompt.toml")
    caption_bot = LLMBot(caption_cfg, Scene_Caption_Output, str(CONFIGS_DIR))
    context_bot = LLMBot(context_cfg, Action_Based_Narration_Output, str(CONFIGS_DIR))
    tts = make_tts(enabled=not args.no_tts)

    records: list[dict] = []
    try:
        for part, label in (("vlm", "A"), ("detections", "B")):
            if args.method not in (part, "both"):
                continue
            for rep in range(1, args.repeat + 1):
                if part == "vlm":
                    res = run_part_a(caption_bot, context_bot, image_b64,
                                     args.driving_action, tts)
                else:
                    res = run_part_b(context_bot, objects, args.driving_action, tts)
                total = res["stage1_s"] + res["context_s"] + res["speech_s"]
                row = {"part": label, "rep": rep, "caption_model": args.model,
                       "context_model": context_model,
                       "stage1_s": round(res["stage1_s"], 3),
                       "context_s": round(res["context_s"], 3),
                       "speech_s": round(res["speech_s"], 3),
                       "total_s": round(total, 3),
                       "scene_description": res["scene_description"],
                       "narration": res["narration"]}
                records.append(row)
                stage1_name = "caption_s" if part == "vlm" else "detect_s"
                print(f"\n[Part {label}] rep {rep}/{args.repeat} "
                      f"({stage1_name}={row['stage1_s']}s context_s={row['context_s']}s "
                      f"speech_s={row['speech_s']}s TOTAL={row['total_s']}s)")
                print(f"  scene_description: {row['scene_description']}")
                print(f"  narration: {row['narration']}")
    finally:
        tts.close()

    if records:
        append_csv(args.csv, records)
        print(f"\n{len(records)} row(s) -> {args.csv}")

    by_part: dict[str, list[dict]] = {}
    for row in records:
        by_part.setdefault(row["part"], []).append(row)
    if len(by_part) > 1:
        print("\n--- summary (mean total_s) ---")
        for label, rows in by_part.items():
            mean_total = sum(r["total_s"] for r in rows) / len(rows)
            print(f"  Part {label}: {mean_total:.3f}s avg over {len(rows)} rep(s)")


if __name__ == "__main__":
    main()
