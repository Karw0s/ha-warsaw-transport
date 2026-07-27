"""Offline smoke tests for the parsing/merge logic (no network needed).

Run:  python3 scripts/smoke.py
Exits non-zero if any assertion fails.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

ADDON_DIR = os.path.join(os.path.dirname(__file__), "..", "warsaw_transport")
sys.path.insert(0, ADDON_DIR)

from app.cache import DailyCache, service_day  # noqa: E402
from app.departures import (  # noqa: E402
    build_departures,
    next_departures,
    overlay_gps,
    parse_czas,
    vehicle_types_for_stops,
)
from app.eta import VehicleTracker, haversine_m, is_live, parse_fix_time  # noqa: E402
from app.routes import (  # noqa: E402
    MAX_ROUTE_RESIDUAL_M,
    NullRoutesProvider,
    RouteCatalog,
    build_provider,
    build_track,
    dump_routes,
    load_routes,
    normalize_routes,
)
from app.warsaw_api import (  # noqa: E402
    EP_LINES,
    EP_STOPS,
    EP_TIMETABLE,
    EP_VEHICLES,
    WarsawApiError,
    _flatten,
    describe_call,
    match_stops,
    normalize,
    pole_variants,
    stop_coords,
    vehicle_type_for_line,
    vehicle_types_for_lines,
)


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}")


def run(coro):
    """Run one coroutine to completion; the tests below are otherwise sync."""
    return asyncio.run(coro)


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
    check(
        all(d["eta_minutes"] is None and d["delay_minutes"] is None for d in deps),
        "ETA fields are present but empty before the GPS overlay",
    )


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


# --- live arrival estimates ------------------------------------------------

STOP_LAT, STOP_LON = 52.2, 21.0
STOP_KEY = "7009|01"
# One degree of latitude is ~111.2 km, so this converts metres into a position
# due north of the stop without needing a second real-world coordinate.
METRES_PER_DEGREE_LAT = 111_194.9


def _vehicle(metres_away, fix_time, line="190", brigade="2"):
    """A GPS row for a vehicle `metres_away` due north of the test stop."""
    return {
        "Lines": line,
        "Brigade": brigade,
        "Lat": STOP_LAT + metres_away / METRES_PER_DEGREE_LAT,
        "Lon": STOP_LON,
        "VehicleNumber": "1234",
        "Time": fix_time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def test_haversine():
    north = STOP_LAT + 1000 / METRES_PER_DEGREE_LAT
    d = haversine_m(STOP_LAT, STOP_LON, north, STOP_LON)
    check(abs(d - 1000) < 2, f"1 km due north measures as 1 km (got {d:.1f} m)")
    check(haversine_m(STOP_LAT, STOP_LON, STOP_LAT, STOP_LON) == 0.0, "same point is 0 m")

    # Metro Politechnika to Metro Centrum is ~1.5 km along Marszałkowska.
    real = haversine_m(52.21945, 21.01128, 52.23096, 21.01180)
    check(1200 < real < 1400, f"two real Warsaw stops are ~1.3 km apart (got {real:.0f} m)")


def test_parse_fix_time():
    check(
        parse_fix_time("2026-07-25 17:12:33") == datetime(2026, 7, 25, 17, 12, 33),
        "parse the vehicle Time format",
    )
    check(parse_fix_time("") is None, "empty fix time is rejected")
    check(parse_fix_time("nonsense") is None, "unparseable fix time is rejected")


def test_is_live_needs_a_recent_fix():
    now = datetime(2026, 7, 25, 14, 0, 0)
    check(is_live(_vehicle(500, now), now), "a fresh fix counts as live")
    check(
        is_live(_vehicle(500, now - timedelta(seconds=90)), now),
        "a fix from a minute or two ago still counts as live",
    )
    check(
        not is_live(_vehicle(500, datetime(2024, 12, 2, 9, 0)), now),
        "a fix from a previous year is not live",
    )
    check(is_live({"Lines": "190", "Brigade": "2"}, now), "a missing fix time stays live")


def test_eta_from_closing_distance():
    """Two fixes give a real closing speed; one gives only a rough guess."""
    t0 = datetime(2026, 7, 25, 14, 0, 0)
    tracker = VehicleTracker()

    first = tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(1000, t0), now=t0)
    check(first["eta_source"] == "approx", "a first sighting is only an approximation")
    check(first["distance_m"] == 1000, f"distance to the stop is reported: {first['distance_m']}")
    # 1000 m * 1.3 detour / 5 m/s (18 km/h) = 260 s.
    check(first["eta_minutes"] == 4, f"default-speed ETA is ~4 min, got {first['eta_minutes']}")

    t1 = t0 + timedelta(seconds=60)
    scheduled = t1 + timedelta(seconds=60)
    second = tracker.estimate(
        STOP_KEY, STOP_LAT, STOP_LON, _vehicle(700, t1), scheduled=scheduled, now=t1
    )
    # Closed 300 m in 60 s = 5 m/s, so 700 m remain -> 140 s.
    check(second["eta_source"] == "tracked", "a second fix yields a measured closing speed")
    check(second["eta_minutes"] == 2, f"tracked ETA is 2 min, got {second['eta_minutes']}")
    check(second["eta_time"] == "14:03", f"arrival clock time, got {second['eta_time']}")
    check(second["delay_minutes"] == 1, f"1 min behind schedule, got {second['delay_minutes']}")
    check(second["eta_status"] == "ok", "a usable estimate reports status ok")


def test_eta_ignores_a_repeated_fix():
    """The GPS snapshot is cached, so the same fix is offered on several polls."""
    t0 = datetime(2026, 7, 25, 14, 0, 0)
    tracker = VehicleTracker()
    same = _vehicle(1000, t0)

    tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, same, now=t0)
    again = tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, same, now=t0 + timedelta(seconds=20))
    check(
        again["eta_source"] == "approx",
        "re-offering one fix does not invent a speed (would divide by zero elapsed time)",
    )

    later = tracker.estimate(
        STOP_KEY, STOP_LAT, STOP_LON, _vehicle(400, t0 + timedelta(seconds=60)),
        now=t0 + timedelta(seconds=60),
    )
    check(later["eta_source"] == "tracked", "a genuinely new fix does measure the speed")


def test_eta_suppressed_when_moving_away():
    """(line, brigade) is a trip: the same brigade runs back the other way."""
    t0 = datetime(2026, 7, 25, 14, 0, 0)
    tracker = VehicleTracker()
    tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(400, t0), now=t0)
    t1 = t0 + timedelta(seconds=60)
    away = tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(1400, t1), now=t1)
    check(away["eta_minutes"] is None, "a receding vehicle gets no ETA")
    check(away["eta_status"] == "moving_away", "the reason is reported")


def test_eta_withheld_for_a_parked_vehicle():
    """Seen at a terminus: distance unchanged for minutes, never seen moving."""
    t0 = datetime(2026, 7, 25, 14, 0, 0)
    tracker = VehicleTracker()
    parked = tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(2400, t0), now=t0)
    check(parked["eta_source"] == "approx", "a first sighting is still a rough guess")

    t1 = t0 + timedelta(seconds=60)
    still = tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(2400, t1), now=t1)
    check(still["eta_minutes"] is None, "a vehicle that has not moved gets no ETA")
    check(still["eta_status"] == "stalled", "the reason is reported")


def test_eta_survives_a_stop_at_a_light():
    """A vehicle that *was* moving keeps its ETA while it waits at a light."""
    t0 = datetime(2026, 7, 25, 14, 0, 0)
    tracker = VehicleTracker()
    tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(1000, t0), now=t0)
    t1 = t0 + timedelta(seconds=60)
    moving = tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(700, t1), now=t1)
    check(moving["eta_minutes"] == 2, f"closing 300 m/min from 700 m out: 2 min")

    # Then it stops dead for five minutes. Having been seen moving, it keeps an
    # ETA (unlike the parked case above); the estimate degrades as it sits, but
    # the speed floor keeps it bounded instead of running off to an hour.
    for step in range(2, 8):
        t = t0 + timedelta(seconds=60 * step)
        dwell = tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(700, t), now=t)
    check(dwell["eta_source"] == "tracked", "the last measured speed carries over")
    check(
        2 <= dwell["eta_minutes"] <= 10,
        f"a long dwell worsens the ETA but keeps it bounded: {dwell['eta_minutes']} min",
    )


def test_moving_away_does_not_flap():
    """A receding vehicle must stay receding, not reset to a default guess."""
    t0 = datetime(2026, 7, 25, 14, 0, 0)
    tracker = VehicleTracker()
    tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(100, t0), now=t0)
    t1 = t0 + timedelta(seconds=60)
    first = tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(600, t1), now=t1)
    t2 = t1 + timedelta(seconds=60)
    second = tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(900, t2), now=t2)
    check(first["eta_status"] == "moving_away", "the departing vehicle is spotted")
    check(second["eta_status"] == "moving_away", "and is still spotted on the next poll")


def test_eta_withheld_for_a_layover():
    """A vehicle sitting at the stop 40 min before its run is not 'arriving now'."""
    t0 = datetime(2026, 7, 25, 14, 0, 0)
    tracker = VehicleTracker()
    result = tracker.estimate(
        STOP_KEY, STOP_LAT, STOP_LON, _vehicle(40, t0),
        scheduled=t0 + timedelta(minutes=44), now=t0,
    )
    check(result["eta_minutes"] is None, "no ETA 44 min ahead of the timetable")
    check(result["eta_status"] == "waiting", "the reason is reported")

    soon = tracker.estimate(
        STOP_KEY, STOP_LAT, STOP_LON, _vehicle(40, t0),
        scheduled=t0 + timedelta(minutes=2), now=t0,
    )
    check(soon["eta_minutes"] == 0, "a vehicle at the stop just before its run arrives now")


def test_eta_rejects_unusable_fixes():
    now = datetime(2026, 7, 25, 14, 0, 0)
    tracker = VehicleTracker()

    stale = tracker.estimate(
        STOP_KEY, STOP_LAT, STOP_LON, _vehicle(500, datetime(2024, 12, 2, 9, 0)), now=now
    )
    check(stale["eta_status"] == "stale_fix", "a months-old fix produces no ETA")

    far = tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(30_000, now), now=now)
    check(far["eta_status"] == "too_far", "a vehicle 30 km away produces no ETA")

    nowhere = tracker.estimate(
        STOP_KEY, STOP_LAT, STOP_LON, {"Lines": "190", "Brigade": "2"}, now=now
    )
    check(nowhere["eta_status"] == "no_position", "a row without coordinates produces no ETA")


def test_eta_at_the_stop():
    now = datetime(2026, 7, 25, 14, 0, 0)
    tracker = VehicleTracker()
    arrived = tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(50, now), now=now)
    check(arrived["eta_minutes"] == 0, "a vehicle at the stop arrives now")


def test_tracker_prune():
    t0 = datetime(2026, 7, 25, 14, 0, 0)
    tracker = VehicleTracker()
    tracker.estimate(STOP_KEY, STOP_LAT, STOP_LON, _vehicle(500, t0), now=t0)
    check(tracker.prune(t0 + timedelta(minutes=5)) == 0, "a recent track is kept")
    check(tracker.prune(t0 + timedelta(hours=2)) == 1, "an idle track is forgotten")
    check(tracker._samples == {} and tracker._speed == {}, "pruning clears the history")


def test_overlay_adds_eta():
    """The overlay only estimates when it has the stop position and a tracker."""
    t0 = datetime(2026, 7, 25, 14, 0, 0)
    tracker = VehicleTracker()

    def deps(scheduled="14:05"):
        return build_departures(
            {"190": [{"czas": f"{scheduled}:00", "kierunek": "Ursynów", "brygada": "2"}]},
            t0,
        )

    plain = deps()
    overlay_gps(plain, [_vehicle(1000, t0)], now=t0)
    check(plain[0]["live"] is True, "the departure is still flagged live without a tracker")
    check(plain[0]["eta_minutes"] is None, "no ETA without the stop's coordinates")

    first = deps()
    overlay_gps(
        first, [_vehicle(1000, t0)],
        stop_location=(STOP_LAT, STOP_LON), tracker=tracker, stop_key=STOP_KEY, now=t0,
    )
    check(first[0]["eta_source"] == "approx", "the first overlay estimates approximately")

    t1 = t0 + timedelta(seconds=60)
    second = deps()
    overlay_gps(
        second, [_vehicle(700, t1)],
        stop_location=(STOP_LAT, STOP_LON), tracker=tracker, stop_key=STOP_KEY, now=t1,
    )
    check(second[0]["eta_source"] == "tracked", "the next overlay uses the measured speed")
    check(second[0]["eta_time"] == "14:03", f"ETA lands beside the schedule: {second[0]}")
    check(second[0]["delay_minutes"] == -2, "an early arrival shows a negative delay")


def test_overlay_claims_only_the_next_run():
    """A brigade comes back around; one vehicle must not light up both runs."""
    now = datetime(2026, 7, 25, 14, 0, 0)
    deps = build_departures(
        {
            "190": [
                {"czas": "14:05:00", "kierunek": "Ursynów", "brygada": "2"},
                {"czas": "15:20:00", "kierunek": "Ursynów", "brygada": "2"},
            ]
        },
        now,
    )
    overlay_gps(deps, [_vehicle(1000, now)], now=now)
    check(deps[0]["live"] is True, "the soonest run is matched to the vehicle")
    check(deps[1]["live"] is False, "the same brigade's later run is not")


def test_overlay_ignores_stale_vehicles():
    now = datetime(2026, 7, 25, 14, 0, 0)
    deps = build_departures(
        {"190": [{"czas": "14:05:00", "kierunek": "Ursynów", "brygada": "2"}]}, now
    )
    overlay_gps(deps, [_vehicle(1000, datetime(2024, 12, 2, 9, 0))], now=now)
    check(deps[0]["live"] is False, "a vehicle reporting a months-old fix is not live")


# --- route plans -----------------------------------------------------------
#
# `odleglosc` is the distance from the PREVIOUS stop, not from the start of the
# route as the published spec claims: in the live catalogue every route's first
# stop is 0, only 36% of variants are non-decreasing, and reading it cumulatively
# would make 44% of segments negative. Checked against stop coordinates each
# value is ~1.04x the straight line to the previous pole. The adapter therefore
# sums it, and these tests pin that down.

LEGACY_SAMPLE = {
    "result": {
        "1": {
            "TD-3BAN": {
                # Deliberately out of order, and with odleglosc in both the
                # string form the spec promises and the int form actually sent.
                "2": {"nr_zespolu": "3240", "nr_przystanku": "04", "typ": "5",
                      "odleglosc": "245"},
                "0": {"nr_zespolu": "R-03", "nr_przystanku": "00", "typ": "6",
                      "odleglosc": 0},
                "3": {"nr_zespolu": "3239", "nr_przystanku": "04", "typ": "1",
                      "odleglosc": 833},
                "1": {"nr_zespolu": "3241", "nr_przystanku": "02", "typ": "1",
                      "odleglosc": 400},
            }
        }
    }
}


def _straight_route(count=10, spacing=500.0):
    """A synthetic line of `count` stops running due north, `spacing` apart."""
    payload = {
        "190": {
            "TO-TEST": {
                str(i): {
                    "nr_zespolu": f"70{i:02d}", "nr_przystanku": "01",
                    "typ": "1", "odleglosc": 0 if i == 0 else spacing,
                }
                for i in range(count)
            }
        }
    }
    routes = normalize_routes(payload)
    locate = {
        (f"70{i:02d}", "01"): (STOP_LAT + (i * spacing) / METRES_PER_DEGREE_LAT, STOP_LON)
        for i in range(count)
    }
    return routes[("190", "TO-TEST")], locate


def _route_vehicle(metres, fix_time, line="190", brigade="2"):
    """A vehicle `metres` along the synthetic route."""
    return {
        "Lines": line, "Brigade": brigade,
        "Lat": STOP_LAT + metres / METRES_PER_DEGREE_LAT, "Lon": STOP_LON,
        "VehicleNumber": "1234",
        "Time": fix_time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def test_normalize_routes():
    catalogue = normalize_routes(LEGACY_SAMPLE)
    check(list(catalogue) == [("1", "TD-3BAN")], "route keyed by (line, route code)")
    route = catalogue[("1", "TD-3BAN")]
    check([s.order for s in route.stops] == [0, 1, 2, 3], "stops sorted by numeric order")
    check(
        [s.distance_m for s in route.stops] == [0.0, 400.0, 645.0, 1478.0],
        f"per-segment odleglosc summed into cumulative: {[s.distance_m for s in route.stops]}",
    )
    check(route.stops[2].segment_m == 245.0, "odleglosc sent as a string is parsed")
    check(route.length_m == 1478.0, "route length is the last cumulative value")
    check(route.stops[0].zespol == "R-03", "technical stops are kept, not dropped")
    check(route.index_of("3239", "04") == 3, "pole lookup finds its position")
    check(route.index_of("3239", "4") == 3, "pole lookup ignores zero padding")
    check(route.index_of("9999", "01") is None, "a pole not on the route is None")

    bare = normalize_routes(LEGACY_SAMPLE["result"])
    check(bare == catalogue, "the bare payload parses the same as the result envelope")


def test_routes_cache_roundtrip():
    catalogue = normalize_routes(LEGACY_SAMPLE)
    check(load_routes(dump_routes(catalogue)) == catalogue, "catalogue survives the cache encoding")
    check(load_routes("nonsense") == {}, "an unusable cache payload yields no routes")


def test_routes_provider_selection():
    check(build_provider("").name == "disabled", "no legacy key -> no provider")
    check(isinstance(build_provider(""), NullRoutesProvider), "the null provider is used")
    check(build_provider("secret").name == "legacy", "a legacy key selects the legacy provider")
    check(not RouteCatalog(build_provider("")).enabled, "catalogue reports itself disabled")
    check(
        run(RouteCatalog(build_provider("")).ensure()) == {},
        "a disabled catalogue fetches nothing",
    )


def test_route_projection():
    route, locate = _straight_route()
    track = build_track(route, "7008", "01", locate)
    check(track is not None, "track built for a stop on the route")
    check(track.target_index == 8 and track.target_m == 4000.0, "target stop located on the route")
    check(build_track(route, "9999", "01", locate) is None, "no track for a stop off the route")

    # Exactly on a stop, and half way between two.
    at_stop = track.project(STOP_LAT + 2000 / METRES_PER_DEGREE_LAT, STOP_LON)
    check(abs(at_stop.distance_m - 2000) < 1, f"projection at a stop: {at_stop.distance_m:.0f} m")
    between = track.project(STOP_LAT + 2250 / METRES_PER_DEGREE_LAT, STOP_LON)
    check(
        abs(between.distance_m - 2250) < 1,
        f"projection interpolates within a segment: {between.distance_m:.0f} m",
    )
    check(between.residual_m < 5, "a vehicle on the line has a small residual")
    check(track.stops_between(4) == 4, "stops still to serve before ours")

    # A vehicle well off the route (1 km to the side) must not be placed on it.
    aside = track.project(
        STOP_LAT + 2000 / METRES_PER_DEGREE_LAT,
        STOP_LON + 1000 / (METRES_PER_DEGREE_LAT * 0.61),
    )
    check(aside is None, f"a vehicle {MAX_ROUTE_RESIDUAL_M:.0f} m+ off the route is rejected")


def test_route_projection_on_a_loop():
    """An out-and-back route passes the same points twice; progress must not snap back."""
    # Out to 2000 m and back again, so every position has two candidate segments.
    payload = {"190": {"TO-LOOP": {}}}
    outward = [0, 500, 1000, 1500, 2000]
    positions = outward + [1500, 1000, 500, 0]
    for i, metres in enumerate(positions):
        payload["190"]["TO-LOOP"][str(i)] = {
            "nr_zespolu": f"80{i:02d}", "nr_przystanku": "01", "typ": "1",
            "odleglosc": 0 if i == 0 else abs(metres - positions[i - 1]),
        }
    route = normalize_routes(payload)[("190", "TO-LOOP")]
    locate = {
        (f"80{i:02d}", "01"): (STOP_LAT + m / METRES_PER_DEGREE_LAT, STOP_LON)
        for i, m in enumerate(positions)
    }
    # Our stop is the one on the *return* leg, 1000 m out (index 6).
    track = build_track(route, "8006", "01", locate)
    check(track.target_index == 6, "target is the return-leg stop")

    # 1500 m out lies on both legs; only the hint says which one the vehicle is on.
    here = STOP_LAT + 1500 / METRES_PER_DEGREE_LAT
    outbound = track.project(here, STOP_LON, 2)
    check(outbound.index < 4, f"with an outbound hint it stays outbound: index {outbound.index}")
    inbound = track.project(here, STOP_LON, 5)
    check(inbound.index >= 4, f"with a return hint it stays on the return leg: {inbound.index}")
    check(
        inbound.distance_m > outbound.distance_m,
        "the return leg is further along the route than the outbound one",
    )
    check(
        track.stops_between(outbound.index) > track.stops_between(inbound.index),
        "and therefore has more stops still to serve before ours",
    )


def test_route_eta_is_stable():
    """The payoff: a steady vehicle should hold one arrival time across polls."""
    route, locate = _straight_route()
    track = build_track(route, "7008", "01", locate)
    tracker = VehicleTracker()
    t0 = datetime(2026, 7, 25, 14, 0, 0)
    scheduled = t0 + timedelta(minutes=7)
    target_lat = STOP_LAT + 4000 / METRES_PER_DEGREE_LAT

    seen = []
    for step in range(6):
        t = t0 + timedelta(seconds=60 * step)
        seen.append(
            tracker.estimate(
                STOP_KEY, target_lat, STOP_LON, _route_vehicle(1000 + 500 * step, t),
                scheduled=scheduled, now=t, route_track=track,
            )
        )

    check(seen[0]["eta_source"] == "approx", "a first sighting still has no measured speed")
    # Sitting exactly at stop 2, it has *reached* that stop, so six remain.
    check(
        seen[0]["route_distance_m"] == 3000 and seen[0]["stops_away"] == 6,
        f"route distance and stop count reported: {seen[0]['route_distance_m']} m, "
        f"{seen[0]['stops_away']} stops",
    )
    check(all(s["eta_source"] == "route" for s in seen[1:]), "later polls measure along the route")
    times = {s["eta_time"] for s in seen[1:]}
    check(len(times) == 1, f"the arrival time holds steady across polls: {times}")
    check(
        [s["stops_away"] for s in seen] == [6, 5, 4, 3, 2, 1],
        f"stops away counts down: {[s['stops_away'] for s in seen]}",
    )
    check(seen[-1]["route_distance_m"] == 500, "route distance counts down to the stop")


def test_route_eta_uses_passage_times():
    """Speed comes from when the vehicle reached earlier stops, dwell included."""
    route, locate = _straight_route()
    track = build_track(route, "7008", "01", locate)
    tracker = VehicleTracker()
    t0 = datetime(2026, 7, 25, 14, 0, 0)
    target_lat = STOP_LAT + 4000 / METRES_PER_DEGREE_LAT

    # One stop (500 m) every 100 s, i.e. 5 m/s including the time spent at them.
    for step in range(4):
        t = t0 + timedelta(seconds=100 * step)
        result = tracker.estimate(
            STOP_KEY, target_lat, STOP_LON, _route_vehicle(500 * step, t), now=t,
            route_track=track,
        )
    # 4 stops covered, 2500 m to go at 5 m/s = 500 s.
    check(result["eta_source"] == "route", "passage timing yields a route-measured speed")
    check(
        abs(result["eta_minutes"] - 8) <= 1,
        f"ETA from passage speed is ~8 min, got {result['eta_minutes']}",
    )


def test_route_eta_detects_a_passed_stop():
    route, locate = _straight_route()
    track = build_track(route, "7002", "01", locate)  # our stop is 1000 m in
    tracker = VehicleTracker()
    t0 = datetime(2026, 7, 25, 14, 0, 0)
    target_lat = STOP_LAT + 1000 / METRES_PER_DEGREE_LAT

    tracker.estimate(STOP_KEY, target_lat, STOP_LON, _route_vehicle(500, t0), now=t0,
                     route_track=track)
    t1 = t0 + timedelta(seconds=60)
    gone = tracker.estimate(
        STOP_KEY, target_lat, STOP_LON, _route_vehicle(2500, t1), now=t1, route_track=track
    )
    check(gone["eta_minutes"] is None, "no ETA once the trip is beyond our stop")
    check(gone["eta_status"] == "passed", "the reason is reported")


def test_route_eta_falls_back_off_route():
    """A vehicle not on this route keeps the straight-line estimate."""
    route, locate = _straight_route()
    track = build_track(route, "7008", "01", locate)
    tracker = VehicleTracker()
    t0 = datetime(2026, 7, 25, 14, 0, 0)
    target_lat = STOP_LAT + 4000 / METRES_PER_DEGREE_LAT

    # 3 km to the east of the route: far outside the residual limit.
    def aside(metres, t):
        v = _route_vehicle(metres, t)
        v["Lon"] = STOP_LON + 0.05
        return v

    first = tracker.estimate(STOP_KEY, target_lat, STOP_LON, aside(2000, t0), now=t0,
                             route_track=track)
    check(first["route_distance_m"] is None, "no route measurement for an off-route vehicle")
    check(first["eta_status"] == "ok", "it still gets a straight-line estimate")
    t1 = t0 + timedelta(seconds=60)
    second = tracker.estimate(STOP_KEY, target_lat, STOP_LON, aside(2600, t1), now=t1,
                              route_track=track)
    check(second["eta_source"] == "tracked", "the fallback is the straight-line measurement")
    check(second["stops_away"] is None, "no stop count without a route")


def test_stop_coordinate_lookup():
    row = {"zespol": "7009", "slupek": "01", "szer_geo": "52.219450", "dlug_geo": "21.011280"}
    check(stop_coords(row) == (52.21945, 21.01128), "stop coordinates parse from strings")
    check(stop_coords({"zespol": "7009"}) is None, "a row without coordinates yields None")
    check(stop_coords({"szer_geo": "n/a", "dlug_geo": "1"}) is None, "garbage yields None")
    check("01" in pole_variants("1"), "an unpadded pole matches the API's '01'")
    check("1" in pole_variants("01"), "a padded pole matches an unpadded one")


def test_service_day():
    """Departures after midnight belong to the previous service day (04:00 boundary)."""
    check(service_day(datetime(2026, 7, 25, 23, 0)) == "2026-07-25", "late evening -> same day")
    check(service_day(datetime(2026, 7, 26, 3, 59)) == "2026-07-25", "03:59 -> previous day")
    check(service_day(datetime(2026, 7, 26, 4, 0)) == "2026-07-26", "04:00 -> new service day")


def test_daily_cache():
    """Timetables must be fetched once per service day, not once per poll."""
    calls = []

    async def factory(tag="a"):
        calls.append(tag)
        return {"rows": [tag]}

    cache = DailyCache(None, ttl=3600)
    check(run(cache.get("k", factory)) == {"rows": ["a"]}, "first get calls the factory")
    check(run(cache.get("k", factory)) == {"rows": ["a"]}, "second get is served from cache")
    check(len(calls) == 1, f"factory called exactly once, got {len(calls)}")

    run(cache.get("other", factory))
    check(len(calls) == 2, "a different key is fetched separately")

    # Expiry: rewind the stored timestamp past the TTL.
    day, _, value = cache._entries["k"]
    cache._entries["k"] = (day, time.time() - 4000, value)
    run(cache.get("k", factory))
    check(len(calls) == 3, "an entry older than the TTL is refetched")

    # Service-day rollover invalidates even a fresh entry.
    _, at, value = cache._entries["k"]
    cache._entries["k"] = ("1999-01-01", at, value)
    run(cache.get("k", factory))
    check(len(calls) == 4, "an entry from another service day is refetched")


def test_daily_cache_single_flight():
    """A cold stop fans out one timetable request per line; identical keys must share."""
    calls = []

    async def slow():
        calls.append(1)
        await asyncio.sleep(0.01)
        return ["rows"]

    cache = DailyCache(None, ttl=3600)

    async def race():
        return await asyncio.gather(*(cache.get("same", slow) for _ in range(5)))

    results = run(race())
    check(len(calls) == 1, f"concurrent gets of one key fetch once, got {len(calls)}")
    check(all(r == ["rows"] for r in results), "every concurrent caller gets the value")


def test_daily_cache_persistence():
    """A restart must replay the day's timetables instead of refetching them."""
    calls = []

    async def factory():
        calls.append(1)
        return ["rows"]

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "timetable_cache.json")
        cache = DailyCache(path, ttl=3600, name="timetable")
        run(cache.get("timetable|7009|01|190", factory))
        run(cache.flush())
        check(os.path.isfile(path), "flush writes the cache file")

        # A stale-day entry is written but must not survive the next load.
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        payload["entries"]["timetable|7009|01|999"] = {
            "day": "1999-01-01", "at": time.time(), "value": ["old"],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

        # A fresh process warms the cache up front, the way the app's lifespan
        # does, so startup reports what it will reuse before any request.
        reloaded = DailyCache(path, ttl=3600, name="timetable")
        reloaded.load()
        check(
            "timetable|7009|01|190" in reloaded._entries,
            "an eager load() populates the cache before the first get()",
        )
        entries_after_first_load = dict(reloaded._entries)
        reloaded.load()
        check(
            reloaded._entries == entries_after_first_load,
            "load() is idempotent — a second call changes nothing",
        )
        check(
            run(reloaded.get("timetable|7009|01|190", factory)) == ["rows"],
            "a fresh process reads the value back from disk",
        )
        check(len(calls) == 1, "reloading from disk issues no new request")
        check(
            "timetable|7009|01|999" not in reloaded._entries,
            "entries from an old service day are pruned on load",
        )

        # A file holding only other service days must start cold, not reuse it.
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"entries": {"timetable|7009|01|190": {
                "day": "1999-01-01", "at": time.time(), "value": ["old"],
            }}}, fh)
        yesterday = DailyCache(path, ttl=3600, name="timetable")
        yesterday.load()
        check(yesterday._entries == {}, "a cache from another service day loads empty")
        check(
            run(yesterday.get("timetable|7009|01|190", factory)) == ["rows"],
            "a stale service day refetches rather than serving yesterday's timetable",
        )
        check(len(calls) == 2, "the stale-day refetch hit the factory")

        # An unreadable file must degrade to an empty cache, not crash.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        broken = DailyCache(path, ttl=3600, name="timetable")
        run(broken.get("k", factory))
        check(len(calls) == 3, "a corrupt cache file is ignored, not fatal")

        # A missing file is the fresh-install path: cold, but not an error.
        os.remove(path)
        cold = DailyCache(path, ttl=3600, name="timetable")
        cold.load()
        check(cold._entries == {}, "a missing cache file starts cold")
        run(cold.get("k", factory))
        check(len(calls) == 4, "a cold start fetches")


def test_describe_call():
    """Every logged request must name its endpoint and parameters."""
    bus = describe_call(EP_VEHICLES, {"type": 1})
    tram = describe_call(EP_VEHICLES, {"type": 2})
    check(bus == "vehicles[bus] type=1", f"bus GPS call is labelled: {bus}")
    check(tram == "vehicles[tram] type=2", f"tram GPS call is labelled: {tram}")
    check(bus != tram, "bus and tram GPS calls are distinguishable in the log")

    check(
        describe_call(EP_TIMETABLE, {"busstopId": "7009", "busstopNr": "01", "line": "190"})
        == "timetable busstopId=7009 busstopNr=01 line=190",
        "timetable call shows stop and line",
    )
    check(
        describe_call(EP_LINES, {"busstopId": "7009", "busstopNr": "01"})
        == "lines busstopId=7009 busstopNr=01",
        "line-list call shows the stop",
    )
    check(describe_call(EP_STOPS) == "stops", "parameterless call is just its label")


class _FakeClient:
    """Counts API calls so the tests can assert on request volume."""

    def __init__(self, lines=("190", "500")):
        self._lines = list(lines)
        self.calls = {"lines": 0, "timetable": 0, "vehicles": 0}
        # Which GPS feeds were asked for, in call order.
        self.vehicle_types = []

    async def lines_for_stop(self, busstop_id, pole):
        self.calls["lines"] += 1
        return self._lines

    async def timetable(self, busstop_id, pole, line):
        self.calls["timetable"] += 1
        return [{"czas": "14:05:00", "kierunek": "Marymont", "brygada": "2"}]

    async def vehicle_positions(self, vehicle_type, line=None):
        self.calls["vehicles"] += 1
        self.vehicle_types.append(vehicle_type)
        # Answer with a line of the requested mode, so a test can tell which feed
        # produced an overlay rather than only counting calls.
        return [
            {
                "Lines": "190" if vehicle_type == 1 else "15",
                "Brigade": "2",
                "Lat": 52.2,
                "Lon": 21.0,
            }
        ]


def test_shared_vehicle_snapshot():
    """The GPS feeds are city-wide: one snapshot must serve every tracked stop."""
    now = datetime(2026, 7, 25, 14, 0, 0)

    # Left to itself, a stop fetches a snapshot — but only of the feeds its own
    # lines need; 190 and 500 are buses.
    client = _FakeClient()
    run(next_departures(client, "7009", "01", now=now))
    check(client.vehicle_types == [1], "unshared call fetches only the feed the lines need")

    # A caller that already has a snapshot must issue no vehicle request at all,
    # however many stops it processes.
    client = _FakeClient()
    shared = [{"Lines": "190", "Brigade": "2", "Lat": 52.3, "Lon": 21.1}]
    deps = []
    for pole in ("01", "02", "03"):
        deps += run(next_departures(client, "7009", pole, vehicles=shared, now=now))
    check(client.calls["vehicles"] == 0, "a shared snapshot triggers no GPS requests")
    check(any(d["live"] and d["lat"] == 52.3 for d in deps), "the shared snapshot still overlays")

    # [] means "we tried and failed" — skip the overlay rather than refetching.
    client = _FakeClient()
    run(next_departures(client, "7009", "01", vehicles=[], now=now))
    check(client.calls["vehicles"] == 0, "an empty snapshot skips the overlay, no refetch")


def test_vehicle_type_for_line():
    """Warsaw numbers trams 1..99 and buses 100+; lettered codes are all buses."""
    cases = {
        "1": 2,
        "15": 2,
        "99": 2,
        "100": 1,
        "190": 1,
        "523": 1,
        # Lettered lines: local, night, substitute, express — never trams.
        "L-8": 1,
        "L33": 1,
        "N44": 1,
        "Z1": 1,
        "E-2": 1,
        # Metro and rail show up at some poles but are in neither GPS feed.
        "M1": None,
        "M2": None,
        "S2": None,
        "": None,
    }
    for line, expected in cases.items():
        got = vehicle_type_for_line(line)
        check(got == expected, f"line {line!r} -> feed {got}")

    check(vehicle_type_for_line(" 15 ") == 2, "the line is stripped before classifying")
    check(
        vehicle_types_for_lines(["15", "33", "190", "M1"]) == (1, 2),
        "a mixed pole needs both feeds, de-duplicated and sorted",
    )
    check(vehicle_types_for_lines(["15", "33"]) == (2,), "a tram-only pole needs one feed")
    check(vehicle_types_for_lines(["M1", "M2"]) == (), "a metro-only pole needs no feed")


def test_feed_selection_per_stop():
    """A stop must download only the GPS feeds its own lines can appear in."""
    now = datetime(2026, 7, 25, 14, 0, 0)

    client = _FakeClient(lines=("15", "33"))
    deps = run(next_departures(client, "7009", "01", now=now))
    check(client.vehicle_types == [2], "a tram-only stop skips the bus feed")
    check(any(d["live"] for d in deps), "the tram feed still overlays its departures")

    client = _FakeClient(lines=("15", "190"))
    run(next_departures(client, "7009", "01", now=now))
    check(sorted(client.vehicle_types) == [1, 2], "a mixed stop still fetches both feeds")

    client = _FakeClient(lines=("M1",))
    deps = run(next_departures(client, "7009", "01", now=now))
    check(client.calls["vehicles"] == 0, "a metro-only stop makes no GPS request")
    check(deps and not any(d["live"] for d in deps), "its departures are scheduled-only")


def test_vehicle_types_for_stops():
    """The poller's feed set is the union over every tracked stop."""

    class _Failing(_FakeClient):
        async def lines_for_stop(self, busstop_id, pole):
            raise WarsawApiError("boom")

    def stop(stop_id, pole="01"):
        return {"id": f"{stop_id}-{pole}", "busstop_id": stop_id, "pole": pole}

    trams = _FakeClient(lines=("15", "33"))
    check(
        run(vehicle_types_for_stops(trams, [stop("7009"), stop("7009", "02")])) == (2,),
        "tram-only stops resolve to the tram feed alone",
    )

    mixed = _FakeClient(lines=("15", "190"))
    check(
        run(vehicle_types_for_stops(mixed, [stop("7009")])) == (1, 2),
        "a stop served by both modes resolves to both feeds",
    )

    check(run(vehicle_types_for_stops(_FakeClient(), [])) == (), "no stops, no feeds")

    # A line list we cannot read must cost the saving, not the live data.
    check(
        run(vehicle_types_for_stops(_Failing(), [stop("7009")])) == (1, 2),
        "an unreadable line list falls back to every feed",
    )


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
        ("eta_time", "card renders the live arrival estimate"),
        ("delay_minutes", "card renders the delay against the timetable"),
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
    test_haversine()
    test_parse_fix_time()
    test_is_live_needs_a_recent_fix()
    test_eta_from_closing_distance()
    test_eta_ignores_a_repeated_fix()
    test_eta_suppressed_when_moving_away()
    test_eta_withheld_for_a_parked_vehicle()
    test_eta_survives_a_stop_at_a_light()
    test_moving_away_does_not_flap()
    test_eta_withheld_for_a_layover()
    test_eta_rejects_unusable_fixes()
    test_eta_at_the_stop()
    test_tracker_prune()
    test_normalize_routes()
    test_routes_cache_roundtrip()
    test_routes_provider_selection()
    test_route_projection()
    test_route_projection_on_a_loop()
    test_route_eta_is_stable()
    test_route_eta_uses_passage_times()
    test_route_eta_detects_a_passed_stop()
    test_route_eta_falls_back_off_route()
    test_overlay_adds_eta()
    test_overlay_claims_only_the_next_run()
    test_overlay_ignores_stale_vehicles()
    test_stop_coordinate_lookup()
    test_service_day()
    test_daily_cache()
    test_daily_cache_single_flight()
    test_daily_cache_persistence()
    test_describe_call()
    test_shared_vehicle_snapshot()
    test_vehicle_type_for_line()
    test_feed_selection_per_stop()
    test_vehicle_types_for_stops()
    test_lovelace_card_shipped()
    test_card_install_detection()
    print("\nAll smoke tests passed.")
