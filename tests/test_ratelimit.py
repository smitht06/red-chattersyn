import asyncio
import time

from chattersyn.core.ratelimit import MinInterval, TokenBucket


def test_bucket_initial_and_exhaustion():
    bucket = TokenBucket(20, 20 / 30)
    assert bucket.try_acquire(20)
    assert not bucket.try_acquire(1)


def test_bucket_refills():
    bucket = TokenBucket(1, 1.0)
    bucket.try_acquire(1)
    bucket._last -= 1.5  # simulate 1.5s of refill
    assert bucket.try_acquire(1)


async def test_acquire_waits_for_refill():
    bucket = TokenBucket(1, 2.0)  # 1 token per 0.5s
    bucket.try_acquire(1)
    start = time.monotonic()
    await asyncio.wait_for(bucket.acquire(1), timeout=1.0)
    assert time.monotonic() - start >= 0.45


async def test_min_interval_spacing():
    interval = MinInterval(0.1)
    await interval.wait()
    start = time.monotonic()
    await interval.wait()
    assert time.monotonic() - start >= 0.09
