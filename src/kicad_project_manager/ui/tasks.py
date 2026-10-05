"""Run blocking work (git, network) off the UI thread."""

from __future__ import annotations

import itertools
import logging
import traceback
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

log = logging.getLogger(__name__)


class _Signals(QObject):
    finished = Signal(int, bool, object)  # task id, success, result or error


class _Task(QRunnable):
    def __init__(self, task_id: int, fn: Callable[[], object], signals: _Signals):
        super().__init__()
        self.task_id = task_id
        self.fn = fn
        self.signals = signals

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as e:  # noqa: BLE001 - reported to the UI
            log.debug("Task failed:\n%s", traceback.format_exc())
            self.signals.finished.emit(self.task_id, False, e)
        else:
            self.signals.finished.emit(self.task_id, True, result)


class TaskRunner(QObject):
    """Runs callables on a thread pool and calls back on the UI thread.

    ``busy_changed`` reports the label of the most recent visible task, or an
    empty string when nothing visible is running. Background tasks
    (``label=None``) don't show up there.
    """

    busy_changed = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(4, self.pool.maxThreadCount()))
        self._signals = _Signals(self)
        self._signals.finished.connect(self._on_finished)
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[Callable | None, Callable | None, str | None]] = {}

    def run(
        self,
        fn: Callable[[], object],
        on_done: Callable[[object], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        label: str | None = None,
    ) -> None:
        task_id = next(self._ids)
        self._pending[task_id] = (on_done, on_error, label)
        if label:
            self.busy_changed.emit(label)
        self.pool.start(_Task(task_id, fn, self._signals))

    def busy_labels(self) -> list[str]:
        return [label for _, _, label in self._pending.values() if label]

    @Slot(int, bool, object)
    def _on_finished(self, task_id: int, success: bool, payload: object) -> None:
        on_done, on_error, label = self._pending.pop(task_id)
        if label:
            labels = self.busy_labels()
            self.busy_changed.emit(labels[-1] if labels else "")
        if success:
            if on_done:
                on_done(payload)
        elif on_error:
            on_error(payload)  # type: ignore[arg-type]
        else:
            log.warning("Background task failed: %s", payload)
