"""The reapers and the slowest legitimate job, held in the right order.

A job re-queued while it is still running is run TWICE, and a creative job
charges. A creative refunded while its job may still re-run is a free
creative. Both hazards are a matter of three numbers staying in order, and
they moved the day generation went from seconds to minutes -- so the order is
asserted, not remembered.
"""

from __future__ import annotations

import math
from datetime import timedelta

from app.config import Settings
from app.creative import pipeline
from app.creative.imagegen import providers as P
from app.queue import client


def _worst_case_job() -> timedelta:
    """Six slides, every one using every attempt the dollar cap allows."""
    s = Settings()
    per_call_s = 120 + 20  # OpenAI: "up to 2 minutes"; plus the inspection call
    attempts = s.imagegen_gate_attempts
    if s.imagegen_gate_budget_micros:
        attempts = min(attempts, s.imagegen_gate_budget_micros // P.DEFAULT_COST_MICROS["openai"])
    waves = math.ceil(6 / max(1, s.imagegen_concurrency))
    return timedelta(seconds=waves * attempts * per_call_s + 60)  # + compose, upload, send


def test_a_slow_healthy_job_is_never_requeued_and_run_twice():
    assert client.STALE_RUNNING > _worst_case_job(), (
        "the job reaper would re-run (and re-charge) a carousel that is merely slow"
    )


def test_a_creative_is_never_refunded_while_its_job_may_still_rerun():
    assert pipeline.STUCK_AFTER > client.STALE_RUNNING


def test_the_vendor_stall_guard_outlasts_the_vendors_own_worst_case():
    assert P.OpenAIProvider.BUDGET_S >= 300 > 120
    assert P.OpenAIProvider.TIMEOUT.read >= 300


def test_the_dollar_cap_allows_honest_retries_but_not_unbounded_ones():
    s = Settings()
    allowed = s.imagegen_gate_budget_micros // P.DEFAULT_COST_MICROS["openai"]
    assert 2 <= allowed <= s.imagegen_gate_attempts
