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
)
from app.eta import VehicleTracker, haversine_m, is_live, parse_fix_time  # noqa: E402
from app.warsaw_api import (  # noqa: E402
    EP_LINES,
    EP_STOPS,
    EP_TIMETABLE,
    EP_VEHICLES,
    _flatten,
    describe_call,
    match_stops,
    normalize,
    pole_variants,
    stop_coords,
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

    async def lines_for_stop(self, busstop_id, pole):
        self.calls["lines"] += 1
        return self._lines

    async def timetable(self, busstop_id, pole, line):
        self.calls["timetable"] += 1
        return [{"czas": "14:05:00", "kierunek": "Marymont", "brygada": "2"}]

    async def vehicle_positions(self, vehicle_type, line=None):
        self.calls["vehicles"] += 1
        return [{"Lines": "190", "Brigade": "2", "Lat": 52.2, "Lon": 21.0}]


def test_shared_vehicle_snapshot():
    """The GPS feeds are city-wide: one snapshot must serve every tracked stop."""
    now = datetime(2026, 7, 25, 14, 0, 0)

    # Left to itself, a stop fetches one snapshot per vehicle type.
    client = _FakeClient()
    run(next_departures(client, "7009", "01", now=now))
    check(client.calls["vehicles"] == 2, "unshared call fetches the bus and tram feeds")

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
    test_lovelace_card_shipped()
    test_card_install_detection()
    print("\nAll smoke tests passed.")
