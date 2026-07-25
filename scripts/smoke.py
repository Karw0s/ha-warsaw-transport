"""Offline smoke tests for the parsing/merge logic (no network needed).

Run:  python3 scripts/smoke.py
Exits non-zero if any assertion fails.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "warsaw_transport"))

from app.departures import build_departures, overlay_gps, parse_czas  # noqa: E402
from app.warsaw_api import _flatten  # noqa: E402


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}")


def test_flatten():
    row = {"values": [{"key": "czas", "value": "14:30:00"}, {"key": "linia", "value": "523"}]}
    out = _flatten(row)
    check(out == {"czas": "14:30:00", "linia": "523"}, "flatten key/value rows")


def test_parse_czas():
    now = datetime(2026, 7, 25, 14, 0, 0)
    # normal future time
    d = parse_czas("14:30:00", now)
    check(d == datetime(2026, 7, 25, 14, 30), "parse normal HH:MM:SS")
    # after-midnight 25:14 -> next day 01:14
    d2 = parse_czas("25:14:00", now)
    check(d2 == datetime(2026, 7, 26, 1, 14), "parse >24h rollover")
    # HH:MM without seconds
    d3 = parse_czas("15:05", now)
    check(d3 == datetime(2026, 7, 25, 15, 5), "parse HH:MM without seconds")
    # garbage
    check(parse_czas("nope", now) is None, "reject non-time string")


def test_build_departures():
    now = datetime(2026, 7, 25, 14, 0, 0)
    timetables = {
        "523": [
            {"czas": "13:55:00", "kierunek": "Marymont", "brygada": "1"},  # past -> dropped
            {"czas": "14:05:00", "kierunek": "Marymont", "brygada": "2"},
            {"czas": "14:40:00", "kierunek": "Marymont", "brygada": "3"},
        ],
        "131": [
            {"czas": "14:10:00", "kierunek": "Dw. Centralny", "brygada": "7"},
            {"czas": "25:14:00", "kierunek": "Dw. Centralny", "brygada": "8"},
        ],
    }
    deps = build_departures(timetables, now, limit=5)
    times = [d["time"] for d in deps]
    check(times == ["14:05", "14:10", "14:40", "01:14"], f"merged+sorted times: {times}")
    check(deps[0]["minutes"] == 5, "minutes computed for first departure")
    check(all(d["live"] is False for d in deps), "no live flag before overlay")


def test_overlay_gps():
    deps = [
        {"line": "523", "brigade": "2", "live": False, "lat": None, "lon": None},
        {"line": "131", "brigade": "7", "live": False, "lat": None, "lon": None},
    ]
    vehicles = [
        {"Lines": "523", "Brigade": "2", "Lat": 52.2, "Lon": 21.0, "VehicleNumber": "1234"},
    ]
    overlay_gps(deps, vehicles)
    check(deps[0]["live"] is True and deps[0]["lat"] == 52.2, "GPS overlay matches line+brigade")
    check(deps[1]["live"] is False, "unmatched departure stays non-live")


if __name__ == "__main__":
    test_flatten()
    test_parse_czas()
    test_build_departures()
    test_overlay_gps()
    print("\nAll smoke tests passed.")
