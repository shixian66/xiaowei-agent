"""可手动推进的时钟；使租约过期测试无需 sleep。"""

import datetime as dt


class ManualClock:
    def __init__(self, start: dt.datetime | None = None) -> None:
        self._now = start or dt.datetime(2026, 9, 2, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self._now

    def advance(self, *, seconds: int) -> None:
        self._now += dt.timedelta(seconds=seconds)
