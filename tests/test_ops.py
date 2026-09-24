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


def test_the_socket_outlasts_the_block_it_waits_on():
    """A BRPOP that blocks longer than the client will wait to READ it fails
    every single time. Raising the block to 30s while the socket read timeout
    stayed at 10 did exactly that in production: every dequeue raised "Timeout
    reading from socket", jobs only moved when the reaper re-pushed them, and
    owners waited minutes for a reply. The two are set together or not at all."""
    from app.queue import client

    assert client.SOCKET_TIMEOUT > client.DEQUEUE_BLOCK, (
        f"socket read timeout {client.SOCKET_TIMEOUT}s must outlast the "
        f"{client.DEQUEUE_BLOCK}s block, with room for the round trip"
    )


def test_the_worker_blocks_for_exactly_what_the_client_is_built_for():
    """One number, one place. The worker used to keep its own copy, which is
    how it drifted away from the socket timeout in the first place."""
    from app.queue import client, worker

    assert worker.DEQUEUE_BLOCK is client.DEQUEUE_BLOCK
