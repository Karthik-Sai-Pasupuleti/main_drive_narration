"""Offline sweep of the ACTION pipeline over the saved turn frames -> MLflow + CSV.

Evaluates the narration OUTPUT and the LATENCY of every vision-capable Ollama
model plus OpenAI's gpt-4o-mini on the SAME saved frames (eval/turn_captures/),
so all models are compared on identical inputs (a live run would show each model
a different frame).

It runs exactly the pipeline main.py's "action" path runs, from the same config
(src/configs/action_pipeline.toml) - one vision bot, one call per event:

    driving_action + forward-view frame + external reports + memories
        -> Action_Based_Narration_Output.narration

The frames were captured on main.py's intersection-turn trigger, so the replayed
event is that same trigger: driving_action = the TURN LEFT string main.py builds,
no external reports. (--scenarios also replays the drone / infrastructure
SLOW DOWN triggers on the same frames, if wanted.)

Every (model, frame) becomes one MLflow run - params, narration_s, the narration
text, the input frame - and one CSV row. No ROS and no RViz needed: it reads the
saved frames and calls the providers directly, reusing the pipeline's own bot
(bot.bot.LLMBot) and prompt (from the config), so the outputs match main.py's
narration path. A model that fails to load, answer, or return valid structured
output is logged as an error and the sweep continues.

    python eval_action_models.py                          # all ollama + gpt-4o-mini
    python eval_action_models.py --models qwen2.5vl:3b,minicpm-v:8b
    python eval_action_models.py --resume                 # skip rows already in the CSV
    python eval_action_models.py --scenarios turn,drone,infra
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import statistics
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
                     Action_Based_Narration_Input, Action_Based_Narration_Output)
from utils.utils import load_config, load_env                    # noqa: E402

CONFIGS_DIR = SRC / "configs"
DEFAULT_CONFIG = CONFIGS_DIR / "action_pipeline.toml"
DEFAULT_IMAGES = ROOT / "eval" / "turn_captures"
DEFAULT_CSV = ROOT / "eval" / "action_eval.csv"
DEFAULT_TRACKING_URI = f"sqlite:///{ROOT / 'mlflow.db'}"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_OPENAI_MODELS = "gpt-4o-mini"

# Reports as published by src/nodes/dummy_events.py, so the optional drone /
# infrastructure scenarios carry the text the live pipeline would receive.
DRONE_REPORT = "an occluded pedestrian crossing behind the parked van on the right"
INFRA_REPORT = "a construction zone 80 metres ahead, right lane closed"
# main.py builds the turn action as: TURN {DIR} (intersection) - we are {approaching|
# now taking} a {dir} turn at the oncoming intersection. The saved frames are all
# "*_left_upcoming_*", i.e. the approaching phase of a left turn.
TURN_ACTION = ("TURN LEFT (intersection) - we are approaching a left turn at the "
               "oncoming intersection")
SLOWDOWN_ACTION = "SLOW DOWN (external report) - the {src} reports a hazard ahead"
# Fixed HUD action memory + narration memory for every run (main.py fills these from
# the live HUD / its own history; freezing them keeps the comparison fair).
ACTION_MEMORY = ("SPEED: 14 km/h | DRIVING | LANE FOLLOW\n"
                 "SPEED: 11 km/h | SLOWING | TURN LEFT (intersection)\n"
                 "SPEED: 9 km/h | TURNING | TURN LEFT (intersection)")
NARRATION_MEMORY = "(none yet)"

SCENARIOS: dict[str, dict] = {
    "turn": {"driving_action": TURN_ACTION,
             "drone_data": "none", "infrastructure_data": "none"},
    "drone": {"driving_action": SLOWDOWN_ACTION.format(src="drone"),
              "drone_data": DRONE_REPORT, "infrastructure_data": "none"},
    "infra": {"driving_action": SLOWDOWN_ACTION.format(src="infrastructure"),
              "drone_data": "none", "infrastructure_data": INFRA_REPORT}}

# OpenAI reasoning models reject temperature != 1 and spend part of their token
# budget on hidden reasoning, so they get temperature 1.0 and a larger cap.
REASONING_RE = re.compile(r"^(o\d|gpt-5)")
REASONING_MIN_PREDICT = 8192

AGENT_ROLE = "narration"
CSV_FIELDS = ["pipeline", "scenario", "provider", "model", "image", "view", "driving_action",
              "drone_data", "infrastructure_data", "narration", "narration_s", "words",
              "valid", "error"]
load_env(str(ROOT / ".env"))


# --------------------------------------------------------------------- discovery
def list_ollama_vision_models() -> list[str]:
    """All Ollama models advertising the 'vision' capability, ordered small->large."""
    try:
        tags = json.load(urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=30))["models"]
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"WARN: cannot list Ollama models at {OLLAMA_URL}: {exc}", file=sys.stderr)
        return []
    found = []
    for m in tags:
        try:
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/show", data=json.dumps({"model": m["name"]}).encode(),
                headers={"Content-Type": "application/json"})
            info = json.loads(urllib.request.urlopen(req, timeout=60).read())
            if "vision" in info.get("capabilities", []):
                found.append((m["name"], m.get("size", 0)))
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    found.sort(key=lambda x: x[1])
    return [name for name, _ in found]


def find_images(images_dir: Path, view: str) -> list[tuple[str, Path, str]]:
    """(name, path, view) per capture. The action pipeline grabs ONE window
    (narration_chase -> the *_front.jpg frames); --view bev/both also feeds the BEV
    frames, which is off-pipeline but shows how much the view matters."""
    out: list[tuple[str, Path, str]] = []
    for kind in (("front", "bev") if view == "both" else (view,)):
        for path in sorted(images_dir.glob(f"*_{kind}.jpg")):
            out.append((path.name[:-len(f"_{kind}.jpg")], path, kind))
    return out


# ------------------------------------------------------------------------- bot
def build_bot(cfg: dict, provider: str, model: str, num_predict: int | None) -> LLMBot:
    """Build the action pipeline's single narration bot on `provider`/`model`.

    Keeps the config's prompt and temperature; forces vision on (the action bot is
    a vision bot) and caps generation so runaway 'thinking' models cannot hang.
    """
    section = dict(cfg[AGENT_ROLE])
    section.update(model=model, provider=provider, vision=True, num_predict=num_predict)
    if provider == "openai" and REASONING_RE.match(model):
        section["temperature"] = 1.0        # reasoning models only accept the default
        section["num_predict"] = max(num_predict, REASONING_MIN_PREDICT) if num_predict else None
    return LLMBot(AgentConfig(**section), Action_Based_Narration_Output, CONFIGS_DIR)


def _run_bot(fn, timeout: float):
    """Invoke a bot in a daemon thread bounded by a wall-clock timeout.

    Returns (result_or_None, seconds, error_or_None). A model that exceeds
    `timeout` (or a stuck load) is abandoned as a TimeoutError so the sweep keeps
    moving; the daemon thread cannot block process exit.
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


def run_one(bot: LLMBot, scenario: dict, image_b64: str, timeout: float) -> dict:
    """One narration event: the model's output text and its inference latency."""
    out, seconds, err = _run_bot(lambda: bot.invoke(Action_Based_Narration_Input(
        driving_action=scenario["driving_action"],
        drone_data=scenario["drone_data"],
        infrastructure_data=scenario["infrastructure_data"],
        action_memory=ACTION_MEMORY,
        narration_memory=NARRATION_MEMORY), image=image_b64), timeout)
    if out is None:
        return {"narration": f"ERROR: {type(err).__name__}: {err}", "narration_s": seconds,
                "words": 0, "valid": False, "error": f"{type(err).__name__}: {str(err)[:300]}"}
    narration = (out.narration or "").strip()
    return {"narration": narration, "narration_s": seconds,
            "words": len(re.findall(r"[\w'’-]+", narration)), "valid": True, "error": ""}


# --------------------------------------------------------------------- logging
def log_mlflow(provider: str, model: str, name: str, view: str, kind: str,
               scenario: dict, res: dict, image_path: Path) -> None:
    with mlflow.start_run(run_name=f"action:{model}:{kind}:{name}"):
        mlflow.log_params({"pipeline": "action", "scenario": kind, "provider": provider,
                           "model": model, f"model_{AGENT_ROLE}": model,
                           f"provider_{AGENT_ROLE}": provider, "image": name, "view": view,
                           "driving_action": scenario["driving_action"],
                           "drone_data": scenario["drone_data"],
                           "infrastructure_data": scenario["infrastructure_data"],
                           "valid": res["valid"]})
        mlflow.log_metrics({"narration_s": round(res["narration_s"], 3),
                            "total_infer_s": round(res["narration_s"], 3),
                            "words": res["words"], "valid": float(res["valid"])})
        mlflow.log_text(res["narration"], "narration.txt")
        try:
            mlflow.log_image(Image.open(image_path), f"input_{view}.jpg")
        except Exception:  # pylint: disable=broad-exception-caught
            pass


def csv_row(provider: str, model: str, name: str, view: str, kind: str,
            scenario: dict, res: dict) -> dict:
    return {"pipeline": "action", "scenario": kind, "provider": provider, "model": model,
            "image": name, "view": view, "driving_action": scenario["driving_action"],
            "drone_data": scenario["drone_data"],
            "infrastructure_data": scenario["infrastructure_data"],
            "narration": res["narration"], "narration_s": round(res["narration_s"], 3),
            "words": res["words"], "valid": res["valid"], "error": res["error"]}


def append_csv(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)


def already_done(path: Path) -> set[tuple[str, str, str, str]]:
    """(model, image, view, scenario) keys already in the CSV (for --resume)."""
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as handle:
        return {(r["model"], r["image"], r.get("view", "front"), r["scenario"])
                for r in csv.DictReader(handle)}


# ------------------------------------------------------------------------ main
def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Offline output + latency sweep of the action narration pipeline -> MLflow.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="pipeline TOML (prompt / temperature come from its [narration])")
    parser.add_argument("--images-dir", default=str(DEFAULT_IMAGES), help="dir of saved turn frames")
    parser.add_argument("--view", choices=("front", "bev", "both"), default="front",
                        help="which frame feeds the bot (front = what the action pipeline grabs)")
    parser.add_argument("--models", default=None,
                        help="comma-separated Ollama models (default: all vision-capable)")
    parser.add_argument("--openai-models", default=DEFAULT_OPENAI_MODELS,
                        help="comma-separated OpenAI models (needs OPENAI_API_KEY)")
    parser.add_argument("--no-ollama", action="store_true", help="skip Ollama models")
    parser.add_argument("--no-openai", action="store_true", help="skip OpenAI models")
    parser.add_argument("--scenarios", default="turn",
                        help=f"comma-separated subset of {','.join(SCENARIOS)}")
    parser.add_argument("--limit-images", type=int, default=0, help="use only the first N frames")
    parser.add_argument("--experiment", default="action_pipeline_eval", help="MLflow experiment")
    parser.add_argument("--tracking-uri", default=DEFAULT_TRACKING_URI,
                        help="MLflow tracking URI (MLFLOW_TRACKING_URI env wins if set)")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="append per-run rows to this CSV")
    parser.add_argument("--num-predict", type=int, default=512,
                        help="max tokens per run; caps runaway 'thinking' models (0 = uncapped)")
    parser.add_argument("--timeout", type=float, default=150.0,
                        help="per-run wall-clock timeout in seconds")
    parser.add_argument("--resume", action="store_true",
                        help="skip (model, image, view, scenario) combos already in the CSV")
    opts = parser.parse_args(argv)

    cfg = load_config(opts.config)
    if AGENT_ROLE not in cfg:
        sys.exit(f"{opts.config} has no [{AGENT_ROLE}] section")
    images = find_images(Path(opts.images_dir), opts.view)
    if opts.limit_images > 0:
        images = images[:opts.limit_images]
    if not images:
        sys.exit(f"no *_{opts.view}.jpg frames found in {opts.images_dir}")
    kinds = [k.strip() for k in opts.scenarios.split(",") if k.strip()]
    unknown = [k for k in kinds if k not in SCENARIOS]
    if unknown:
        sys.exit(f"unknown scenario(s): {unknown}; choose from {list(SCENARIOS)}")

    ollama_models = [] if opts.no_ollama else (
        [m.strip() for m in opts.models.split(",") if m.strip()] if opts.models
        else list_ollama_vision_models())
    openai_models = [] if (opts.no_openai or not opts.openai_models) else [
        m.strip() for m in opts.openai_models.split(",") if m.strip()]
    if openai_models and not os.environ.get("OPENAI_API_KEY"):
        print("WARN: no OPENAI_API_KEY in the env; OpenAI runs will error.", file=sys.stderr)
    entries = [("ollama", m) for m in ollama_models] + [("openai", m) for m in openai_models]
    if not entries:
        sys.exit("no models to evaluate")

    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI") or opts.tracking_uri)
    mlflow.set_experiment(opts.experiment)
    num_predict = opts.num_predict if opts.num_predict > 0 else None
    done_keys = already_done(Path(opts.csv)) if opts.resume else set()
    print(f"tracking  : {mlflow.get_tracking_uri()} | experiment: {opts.experiment}")
    print(f"pipeline  : action (single vision bot) | config: {opts.config}")
    print(f"prompt    : {cfg[AGENT_ROLE]['prompt']} | temperature: {cfg[AGENT_ROLE].get('temperature')}")
    print(f"frames    : {len(images)} {opts.view!r} frame(s) from {opts.images_dir}")
    print(f"scenarios : {kinds}")
    print(f"guards    : num_predict={num_predict}, per-run timeout={opts.timeout:.0f}s")
    print(f"ollama    : {len(ollama_models)} -> {ollama_models}")
    print(f"openai    : {len(openai_models)} -> {openai_models}")
    print(f"=> {len(entries) * len(images) * len(kinds)} runs\n", flush=True)

    done = fails = 0
    started = time.perf_counter()
    for mi, (provider, model) in enumerate(entries, 1):
        print(f"[{mi}/{len(entries)}] {provider}:{model}", flush=True)
        try:
            bot = build_bot(cfg, provider, model, num_predict)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"    build failed: {exc}; skipping model.", flush=True)
            continue
        times, valid = [], 0
        for name, path, view in images:
            image_b64 = base64.b64encode(path.read_bytes()).decode()
            for kind in kinds:
                if opts.resume and (model, name, view, kind) in done_keys:
                    continue
                res = run_one(bot, SCENARIOS[kind], image_b64, opts.timeout)
                try:
                    log_mlflow(provider, model, name, view, kind, SCENARIOS[kind], res, path)
                    append_csv(Path(opts.csv),
                               csv_row(provider, model, name, view, kind, SCENARIOS[kind], res))
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    print(f"    [{kind}/{name}] logging failed: {exc}", flush=True)
                done += 1
                times.append(res["narration_s"])
                valid += int(res["valid"])
                fails += int(not res["valid"])
                flag = "" if res["valid"] else "  <ERROR>"
                print(f"    [{kind}/{view}/{name.split('_')[0]}] {res['narration_s']:6.2f}s "
                      f"| {res['narration'][:80]!r}{flag}", flush=True)
        if times:
            print(f"    -> valid {valid}/{len(times)}, mean {statistics.mean(times):.2f}s/run "
                  f"(min {min(times):.2f} / max {max(times):.2f})", flush=True)

    print(f"\ndone: {done} runs logged ({fails} error(s)) in "
          f"{(time.perf_counter() - started) / 60.0:.1f} min", flush=True)
    print(f"      CSV -> {opts.csv} | MLflow experiment -> {opts.experiment}", flush=True)
    print(f"      view: mlflow ui --backend-store-uri {mlflow.get_tracking_uri()}", flush=True)


if __name__ == "__main__":
    main()
