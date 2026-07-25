"""Offline smoke tests for the parsing/merge logic (no network needed).

Run:  python3 scripts/smoke.py
Exits non-zero if any assertion fails.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "warsaw_transport"))

from app.departures import build_departures, overlay_gps, parse_czas  # noqa: E402
from app.warsaw_api import _flatten, match_stops, normalize  # noqa: E402


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}")


def test_flatten():
    # Stops / lines: rows wrapped in "values".
    row = {"values": [{"key": "czas", "value": "14:30:00"}, {"key": "linia", "value": "523"}]}
    out = _flatten(row)
    check(out == {"czas": "14:30:00", "linia": "523"}, "flatten key/value rows")

    # Timetables: a bare list of pairs, with no "values" wrapper.
    bare = [
        {"value": None, "key": "symbol_1"},
        {"value": "3", "key": "brygada"},
        {"value": "Żerań FSO", "key": "kierunek"},
        {"value": "12:06:00", "key": "czas"},
    ]
    out = _flatten(bare)
    check(
        out == {"symbol_1": None, "brygada": "3", "kierunek": "Żerań FSO", "czas": "12:06:00"},
        "flatten bare pair-list rows (timetable shape)",
    )

    # Vehicles: already flat, passed through untouched.
    veh = {"Lines": "119", "Brigade": "4", "Lat": 52.19, "Lon": 21.11}
    check(_flatten(veh) == veh, "flatten leaves already-flat vehicle rows alone")


def test_stop_matching():
    check(normalize("Żerań FSO") == "zeran fso", "normalize strips Polish diacritics")
    check(normalize("Marszałkowska") == "marszalkowska", "normalize maps ł -> l")

    rows = [
        {"zespol": "1001", "slupek": "01", "nazwa_zespolu": "Żerań FSO"},
        {"zespol": "7009", "slupek": "02", "nazwa_zespolu": "Marszałkowska"},
        {"zespol": "3254", "slupek": "01", "nazwa_zespolu": "PKP Służewiec"},
    ]
    check(
        [r["zespol"] for r in match_stops(rows, "zeran")] == ["1001"],
        "search without diacritics finds Żerań",
    )
    check(
        [r["zespol"] for r in match_stops(rows, "MARSZAŁKOWSKA")] == ["7009"],
        "search is case-insensitive and accepts diacritics",
    )
    check(
        [r["zespol"] for r in match_stops(rows, "sluzewiec")] == ["3254"],
        "search matches a substring, not just a prefix",
    )
    check(match_stops(rows, "nowhere") == [], "no matches for an unknown name")
    check(match_stops(rows, "  ") == [], "blank query returns nothing")


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


def test_overlay_gps_mixed_feeds():
    """Bus and tram snapshots are concatenated before overlaying."""
    deps = [
        {"line": "20", "brigade": "3", "live": False, "lat": None, "lon": None},   # tram
        {"line": "523", "brigade": "2", "live": False, "lat": None, "lon": None},  # bus
        {"line": "L-8", "brigade": "M1", "live": False, "lat": None, "lon": None},
    ]
    buses = [{"Lines": "523", "Brigade": "2", "Lat": 52.2, "Lon": 21.0, "VehicleNumber": "1234"}]
    trams = [{"Lines": "20", "Brigade": "3", "Lat": 52.3, "Lon": 21.1, "VehicleNumber": "3001"}]
    overlay_gps(deps, buses + trams)
    check(deps[0]["live"] is True and deps[0]["lat"] == 52.3, "tram departure matched from tram feed")
    check(deps[1]["live"] is True and deps[1]["lat"] == 52.2, "bus departure matched from bus feed")
    check(deps[2]["live"] is False, "non-numeric brigade with no vehicle stays non-live")


if __name__ == "__main__":
    test_flatten()
    test_stop_matching()
    test_parse_czas()
    test_build_departures()
    test_overlay_gps()
    test_overlay_gps_mixed_feeds()
    print("\nAll smoke tests passed.")
