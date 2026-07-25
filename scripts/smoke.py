"""Offline smoke tests for the parsing/merge logic (no network needed).

Run:  python3 scripts/smoke.py
Exits non-zero if any assertion fails.
"""
import os
import sys
from datetime import datetime

ADDON_DIR = os.path.join(os.path.dirname(__file__), "..", "warsaw_transport")
sys.path.insert(0, ADDON_DIR)

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


def test_card_install_detection():
    """Settings.card_installed must reflect the file on disk, not just the env var."""
    from app.config import load_settings  # noqa: PLC0415 - env is set per-case below

    saved = {k: os.environ.get(k) for k in ("WT_CARD_PATH", "WT_WWW_CREATED")}
    try:
        os.environ["WT_CARD_PATH"] = ""
        check(not load_settings().card_installed, "no card path -> not installed")

        os.environ["WT_CARD_PATH"] = "/nonexistent/warsaw-transport-card.js"
        check(
            not load_settings().card_installed,
            "card path pointing at a missing file -> not installed",
        )

        os.environ["WT_CARD_PATH"] = os.path.join(
            ADDON_DIR, "lovelace", "warsaw-transport-card.js"
        )
        check(load_settings().card_installed, "card path pointing at a real file -> installed")

        os.environ["WT_WWW_CREATED"] = "true"
        check(load_settings().www_created, "WT_WWW_CREATED=true is parsed")
        os.environ["WT_WWW_CREATED"] = "false"
        check(not load_settings().www_created, "WT_WWW_CREATED=false is parsed")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_lovelace_card_shipped():
    """The card is plain JS with no build step, so just check it ships intact."""
    card_path = os.path.join(ADDON_DIR, "lovelace", "warsaw-transport-card.js")
    check(os.path.isfile(card_path), "lovelace card file exists")
    with open(card_path, encoding="utf-8") as fh:
        card = fh.read()
    for snippet, msg in [
        ('customElements.define("warsaw-transport-card"', "card element is registered"),
        ('customElements.define("warsaw-transport-card-editor"', "GUI editor is registered"),
        ("window.customCards.push", "card is listed in the dashboard card picker"),
        ("getConfigElement", "card exposes the GUI editor"),
        ("attributes.departures", "card reads the sensor's departures attribute"),
    ]:
        check(snippet in card, msg)

    with open(os.path.join(ADDON_DIR, "Dockerfile"), encoding="utf-8") as fh:
        dockerfile = fh.read()
    check("COPY lovelace/" in dockerfile, "Dockerfile ships the lovelace folder")

    with open(os.path.join(ADDON_DIR, "run.sh"), encoding="utf-8") as fh:
        run_sh = fh.read()
    check("www/warsaw_transport" in run_sh, "run.sh installs the card into the HA www folder")
    # A directory that merely exists is not the HA config folder: without this
    # marker check the copy can silently land inside the container.
    check(
        "configuration.yaml" in run_sh,
        "run.sh identifies the HA config folder by configuration.yaml",
    )
    check(
        'if [ -s "${CARD_DEST}" ]' in run_sh,
        "run.sh verifies the copied card exists on disk before reporting success",
    )
    check(
        "WT_WWW_CREATED" in run_sh,
        "run.sh reports whether it had to create the www folder",
    )

    with open(os.path.join(ADDON_DIR, "DOCS.md"), encoding="utf-8") as fh:
        docs = fh.read()
    check(
        "Restart Home Assistant Core once" in docs,
        "DOCS.md requires the Home Assistant Core restart",
    )
    check(
        "Custom element not found" in docs,
        "DOCS.md troubleshoots the 404 / missing-element symptom",
    )

    with open(os.path.join(ADDON_DIR, "config.yaml"), encoding="utf-8") as fh:
        config_yaml = fh.read()
    check(
        "homeassistant_config:rw" in config_yaml,
        "config.yaml maps the HA config folder read-write",
    )


if __name__ == "__main__":
    test_flatten()
    test_stop_matching()
    test_parse_czas()
    test_build_departures()
    test_overlay_gps()
    test_overlay_gps_mixed_feeds()
    test_lovelace_card_shipped()
    test_card_install_detection()
    print("\nAll smoke tests passed.")
