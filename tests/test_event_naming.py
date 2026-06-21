"""Tests for the unified single/multi-day event folder naming in _event_subdir."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from photo_organizer.planner import _event_subdir


def test_single_day_naming_uses_space():
    dt = datetime(2012, 9, 8, 10, 0, 0)
    sub = _event_subdir(Path("D:/Media/Masters"), dt, "上海", None)
    assert str(sub) == str(Path("D:/Media/Masters/2012/2012-09-08 上海"))


def test_single_day_no_event_is_date_only():
    dt = datetime(2012, 9, 8, 10, 0, 0)
    sub = _event_subdir(Path("D:/Media/Masters"), dt, "", None)
    assert str(sub) == str(Path("D:/Media/Masters/2012/2012-09-08"))


def test_multiday_naming_paren_span():
    dt = datetime(2005, 8, 8, 10, 0, 0)
    group = {"kind": "event", "start": date(2005, 8, 6), "span": 9}
    sub = _event_subdir(Path("D:/Media/Masters"), dt, "蒙古", group)
    assert str(sub) == str(Path("D:/Media/Masters/2005/2005-08-06(9d) 蒙古"))


def test_multiday_no_event_is_date_span_only():
    dt = datetime(2005, 8, 8, 10, 0, 0)
    group = {"kind": "event", "start": date(2005, 8, 6), "span": 9}
    sub = _event_subdir(Path("D:/Media/Masters"), dt, "", group)
    assert str(sub) == str(Path("D:/Media/Masters/2005/2005-08-06(9d)"))


def test_subject_naming_unchanged():
    dt = datetime(2012, 9, 8, 10, 0, 0)
    group = {"kind": "subject", "label": "愷"}
    sub = _event_subdir(Path("D:/Media/Masters"), dt, "愷", group)
    assert str(sub) == str(Path("D:/Media/Masters/愷/2012"))
