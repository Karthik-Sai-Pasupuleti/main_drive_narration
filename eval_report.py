"""Build a clean 'best model' report from eval/vlm_eval.csv.

gpt-4o-mini is the ground truth: for each turn image its road / obstacles /
narration are the reference. Every other model is scored on three axes:

  1. reliability - how many of its runs produced clean structured output (no
     validation error from a bot),
  2. latency    - average seconds per run (and per bot),
  3. agreement  - an LLM judge (gpt-4o-mini, temperature 0) rates 0-100 how well
     the model's road/obstacles/narration match the gpt-4o-mini reference for
     the same image, and whether the left-turn maneuver is conveyed correctly.

Writes eval/vlm_eval_report.md and prints a ranked table. Reads only the CSV;
the judge runs over the valid rows (needs OPENAI_API_KEY in .env).

    python eval_report.py                 # reliability + latency + judged agreement
    python eval_report.py --no-judge      # reliability + latency only (no API calls)
"""
from __future__ import annotations

import argparse
import collections
import csv
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
from utils.utils import load_env                                 # noqa: E402

GT_MODEL = "gpt-4o-mini"
DEFAULT_CSV = ROOT / "eval" / "vlm_eval.csv"
DEFAULT_REPORT = ROOT / "eval" / "vlm_eval_report.md"
FIELDS = ("road_geometry", "obstacles", "narration")
load_env(str(ROOT / ".env"))


def is_error(text: str) -> bool:
    return text.strip().startswith("ERROR:")


def run_valid(row: dict) -> bool:
    return not any(is_error(row[f]) for f in FIELDS)


def make_judge():
    """gpt-4o-mini structured judge, or None if unavailable."""
    from langchain_openai import ChatOpenAI
    from pydantic import BaseModel, Field

    class Judgement(BaseModel):
        agreement: int = Field(description="0-100: how well the candidate's road+obstacles+"
                               "narration match the reference for the same scene")
        maneuver_ok: bool = Field(description="does the candidate narration correctly convey "
                                  "the left turn at the intersection")
        note: str = Field(description="one short phrase on the main difference from the reference")

    model = ChatOpenAI(model="gpt-4o-mini", temperature=0).with_structured_output(Judgement)

    def judge(gt: dict, cand: dict, action: str):
        prompt = (
            "You compare an autonomous-driving narration system's output to a REFERENCE "
            "(treated as ground truth). Same scene and driving action for both.\n\n"
            f"DRIVING ACTION: {action}\n\n"
            f"REFERENCE (ground truth):\n- road: {gt['road_geometry']}\n"
            f"- obstacles: {gt['obstacles']}\n- narration: {gt['narration']}\n\n"
            f"CANDIDATE:\n- road: {cand['road_geometry']}\n"
            f"- obstacles: {cand['obstacles']}\n- narration: {cand['narration']}\n\n"
            "Rate overall agreement 0-100 (scene understanding + narration accuracy vs the "
            "reference), whether the left-turn maneuver is correctly conveyed, and one short note.")
        return model.invoke(prompt)

    return judge


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Best-model report from the VLM eval CSV (gpt-4o-mini = GT).")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--out", default=str(DEFAULT_REPORT))
    parser.add_argument("--no-judge", action="store_true", help="skip the LLM agreement judge (no API)")
    opts = parser.parse_args(argv)

    rows = list(csv.DictReader(open(opts.csv, encoding="utf-8")))
    by_model: dict[str, list[dict]] = collections.OrderedDict()
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)
    if GT_MODEL not in by_model:
        sys.exit(f"ground-truth model {GT_MODEL!r} not found in {opts.csv}")
    gt = {r["image"]: r for r in by_model[GT_MODEL]}

    judge = None
    if not opts.no_judge:
        try:
            judge = make_judge()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"WARN: judge unavailable ({exc}); reporting reliability + latency only.", file=sys.stderr)

    stats = []
    for model, mrows in by_model.items():
        if model == GT_MODEL:
            continue
        n = len(mrows)
        valid = [r for r in mrows if run_valid(r)]
        errs = sum(1 for r in mrows for f in FIELDS if is_error(r[f]))
        lat = statistics.mean(float(r["total_infer_s"]) for r in mrows)
        road_s = statistics.mean(float(r["road_s"]) for r in mrows)
        obst_s = statistics.mean(float(r["obstacles_s"]) for r in mrows)
        narr_s = statistics.mean(float(r["narration_s"]) for r in mrows)
        agrees, maneuver = [], []
        if judge is not None:
            for r in valid:
                if r["image"] not in gt:
                    continue
                try:
                    j = judge(gt[r["image"]], r, r["driving_action"])
                    agrees.append(j.agreement)
                    maneuver.append(j.maneuver_ok)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
        agreement = round(statistics.mean(agrees), 1) if agrees else None
        maneuver_ok = f"{sum(maneuver)}/{len(maneuver)}" if maneuver else "-"
        sample = next((r["narration"] for r in valid), mrows[0]["narration"])
        stats.append({"model": model, "n": n, "valid": len(valid), "errors": errs,
                      "lat": lat, "road_s": road_s, "obst_s": obst_s, "narr_s": narr_s,
                      "agreement": agreement, "maneuver_ok": maneuver_ok, "sample": sample})

    # rank: fully-valid first, then higher agreement, then lower latency
    stats.sort(key=lambda s: (-(s["valid"] == s["n"]), -(s["agreement"] or -1), s["lat"]))

    n_models = len(stats)
    fully = [s for s in stats if s["valid"] == s["n"]]
    partial = [s for s in stats if 0 < s["valid"] < s["n"]]
    failed = [s for s in stats if s["valid"] == 0]
    best = fully[0] if fully else (partial[0] if partial else None)

    def fmt_agree(a):
        return "-" if a is None else f"{a:.0f}"

    lines = []
    lines.append("# VLM multi-agent narration — model comparison report\n")
    lines.append(f"- **Ground truth:** `{GT_MODEL}` (OpenAI). Every other model is compared to it.")
    lines.append(f"- **Task:** fixed `driving_action = \"TURN LEFT (intersection)\"`; road-geometry from the "
                 "BEV frame, obstacles from the front frame, then narration.")
    lines.append(f"- **Test inputs:** {len(gt)} turn frame-pairs (`eval/turn_captures/`). Source: `{opts.csv}`.")
    lines.append(f"- **Agreement:** {'gpt-4o-mini LLM judge (temp 0), 0-100 vs GT' if judge else 'not scored (--no-judge)'}.\n")

    lines.append("## Summary\n")
    lines.append(f"- **{n_models}** models compared against the `{GT_MODEL}` reference.")
    lines.append(f"- **{len(failed)}** failed validation on **every** run (0 clean outputs) — "
                 "structured-output / JSON failures, all 'thinking' or oversized models.")
    lines.append(f"- **{len(partial)}** were partially valid; **{len(fully)}** produced clean output on all runs.")
    if best:
        lines.append(f"- **Recommended:** `{best['model']}` — "
                     f"{best['valid']}/{best['n']} valid, {best['lat']:.1f}s/run"
                     + (f", agreement {fmt_agree(best['agreement'])}/100 with GT." if best['agreement'] is not None else "."))
    lines.append("")

    lines.append("## Ranked comparison\n")
    header = "| # | model | valid | val-errors | latency s (road/obst/narr) | agreement→GT | maneuver ok | sample narration |"
    lines.append(header)
    lines.append("|--:|---|:--:|:--:|---|:--:|:--:|---|")
    for i, s in enumerate(stats, 1):
        lines.append(f"| {i} | `{s['model']}` | {s['valid']}/{s['n']} | {s['errors']} | "
                     f"{s['lat']:.1f} ({s['road_s']:.1f}/{s['obst_s']:.1f}/{s['narr_s']:.1f}) | "
                     f"{fmt_agree(s['agreement'])} | {s['maneuver_ok']} | {s['sample'][:60].replace(chr(10),' ').replace('|','/')} |")
    lines.append("")
    lines.append(f"_Reference row — `{GT_MODEL}`: "
                 f"{statistics.mean(float(r['total_infer_s']) for r in by_model[GT_MODEL]):.1f}s/run, "
                 f"sample: \"{gt[list(gt)[0]]['narration'][:70]}\"._\n")

    lines.append("## How to read this\n")
    lines.append("- **valid** = runs where all 3 bots returned clean structured output (n = # test images).")
    lines.append("- **val-errors** = individual bot outputs that failed validation (max = 3×n).")
    lines.append("- **agreement→GT** = LLM-judge score (0-100) vs the gpt-4o-mini reference, averaged over valid runs.")
    lines.append("- **Caveats:** gpt-4o-mini is both the reference and the judge (self-similarity bias); "
                 f"only {len(gt)} images, all left-turns. Latency is local Ollama vs OpenAI API and depends on hardware/network.\n")

    report = "\n".join(lines)
    Path(opts.out).write_text(report, encoding="utf-8")

    # terminal view
    print(f"\n{'#':>2}  {'model':<40}{'valid':>6}{'err':>5}{'lat_s':>7}{'agree':>7}  maneuver")
    print("-" * 82)
    for i, s in enumerate(stats, 1):
        vn = f"{s['valid']}/{s['n']}"
        print(f"{i:>2}  {s['model']:<40}{vn:>6}{s['errors']:>5}"
              f"{s['lat']:>7.1f}{fmt_agree(s['agreement']):>7}  {s['maneuver_ok']}")
    print(f"\nfully-valid={len(fully)}  partial={len(partial)}  failed={len(failed)}  "
          f"(GT={GT_MODEL})")
    if best:
        print(f"recommended: {best['model']} ({best['valid']}/{best['n']} valid, {best['lat']:.1f}s/run, "
              f"agreement {fmt_agree(best['agreement'])})")
    print(f"\nreport written -> {opts.out}")


if __name__ == "__main__":
    main()
