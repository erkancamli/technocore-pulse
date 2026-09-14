"""The tail must never be cut mid-object, whatever the reading looks like.

This is here because the first implementation did exactly that: it sliced the
JSON to fit and produced a string that still began with the `pulse=` marker and
still looked parseable. A reader would not have found a bug, it would have found
a lie. The tests below are the ones that caught it.
"""

from __future__ import annotations

import json

import technocore_pulse as p

ENTRY = {
    "last_seq": 8_400_000,
    "per_second": 4.63,
    "per_day_projected": 399_765,
    "distinct_ratio": 0.347,
    "top_line_share": 0.075,
    "distinct_senders": 368,
    "sampled": 400,
}


def reading(room_count: int) -> dict:
    return {
        "at": "2026-09-14T15:50:01+00:00",
        "rooms": {f"room{i}": dict(ENTRY) for i in range(room_count)},
    }


def parse(tail: str) -> dict:
    assert tail.startswith("pulse=")
    return json.loads(tail[len("pulse=") :])


def test_a_tail_that_fits_keeps_every_room_and_says_nothing_about_truncation():
    payload = parse(p.machine_tail(reading(2)))
    assert len(payload["rooms"]) == 2
    assert "trunc" not in payload


def test_an_oversized_reading_drops_rooms_rather_than_characters():
    tail = p.machine_tail(reading(200))
    assert len(tail) <= 4000
    payload = parse(tail)  # the point: it still parses
    assert payload["trunc"] == 200 - len(payload["rooms"])


def test_the_dropped_count_and_the_kept_rooms_always_add_up():
    for count in (1, 5, 40, 120, 300):
        payload = parse(p.machine_tail(reading(count)))
        assert len(payload["rooms"]) + payload.get("trunc", 0) == count


def test_a_tight_budget_still_yields_valid_json():
    for budget in (4000, 1000, 400, 200, 120):
        tail = p.machine_tail(reading(50), budget=budget)
        parse(tail)


def test_the_configured_report_room_survives_truncation():
    result = reading(200)
    result["rooms"][p.REPORT_ROOM] = dict(ENTRY)
    payload = parse(p.machine_tail(result))
    assert p.REPORT_ROOM in payload["rooms"]


def test_composed_messages_parse_at_every_size():
    for count in (1, 3, 25, 100, 400):
        line = p.compose(reading(count))
        assert len(line) <= 4000
        parse(line[line.index("pulse=") :])
