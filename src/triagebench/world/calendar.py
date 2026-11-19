"""Business calendar: weekends and bank holidays."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta

from triagebench.config import BANK_HOLIDAYS


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in BANK_HOLIDAYS


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def business_days(start: date, end: date) -> list[date]:
    return [d for d in date_range(start, end) if is_business_day(d)]


def date_range(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def next_business_day(d: date) -> date:
    n = d + timedelta(days=1)
    while not is_business_day(n):
        n += timedelta(days=1)
    return n


def prev_business_day(d: date) -> date:
    p = d - timedelta(days=1)
    while not is_business_day(p):
        p -= timedelta(days=1)
    return p


def prev_weekday(d: date) -> date:
    p = d - timedelta(days=1)
    while not is_weekday(p):
        p -= timedelta(days=1)
    return p


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def bureau_delivery_day(d: date) -> date:
    """The bureau refresh is due on Monday, or the next business day if Monday is a holiday."""
    m = week_monday(d)
    return m if is_business_day(m) else next_business_day(m)


def is_bureau_day(d: date) -> bool:
    return is_business_day(d) and bureau_delivery_day(d) == d


def is_first_business_day_of_week(d: date) -> bool:
    return is_business_day(d) and bureau_delivery_day(d) == d


def run_time_for(ds: date) -> datetime:
    """The daily DAG for partition ds runs at 06:00 on the next calendar day."""
    return datetime.combine(ds + timedelta(days=1), time(6, 0))


def check_time_for(ds: date) -> datetime:
    return datetime.combine(ds + timedelta(days=1), time(8, 30))


def fmt_ts(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
