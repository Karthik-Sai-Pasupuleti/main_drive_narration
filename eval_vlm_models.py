"""Offline VLM sweep for the multi-agent narration pipeline, logged to MLflow.

Evaluates EVERY vision-capable Ollama model on the SAME saved turn frames, so the
models are compared on identical inputs (a live run would show each model a
different frame). For each model it runs the exact 3-bot pipeline main.py uses:

  - road-geometry bot : describes the BEV image     (eval/turn_captures/*_bev.jpg)
  - obstacles bot     : describes the front image   (*_front.jpg)
  - narration bot     : driving_action = "TURN LEFT (intersection)" + the two
                        descriptions -> one narration sentence

Each bot's latency is measured. Every (model, image) becomes one MLflow run
(params, per-bot metrics road_s/obstacles_s/narration_s/total_infer_s, the three
text outputs, and the two input images) and one row in a CSV matching eval/eval.csv.

No ROS and no RViz needed - it reads the saved frames and calls Ollama directly,
reusing the pipeline's own bots (bot.bot.LLMBot) and prompts (from the config),
so results match main.py's narration path. Robust: a model that fails to load or
answer is logged as an error and the sweep continues.

    python eval_vlm_models.py                          # all vision models, eval/turn_captures/
    python eval_vlm_models.py --models qwen2.5vl:3b,gemma4:12b
    python eval_vlm_models.py --images-dir eval/run1 --experiment my_eval
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import mlflow
from PIL import Image

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from bot.bot import (AgentConfig, LLMBot,                        # noqa: E402
                     Multi_Agent_Narration_Input, Multi_Agent_Narration_Output,
                     Road_Geometry_Input, Road_Geometry_Output,
                     Obstacles_description_Input, Obstacles_description_Output)
from utils.utils import load_config, load_env                    # noqa: E402

CONFIGS_DIR = SRC / "configs"
DEFAULT_CONFIG = CONFIGS_DIR / "multi_agent_pipeline.toml"
DEFAULT_IMAGES = ROOT / "eval" / "turn_captures"
DEFAULT_CSV = ROOT / "eval" / "vlm_eval.csv"
DEFAULT_ACTION = "TURN LEFT (intersection)"
DEFAULT_TRACKING_URI = f"sqlite:///{ROOT / 'mlflow.db'}"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
AGENT_ROLES = ("road_geometry", "obstacles", "narration")
CSV_FIELDS = ["pipeline", "event", "model", "image", "driving_action", "road_geometry",
              "obstacles", "narration", "road_s", "obstacles_s", "narration_s", "total_infer_s"]
load_env(str(ROOT / ".env"))


def list_vision_models() -> list[str]:
    """All Ollama models advertising the 'vision' capability, ordered small->large."""
    tags = json.load(urllib.request.urlopen(f"{OLLAMA_URL}/api/tags"))["models"]
    found = []
    for m in tags:
        try:
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/show", data=json.dumps({"model": m["name"]}).encode(),
                headers={"Content-Type": "application/json"})
            info = json.loads(urllib.request.urlopen(req).read())
            if "vision" in info.get("capabilities", []):
                found.append((m["name"], m.get("size", 0)))
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    found.sort(key=lambda x: x[1])
    return [name for name, _ in found]


def find_pairs(images_dir: Path) -> list[tuple[str, Path, Path]]:
    """(name, bev_path, front_path) for each *_bev.jpg with a matching *_front.jpg."""
    pairs = []
    for bev in sorted(images_dir.glob("*_bev.jpg")):
        front = bev.with_name(bev.name.replace("_bev.jpg", "_front.jpg"))
        if front.exists():
            pairs.append((bev.name[:-len("_bev.jpg")], bev, front))
    return pairs


def build_bots(cfg: dict, provider: str, model: str, num_predict: int | None):
    """Build the 3 pipeline bots, all using `provider`/`model` (road/obstacles vision,
    narration text). num_predict caps generation so runaway 'thinking' models can't hang."""
    def agent(role, output_model, vision):
        section = dict(cfg[role])
        section.update(model=model, provider=provider, vision=vision, num_predict=num_predict)
        return LLMBot(AgentConfig(**section), output_model, CONFIGS_DIR)
    return (agent("road_geometry", Road_Geometry_Output, True),
            agent("obstacles", Obstacles_description_Output, True),
            agent("narration", Multi_Agent_Narration_Output, False))


def _run_bot(fn, timeout: float):
    """Invoke a bot in a daemon thread bounded by a wall-clock timeout.

    Returns (result_or_None, seconds, error_or_None). A model that exceeds
    `timeout` (or a stuck load) is abandoned as a TimeoutError so the sweep keeps
    moving; the daemon thread can't block process exit.
    """
    start = time.perf_counter()
    box: dict = {}

    def target():
        try:
            box["r"] = fn()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            box["e"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    seconds = time.perf_counter() - start
    if thread.is_alive():
        return None, seconds, TimeoutError(f"exceeded {timeout:.0f}s")
    if "e" in box:
        return None, seconds, box["e"]
    return box.get("r"), seconds, None


def run_pair(bots, action: str, bev_b64: str, front_b64: str, timeout: float) -> dict:
    """Run the 3 bots on one image pair; outputs + per-bot seconds (errors captured)."""
    geometry, obstacles, narration = bots
    road_out, road_s, e1 = _run_bot(
        lambda: geometry.invoke(Road_Geometry_Input(Bev_image=bev_b64)), timeout)
    road = road_out.road_geometry.strip() if road_out else f"ERROR: {type(e1).__name__}: {e1}"
    obst_out, obst_s, e2 = _run_bot(lambda: obstacles.invoke(
        Obstacles_description_Input(front_perspective_image=front_b64)), timeout)
    obstacles_txt = obst_out.obstacles_description.strip() if obst_out else f"ERROR: {type(e2).__name__}: {e2}"
    narr_out, narr_s, e3 = _run_bot(lambda: narration.invoke(Multi_Agent_Narration_Input(
        driving_action=action, road_geometry_description=road, obstacles_description=obstacles_txt,
        drone_data="none", infrastructure_data="none",
        action_memory="(no readings)", narration_memory="(none yet)")), timeout)
    narration_txt = narr_out.Narration.strip() if narr_out else f"ERROR: {type(e3).__name__}: {e3}"
    return {"road": road, "obstacles": obstacles_txt, "narration": narration_txt,
            "road_s": road_s, "obstacles_s": obst_s, "narration_s": narr_s,
            "ok": not (e1 or e2 or e3)}


def log_mlflow(provider: str, model: str, image_name: str, action: str, res: dict,
               bev_path: Path, front_path: Path) -> None:
    total = res["road_s"] + res["obstacles_s"] + res["narration_s"]
    with mlflow.start_run(run_name=f"multiagent:{model}:{image_name}"):
        params = {"pipeline": "multiagent", "event": "turn", "model": model,
                  "image": image_name, "driving_action": action, "provider": provider,
                  "ok": res["ok"]}
        for role in AGENT_ROLES:
            params[f"model_{role}"] = model
            params[f"provider_{role}"] = provider
        mlflow.log_params(params)
        mlflow.log_metrics({"road_s": round(res["road_s"], 3), "obstacles_s": round(res["obstacles_s"], 3),
                            "narration_s": round(res["narration_s"], 3), "total_infer_s": round(total, 3)})
        mlflow.log_text(res["narration"], "narration.txt")
        mlflow.log_text(res["road"], "road_geometry.txt")
        mlflow.log_text(res["obstacles"], "obstacles.txt")
        for path, name in ((bev_path, "input_bev.jpg"), (front_path, "input_perspective.jpg")):
            try:
                mlflow.log_image(Image.open(path), name)
            except Exception:  # pylint: disable=broad-exception-caught
                pass


def csv_row(model: str, image_name: str, action: str, res: dict) -> dict:
    total = res["road_s"] + res["obstacles_s"] + res["narration_s"]
    return {"pipeline": "multiagent", "event": "turn", "model": model, "image": image_name,
            "driving_action": action, "road_geometry": res["road"], "obstacles": res["obstacles"],
            "narration": res["narration"], "road_s": round(res["road_s"], 3),
            "obstacles_s": round(res["obstacles_s"], 3), "narration_s": round(res["narration_s"], 3),
            "total_infer_s": round(total, 3)}


def append_csv(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Offline VLM sweep of the multi-agent pipeline -> MLflow.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="pipeline TOML (for prompts/settings)")
    parser.add_argument("--images-dir", default=str(DEFAULT_IMAGES), help="dir of *_bev.jpg / *_front.jpg pairs")
    parser.add_argument("--models", default=None,
                        help="comma-separated Ollama model list (default: all vision-capable Ollama models)")
    parser.add_argument("--openai-models", default=None,
                        help="comma-separated OpenAI model(s) to also evaluate, e.g. gpt-4o-mini (needs OPENAI_API_KEY)")
    parser.add_argument("--action", default=DEFAULT_ACTION, help="fixed driving_action for every run")
    parser.add_argument("--experiment", default="vlm_multiagent_eval", help="MLflow experiment name")
    parser.add_argument("--tracking-uri", default=DEFAULT_TRACKING_URI,
                        help="MLflow tracking URI (MLFLOW_TRACKING_URI env wins if set)")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="append per-run rows to this CSV")
    parser.add_argument("--num-predict", type=int, default=512,
                        help="max tokens per bot; caps runaway 'thinking' models (0 = uncapped)")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="per-bot wall-clock timeout in seconds")
    opts = parser.parse_args(argv)

    cfg = load_config(opts.config)
    pairs = find_pairs(Path(opts.images_dir))
    if not pairs:
        sys.exit(f"no *_bev.jpg / *_front.jpg pairs found in {opts.images_dir}")
    ollama_models = ([m.strip() for m in opts.models.split(",") if m.strip()] if opts.models
                     else ([] if opts.openai_models else list_vision_models()))
    openai_models = ([m.strip() for m in opts.openai_models.split(",") if m.strip()]
                     if opts.openai_models else [])
    entries = [("ollama", m) for m in ollama_models] + [("openai", m) for m in openai_models]
    if not entries:
        sys.exit("no models to evaluate")

    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI") or opts.tracking_uri)
    mlflow.set_experiment(opts.experiment)
    print(f"tracking : {mlflow.get_tracking_uri()}  | experiment: {opts.experiment}")
    print(f"images   : {len(pairs)} pair(s) from {opts.images_dir}: {[p[0] for p in pairs]}")
    num_predict = opts.num_predict if opts.num_predict > 0 else None
    print(f"action   : {opts.action!r}")
    print(f"guards   : num_predict={num_predict}, per-bot timeout={opts.timeout:.0f}s")
    print(f"models   : {len(entries)} -> {[f'{p}:{m}' for p, m in entries]}")
    print(f"=> {len(entries) * len(pairs)} runs\n", flush=True)

    done = fails = 0
    for mi, (provider, model) in enumerate(entries, 1):
        print(f"[{mi}/{len(entries)}] {provider}:{model}", flush=True)
        try:
            bots = build_bots(cfg, provider, model, num_predict)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"    build failed: {exc}; skipping model.", flush=True)
            fails += 1
            continue
        for name, bev_path, front_path in pairs:
            res = run_pair(bots, opts.action, base64.b64encode(bev_path.read_bytes()).decode(),
                           base64.b64encode(front_path.read_bytes()).decode(), opts.timeout)
            try:
                log_mlflow(provider, model, name, opts.action, res, bev_path, front_path)
                append_csv(Path(opts.csv), csv_row(model, name, opts.action, res))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                print(f"    [{name}] logging failed: {exc}", flush=True)
            done += 1
            fails += 0 if res["ok"] else 1
            flag = "" if res["ok"] else "  <ERROR>"
            print(f"    [{name}] road={res['road_s']:.2f}s obst={res['obstacles_s']:.2f}s "
                  f"narr={res['narration_s']:.2f}s | {res['narration'][:70]!r}{flag}", flush=True)

    print(f"\ndone: {done} runs logged ({fails} bot error(s)) -> {opts.csv} + MLflow '{opts.experiment}'",
          flush=True)


if __name__ == "__main__":
    main()
