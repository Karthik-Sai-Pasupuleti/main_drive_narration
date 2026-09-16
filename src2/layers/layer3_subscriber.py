"""Layer 3 - the single LLM subscriber sitting under the three publisher nodes.

One subscription per topic, one shared LLM. The rules live in the prompt's system
role; the user prompt has exactly three placeholders - one per ROS node - and the
slots that have not published yet read "(not received yet)". So the 30 s call
sees one report, the 60 s call sees two and the 90 s call sees all three, and the
model's job is to refine the newest report into context that stays consistent
with what the passenger was already told.

Each refined context is written to this layer's folder as text with its WAV
beside it.
"""
from __future__ import annotations

import logging
import time

from core.artifacts import RunFolder
from core.bus import Bus, Message
from core.llm import make_agent, model_label
from core.schemas import NOT_RECEIVED, SubscriberInput, SubscriberOutput
from core.speech import Speaker
from core.worker import SerialWorker

logger = logging.getLogger(__name__)

LAYER = "layer3"
# Which prompt placeholder each publishing node fills (see SubscriberInput).
SLOTS = {"infrapole": "infrapole_report",
         "drone": "drone_report",
         "robot": "robot_report"}


class SubscriberLayer:
    """Keeps the three node slots and narrates each report as it arrives."""

    def __init__(self, cfg: dict, prompts_dir, bus: Bus, topics: list[str],
                 speaker: Speaker, run: RunFolder, worker: SerialWorker,
                 memory: list[str], use_llm: bool = True) -> None:
        """Subscribe to every publisher topic and build the subscriber's model.

        Args:
            cfg (dict): the `[layer3]` config section (provider / model / prompt).
            prompts_dir (str | Path): base directory for the section's `prompt` path.
            bus (Bus): the bus layer 2 publishes on.
            topics (list[str]): topics to subscribe to.
            speaker (Speaker): renders and plays the refined line.
            run (RunFolder): where the text / audio are written.
            worker (SerialWorker): runs the LLM call and the speech off the clock thread.
            memory (list[str]): lines already spoken on this drive (shared with layer 1).
            use_llm (bool, optional): False narrates from a template (dry run).
        """
        self._speaker, self._run, self._worker, self._memory = speaker, run, worker, memory
        self._model = model_label(cfg, use_llm)
        self._agent = make_agent(cfg, SubscriberOutput, prompts_dir, use_llm)
        self._slots = {field: NOT_RECEIVED for field in SLOTS.values()}
        self._count = 0
        for topic in topics:
            bus.create_subscription(topic, self.on_report)
        logger.info("layer 3 subscriber listening on %s", ", ".join(topics))

    def on_report(self, msg: Message) -> None:
        """Fill the reporting node's slot and queue the refinement.

        Args:
            msg (Message): the published report (its `source` selects the slot).
        """
        field = SLOTS.get(msg.source)
        if field is None:
            logger.warning("no prompt slot for source %r (known: %s); ignoring the report",
                           msg.source, ", ".join(SLOTS))
            return
        self._slots[field] = msg.data
        self._count += 1
        index = self._count
        slots = dict(self._slots)          # snapshot: this report's view of the drive
        self._worker.submit(f"layer3 report {index}",
                            lambda: self._narrate(index, msg, slots))

    def _narrate(self, index: int, msg: Message, slots: dict) -> None:
        """Run the subscriber LLM, write `<stem>.txt`, speak and log the event."""
        started = time.perf_counter()
        context, narration = self._refine(msg, slots)
        infer_s = time.perf_counter() - started
        if not narration:
            logger.warning("layer 3: empty narration for report %d; nothing saved", index)
            return
        logger.info("LAYER 3 (%.2fs): %s", infer_s, narration)

        received = "\n".join(f"- {field}: {value}" for field, value in slots.items())
        stem = f"report_{index:02d}_{msg.source}"
        paths = self._run.save(
            LAYER, stem,
            f"PUBLISHED MESSAGE ({msg.node} / {msg.source})\n{msg.data}\n\n"
            f"SUBSCRIBER SLOTS AT THIS MOMENT\n{received}\n\n"
            f"REFINED CONTEXT\n{context}\n\nNARRATION\n{narration}",
            {"layer": "3 - single LLM subscriber over the 3 publisher nodes",
             "drive_time_s": f"{msg.stamp:.1f}",
             "node": msg.node,
             "source": msg.source,
             "topic": msg.topic,
             "model": self._model,
             "inference_s": f"{infer_s:.2f}"})
        self._memory.append(narration)
        speech_s = self._speaker.say(narration, paths["wav"])
        self._run.log({"layer": LAYER, "event": msg.source, "drive_time_s": msg.stamp,
                       "node": msg.node, "topic": msg.topic, "message": msg.data,
                       "model": self._model, "refined_context": context,
                       "narration": narration, "infer_s": round(infer_s, 3),
                       "speech_s": round(speech_s, 3), **paths})

    def _refine(self, msg: Message, slots: dict) -> tuple[str, str]:
        """Return (refined_context, narration) from the LLM, or the raw report in dry-run mode."""
        if self._agent is None:
            return f"(dry run) {msg.node} ({msg.source}) reported: {msg.data}", msg.data
        out = self._agent.invoke(SubscriberInput(**slots))
        return out.refined_context.strip(), out.narration.strip()
