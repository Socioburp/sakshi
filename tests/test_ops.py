# --------------------------------------------------------------------------- #
# the idle queue must not spend the Redis allowance
# --------------------------------------------------------------------------- #
def test_the_idle_loop_does_not_spend_the_redis_allowance():
    """A managed Redis bills by the request. Promoting once a second plus a
    five-second BRPOP is over 100,000 calls a day on an EMPTY queue, which ran
    a 500,000-request allowance out in five days and stopped every client's
    messages being answered. Neither number bought anything."""
    from app.queue import worker

    a_day = 24 * 60 * 60
    promotes = a_day / worker.PROMOTE_EVERY
    blocks = a_day / worker.DEQUEUE_BLOCK

    assert promotes + blocks < 15_000, (
        f"idle Redis usage is {promotes + blocks:.0f} requests a day; "
        "that is what exhausted the plan"
    )


async def test_a_redis_that_keeps_refusing_is_asked_less_often(monkeypatch):
    """When the allowance ran out the loop retried every two seconds, holding
    the account at its limit and burying the cause in the log. Each failure in
    a row must wait longer than the one before."""
    from app.queue import worker

    slept: list[float] = []

    def refuse(timeout=None):
        raise RuntimeError("max requests limit exceeded")

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(worker, "dequeue", refuse)
    monkeypatch.setattr(worker.asyncio, "sleep", record)
    monkeypatch.setattr(worker, "_dequeue_failures", 0)

    for _ in range(6):
        assert await worker.run_once() is False

    assert slept == sorted(slept), "each wait is at least as long as the last"
    assert slept[-1] > slept[0], "the wait actually grows"
    assert slept[-1] <= worker.RETRY_MAX_S, "but it is capped"


async def test_one_good_dequeue_forgets_the_bad_ones(monkeypatch):
    """A blip must not leave the worker crawling once Redis is back."""
    from app.queue import worker

    monkeypatch.setattr(worker, "_dequeue_failures", 4)
    monkeypatch.setattr(worker, "dequeue", lambda timeout=None: None)

    assert await worker.run_once() is False
    assert worker._dequeue_failures == 0
