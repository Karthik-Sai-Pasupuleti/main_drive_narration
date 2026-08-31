# VLM multi-agent narration — model comparison report

- **Ground truth:** `gpt-4o-mini` (OpenAI). Every other model is compared to it.
- **Task:** fixed `driving_action = "TURN LEFT (intersection)"`; road-geometry from the BEV frame, obstacles from the front frame, then narration.
- **Test inputs:** 3 turn frame-pairs (`eval/turn_captures/`). Source: `/home/karthik/Desktop/rviz_to_speech/main_drive_narration/eval/vlm_eval.csv`.
- **Agreement:** gpt-4o-mini LLM judge (temp 0), 0-100 vs GT.

## Summary

- **17** models compared against the `gpt-4o-mini` reference.
- **9** failed validation on **every** run (0 clean outputs) — structured-output / JSON failures, all 'thinking' or oversized models.
- **4** were partially valid; **4** produced clean output on all runs.
- **Recommended:** `minicpm-v4.5:latest` — 3/3 valid, 4.0s/run, agreement 50/100 with GT.

## Ranked comparison

| # | model | valid | val-errors | latency s (road/obst/narr) | agreement→GT | maneuver ok | sample narration |
|--:|---|:--:|:--:|---|:--:|:--:|---|
| 1 | `minicpm-v4.5:latest` | 3/3 | 0 | 4.0 (3.0/0.7/0.3) | 50 | 2/3 | We're turning left as we approach this sharp curve ahead. |
| 2 | `openbmb/minicpm-o2.6:latest` | 3/3 | 0 | 4.3 (3.3/0.6/0.3) | 27 | 1/3 | We're turning left where it curves sharply to right ahead. |
| 3 | `llava-phi3:latest` | 3/3 | 0 | 4.3 (3.5/0.5/0.3) | 27 | 2/3 | We're turning left at the intersection ahead. The pedestrian |
| 4 | `minicpm-v:8b` | 3/3 | 0 | 4.1 (3.2/0.6/0.3) | 23 | 0/3 | We're turning left where the road curves right. |
| 5 | `gemma4:latest` | 1/3 | 2 | 8.5 (3.3/2.5/2.7) | 75 | 1/1 | We're turning left as the road curves gently ahead of us. |
| 6 | `minicpm-v4.6:latest` | 1/3 | 2 | 4.1 (2.2/1.1/0.7) | 70 | 1/1 | We're turning left at the intersection. |
| 7 | `qwen2.5vl:3b` | 2/3 | 1 | 6.0 (2.8/0.6/2.6) | 40 | 1/2 | We are turning left where the road bends ahead. |
| 8 | `drivefusionqa:latest` | 2/3 | 1 | 7.5 (3.4/1.2/2.8) | 40 | 1/2 | We're easing left where the intersection opens up ahead. |
| 9 | `qwen3-vl:2b` | 0/3 | 8 | 3.7 (1.3/1.3/1.1) | - | - | ERROR: OutputParserException: Invalid json output:  For trou |
| 10 | `qwen3.5:2b` | 0/3 | 9 | 6.2 (2.8/1.8/1.7) | - | - | ERROR: OutputParserException: Invalid json output:  For trou |
| 11 | `qwen3-vl:4b` | 0/3 | 8 | 6.9 (2.9/2.1/1.9) | - | - | ERROR: OutputParserException: Invalid json output:  For trou |
| 12 | `qwen3.5:4b` | 0/3 | 9 | 9.3 (4.8/2.3/2.1) | - | - | ERROR: OutputParserException: Invalid json output:  For trou |
| 13 | `qwen3-vl:8b` | 0/3 | 9 | 11.2 (5.7/2.9/2.6) | - | - | ERROR: OutputParserException: Invalid json output:  For trou |
| 14 | `qwen3.6:latest` | 0/2 | 6 | 12.0 (6.9/2.7/2.4) | - | - | ERROR: OutputParserException: Invalid json output:  For trou |
| 15 | `gemma4:12b` | 0/3 | 5 | 17.2 (9.5/4.4/3.4) | - | - | We're turning left at the intersection ahead. |
| 16 | `gemma4:31b` | 0/3 | 3 | 24.3 (8.3/8.2/7.8) | - | - | ERROR: OutputParserException: Invalid json output:  For trou |
| 17 | `oroboroslabs/deepseek-v4-pro-neuromorph9.6gb:latest` | 0/3 | 3 | 43.4 (14.9/15.7/12.8) | - | - | We are turning left as the road curves gently around the cor |

_Reference row — `gpt-4o-mini`: 4.4s/run, sample: "We're turning left as the road curves gently ahead."._

## How to read this

- **valid** = runs where all 3 bots returned clean structured output (n = # test images).
- **val-errors** = individual bot outputs that failed validation (max = 3×n).
- **agreement→GT** = LLM-judge score (0-100) vs the gpt-4o-mini reference, averaged over valid runs.
- **Caveats:** gpt-4o-mini is both the reference and the judge (self-similarity bias); only 3 images, all left-turns. Latency is local Ollama vs OpenAI API and depends on hardware/network.
