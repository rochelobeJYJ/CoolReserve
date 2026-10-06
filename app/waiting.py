"""Wait for an accepted observation to remain unchanged; never repeat an action."""
from __future__ import annotations

import time


def stable_observation(observe, accept, *, timeout=5, interval=.1, samples=2,
                       checkpoint=lambda: None, wait=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    previous, count = object(), 0
    while True:
        checkpoint()
        value = observe()
        if accept(value):
            count = count + 1 if value == previous else 1
            if count >= samples:
                return value
        else:
            count = 0
        previous = value
        if clock() >= deadline:
            raise TimeoutError('화면 결과가 안정적으로 확인되지 않았습니다.')
        wait(min(interval, max(0, deadline - clock())))
