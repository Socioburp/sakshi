"""Per-stage latency timers.

A WhatsApp turn that takes 40 seconds is the product's main failure mode, and
"it was slow" is not debuggable. Every stage is timed and written to
`stage_timings` with a shared trace id, so you can answer "was it STT, the
model, the image provider, or Chromium?" from SQL.

    with trace("abc123", account_id) as t:
        with t.stage("stt"): ...
        with t.stage("agent"): ...
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

from app.db.models import StageTiming
from app.db.session import session_scope
from app.logging import get_logger

log = get_logger(__name__)


@dataclass
class Trace:
    trace_id: str
    account_id: uuid.UUID | None = None
    timings: dict[str, int] = field(default_factory=dict)
    _rows: list[StageTiming] = field(default_factory=list)

    @contextmanager
    def stage(self, name: str, **meta):
        t0 = time.perf_counter()
        ok = True
        try:
            yield
        except Exception as exc:  # noqa: BLE001
            ok = False
            meta["error"] = str(exc)[:500]
            raise
        finally:
            ms = int((time.perf_counter() - t0) * 1000)
            self.timings[name] = ms
            self._rows.append(
                StageTiming(
                    trace_id=self.trace_id,
                    account_id=self.account_id,
                    stage=name,
                    ms=ms,
                    ok=ok,
                    meta=meta,
                )
            )
            log.info("stage", trace_id=self.trace_id, stage=name, ms=ms, ok=ok)

    def total_ms(self) -> int:
        return sum(self.timings.values())

    def flush(self) -> None:
        if not self._rows:
            return
        try:
            with session_scope() as db:
                db.add_all(self._rows)
        except Exception:  # noqa: BLE001
            log.warning("stage_flush_failed", trace_id=self.trace_id)
        finally:
            self._rows = []


@contextmanager
def trace(trace_id: str | None = None, account_id: uuid.UUID | None = None):
    t = Trace(trace_id=trace_id or uuid.uuid4().hex[:16], account_id=account_id)
    try:
        yield t
    finally:
        t.flush()
        log.info("trace_done", trace_id=t.trace_id, total_ms=t.total_ms(), stages=t.timings)
