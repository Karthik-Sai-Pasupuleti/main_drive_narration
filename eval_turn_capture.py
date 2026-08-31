"""Save the BEV + front-view RViz frames on every NARRATION trigger.

A standalone eval helper for the multi-agent pipeline. It runs as its own ROS
node and fires on exactly the same triggers main.py's Narrator narrates on, so
the frames it writes are precisely the frames that would be narrated - no more,
no less. On each trigger it grabs BOTH RViz windows - narration_bev (BEV, road
geometry) and narration_chase (front / perspective, obstacles) - and writes
them to disk as a JPEG pair. No VLM and no TTS run here, so the result is a
clean, narration-aligned image set for evaluating the narration models against
exactly what the car saw.

The narrator's narration triggers (mirrored here 1:1):
  - turn   : intersection left/right turn  (VehicleActionsExtractor on /hud/decision)
  - drone  : SLOW DOWN, drone report        (/drone/reports, deduped by text)
  - infra  : SLOW DOWN, infrastructure report (/infrastructure/reports, deduped by text)
In run_demo.sh only `turn` fires; drone/infra fire only when those topics are
published (e.g. `python3 src/nodes/dummy_events.py`). It reuses the pipeline's
own detector + screen grabber and re-resolves each RViz window's region on every
trigger, so you can still arrange the two windows side by side after launching.

Run the demo stack first (RViz BEV + chase + HUD), then in another terminal:

    python eval_turn_capture.py                         # -> eval/turn_captures/
    python eval_turn_capture.py --out eval/run1 --events 8
    python eval_turn_capture.py --downscale 1.0 --quality 95   # (defaults: full res)

--events / --timeout bound the run so it exits on its own; otherwise it runs
until Ctrl-C. It may run alongside narration.sh (both react to the same
triggers); ROS will warn about a duplicate node name, which is harmless.
"""
from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from utils.utils import load_config                         # noqa: E402
from utils.vision import capture, rviz_bbox                 # noqa: E402
from utils.vehicle_actions_extraction import VehicleActionsExtractor  # noqa: E402

DEFAULT_CONFIG = SRC / "configs" / "multi_agent_pipeline.toml"
DEFAULT_OUT = ROOT / "eval" / "turn_captures"


class NarrationCapture(Node):
    """Grabs the BEV + front RViz windows on each narration trigger the narrator
    fires on (intersection turn, drone report, infrastructure report), writing
    one BEV+front JPEG pair per trigger."""

    def __init__(self, bev_win: str, persp_win: str, out_dir: Path,
                 downscale: float = 1.0, quality: int = 95) -> None:
        super().__init__("narration_capture")
        self.bev_win = bev_win
        self.persp_win = persp_win
        self.out_dir = out_dir
        self.downscale = downscale     # 1.0 = full RViz-window resolution (no shrink)
        self.quality = quality         # JPEG quality (1-95)
        out_dir.mkdir(parents=True, exist_ok=True)
        self._counts = {"turn": 0, "drone": 0, "infra": 0}  # per-kind sequence numbers
        self.saved = 0                 # trigger events with >=1 frame written
        self._last_drone = "none"      # dedup state, mirroring main.py's Narrator
        self._last_infra = "none"
        # Resolve once up front purely to warn if a window is missing (a None
        # bbox makes capture() grab the whole screen). Re-resolved per trigger.
        for label, win in (("BEV", bev_win), ("front", persp_win)):
            if rviz_bbox(win) is None:
                self.get_logger().warning(
                    f"no RViz window matching {win!r} ({label}); its frames would be "
                    "full-screen grabs until it appears.")
        # turn trigger: reuse the pipeline's own detector (same callback main.py uses)
        self.extractor = VehicleActionsExtractor(on_route_deviation=self.on_turn)
        # external-report triggers: same topics + QoS + dedup as main.py's Narrator
        self.create_subscription(String, "/drone/reports", self.on_drone, 10)
        self.create_subscription(String, "/infrastructure/reports", self.on_infra, 10)
        self.get_logger().info(
            f"narration-capture running: triggers=turn+drone+infra, "
            f"BEV={bev_win!r}, front={persp_win!r} "
            f"(downscale={downscale}, quality={quality}) -> {out_dir}")

    # ------------------------------------------------ triggers (mirror Narrator)
    def on_turn(self, direction: str, phase: str) -> None:
        """Intersection turn -> capture the frame pair."""
        self._save_pair("turn", f"{direction}_{phase}")

    def on_drone(self, msg: String) -> None:
        """New drone report (SLOW DOWN) -> capture the frame pair."""
        if not msg.data or msg.data == self._last_drone:
            return
        self._last_drone = msg.data
        self.get_logger().info(f"drone report: {msg.data}")
        self._save_pair("drone")

    def on_infra(self, msg: String) -> None:
        """New infrastructure report (SLOW DOWN) -> capture the frame pair."""
        if not msg.data or msg.data == self._last_infra:
            return
        self._last_infra = msg.data
        self.get_logger().info(f"infrastructure report: {msg.data}")
        self._save_pair("infra")

    # ------------------------------------------------ capture + save
    def _save_pair(self, kind: str, extra: str = "") -> None:
        """Grab BEV + front (re-resolving each window region) and write the pair."""
        self._counts[kind] += 1
        seq = self._counts[kind]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        tag = f"{kind}{seq:03d}_{stamp}" + (f"_{extra}" if extra else "")
        written = []
        for view, win in (("bev", self.bev_win), ("front", self.persp_win)):
            frame = capture(bbox=rviz_bbox(win), downscale=self.downscale, quality=self.quality)
            if not frame:
                self.get_logger().warning(f"{tag}: no {view} frame captured; skipping.")
                continue
            name = f"{tag}_{view}.jpg"
            try:
                (self.out_dir / name).write_bytes(base64.b64decode(frame))
                written.append(name)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self.get_logger().error(f"{tag}: could not write {view} frame: {exc}")
        if written:
            self.saved += 1
            self.get_logger().info(f"narration trigger [{kind} #{seq}]: saved {', '.join(written)}")
        if len(written) < 2:
            self.get_logger().warning(
                f"{tag}: incomplete pair ({len(written)}/2 frames) - "
                "check both RViz windows are open and not overlapping.")

    def close(self) -> None:
        self.extractor.destroy_node()
        self.destroy_node()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Save BEV + front RViz frames on each narration trigger (turn/drone/infra).")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="pipeline TOML (read-only) for the capture window names")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="directory to write the JPEG pairs into")
    parser.add_argument("--bev", default=None, help="override the BEV RViz window title")
    parser.add_argument("--front", default=None,
                        help="override the front/perspective RViz window title")
    parser.add_argument("--events", type=int, default=0,
                        help="stop after N captured triggers (0 = run until Ctrl-C)")
    parser.add_argument("--timeout", type=float, default=0.0,
                        help="stop after this many seconds (0 = no limit)")
    parser.add_argument("--downscale", type=float, default=1.0,
                        help="shrink saved frames by this factor (1.0 = full RViz-window resolution)")
    parser.add_argument("--quality", type=int, default=95,
                        help="JPEG quality of saved frames (1-95)")
    opts, ros_argv = parser.parse_known_args(argv)

    cap = {}
    try:
        cap = load_config(opts.config).get("capture", {})
    except Exception as exc:  # config is optional; CLI/defaults still work
        print(f"WARN: could not read {opts.config} ({exc}); using CLI/defaults.", file=sys.stderr)
    bev_win = opts.bev or cap.get("bev", "narration_bev")
    persp_win = opts.front or cap.get("perspective", "narration_chase")

    rclpy.init(args=ros_argv)
    node = NarrationCapture(bev_win, persp_win, Path(opts.out),
                            downscale=opts.downscale, quality=opts.quality)
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    executor.add_node(node.extractor)
    start = time.monotonic()
    try:
        if opts.events > 0 or opts.timeout > 0:    # bounded run
            while rclpy.ok():
                if opts.events > 0 and node.saved >= opts.events:
                    break
                if opts.timeout > 0 and (time.monotonic() - start) >= opts.timeout:
                    break
                executor.spin_once(timeout_sec=0.5)
        else:                                      # run until interrupted
            executor.spin()
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.get_logger().info(
            f"done: saved {node.saved} trigger(s) {dict(node._counts)} -> {opts.out}")
        node.close()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
