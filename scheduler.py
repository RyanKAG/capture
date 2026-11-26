"""Run a synchronous job on an elapsed-time or local wall-clock schedule."""

import math
import re
import time
from configparser import ConfigParser
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time
from itertools import islice
from typing import Callable, Iterator, Optional, Tuple, Union


@dataclass(frozen=True)
class IntervalSchedule:
    interval_minutes: float


@dataclass(frozen=True)
class DailySchedule:
    daily_times: Tuple[wall_time, ...]


Schedule = Union[IntervalSchedule, DailySchedule]


def load_schedule(config: ConfigParser) -> Schedule:
    mode = config.get("schedule", "mode", fallback="").strip().lower()
    if mode == "interval":
        try:
            minutes = config.getfloat("schedule", "interval_minutes", fallback=0.0)
        except ValueError as exc:
            raise ValueError("interval_minutes must be a positive number.") from exc
        if minutes <= 0 or not math.isfinite(minutes * 60):
            raise ValueError("interval_minutes must be a positive, finite duration.")
        return IntervalSchedule(minutes)

    if mode == "daily":
        raw_times = config.get("schedule", "daily_times", fallback="")
        times = set()
        for item in raw_times.split(","):
            value = item.strip()
            if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
                raise ValueError("daily_times must contain local HH:MM times, e.g. 09:00, 17:30.")
            times.add(wall_time.fromisoformat(value))
        return DailySchedule(tuple(sorted(times)))

    raise ValueError("Set [schedule] mode to interval or daily.")


def _scheduled_ticks(schedule: Schedule) -> Iterator[None]:
    if isinstance(schedule, IntervalSchedule):
        period = schedule.interval_minutes * 60
        due = time.monotonic()
        while True:
            now = time.monotonic()
            if now < due:
                time.sleep(min(due - now, 60))
                continue
            due = now + period
            yield
            finished = time.monotonic()
            if finished > due:
                # An overrun skips missed captures and starts a fresh interval.
                due = finished + period

    else:
        last_dates = {}
        while True:
            now = datetime.now()
            slot = now.time().replace(second=0, microsecond=0)
            today = now.date()
            if slot in schedule.daily_times and today > last_dates.get(slot, date.min):
                last_dates[slot] = today
                yield
                continue
            # Recheck the local clock after sleep, DST changes, or a manual adjustment.
            seconds_to_next_minute = 60 - now.second - now.microsecond / 1_000_000
            time.sleep(min(30, seconds_to_next_minute))


def run_schedule(schedule: Schedule, job: Callable[[], None], *, max_runs: Optional[int] = None) -> None:
    """Run jobs sequentially until interrupted or the requested run count is reached."""
    for _ in islice(_scheduled_ticks(schedule), max_runs):
        job()
