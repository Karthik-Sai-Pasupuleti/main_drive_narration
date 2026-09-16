"""One background thread that runs narration jobs strictly one at a time.

A VLM call plus its speech takes seconds, and the timeline has to keep its
timestamps, so the layers never narrate on the thread that detected the event:
they submit a job here. Serial execution also keeps two layers from talking over
each other - the second line starts only once the first has finished speaking.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class SerialWorker:
    """FIFO job queue drained by a single daemon thread."""

    def __init__(self, maxsize: int = 16) -> None:
        """Start the worker thread.

        Args:
            maxsize (int, optional): queue depth; submissions beyond it are dropped
                with a warning rather than stalling the timeline.
        """
        self._jobs: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread = threading.Thread(target=self._run, name="narration", daemon=True)
        self._thread.start()

    def submit(self, label: str, job: Callable[[], None]) -> bool:
        """Queue `job`; return False if the queue was full and it was dropped.

        Args:
            label (str): short name used in the log when the job fails or is dropped.
            job (Callable[[], None]): the work to run on the worker thread.

        Returns:
            bool: True when the job was queued.
        """
        try:
            self._jobs.put_nowait((label, job))
            return True
        except queue.Full:
            logger.warning("narration queue full; dropping %s", label)
            return False

    def _run(self) -> None:
        """Drain the queue until a None sentinel arrives."""
        while True:
            item = self._jobs.get()
            if item is None:
                return
            label, job = item
            try:
                job()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.error("%s failed: %s", label, exc)

    def close(self, timeout: float = 120.0) -> None:
        """Let the queued jobs finish, then stop the thread.

        Args:
            timeout (float, optional): seconds to wait for the backlog to drain.
        """
        self._jobs.put(None)
        self._thread.join(timeout=timeout)
