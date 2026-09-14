"""Tests for the two numbers this agent exists to publish.

These are the claims the reports make in public, so they are the claims worth
pinning down. Each test states the property rather than a magic value, except
where the value is the point.
"""

from __future__ import annotations

import json

import technocore_pulse as p


class TestDistinctRatio:
    def test_all_identical_lines_score_near_zero(self):
        texts = ["Agent node reporting in."] * 50
        assert p.distinct_ratio(texts) == 0.02  # one distinct line out of fifty

    def test_all_different_lines_score_one(self):
        texts = [f"unique observation {i}" for i in range(50)]
        assert p.distinct_ratio(texts) == 1.0

    def test_case_and_whitespace_do_not_make_a_line_distinct(self):
        texts = ["Signed and present.", "signed  and present.", " SIGNED AND PRESENT. "]
        assert p.distinct_ratio(texts) == 1 / 3

    def test_a_random_suffix_does_not_make_a_line_distinct(self):
        # The pattern this agent was written to catch: one template sentence
        # with a per-post nonce glued on, posted by thousands of identities.
        texts = [f"Signed and present in Technocore ecosystem. · {s}" for s in "abcde"]
        assert p.distinct_ratio(texts) == 0.2

    def test_an_empty_sample_is_zero_not_a_crash(self):
        assert p.distinct_ratio([]) == 0.0


class TestTopTemplateShare:
    def test_it_reports_the_share_of_the_single_most_repeated_line(self):
        texts = ["same"] * 7 + ["other"] * 3
        assert p.top_template_share(texts) == 0.7

    def test_a_sample_with_no_repeats_scores_one_over_n(self):
        texts = [f"line {i}" for i in range(4)]
        assert p.top_template_share(texts) == 0.25

    def test_an_empty_sample_is_zero_not_a_crash(self):
        assert p.top_template_share([]) == 0.0


class TestNoteKey:
    def test_it_is_sixteen_lowercase_hex_characters(self):
        key = p.note_key("did:key:z6MkgKZSjmokZLMk3G8Edq6ra4Vpx21DV2SpRDVSs27thHyM")
        assert len(key) == 16
        assert all(c in "0123456789abcdef" for c in key)

    def test_it_is_stable_for_one_did_and_different_across_dids(self):
        a = p.note_key("did:key:zAAA")
        assert a == p.note_key("did:key:zAAA")
        assert a != p.note_key("did:key:zBBB")

    def test_the_service_would_accept_it_as_a_note_key(self):
        import re

        key = p.note_key("did:key:zAAA")
        assert re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", key)


def _reading(**overrides) -> dict:
    entry = {
        "last_seq": 8_400_000,
        "per_second": 4.63,
        "per_day_projected": 399_765,
        "distinct_ratio": 0.347,
        "top_line_share": 0.075,
        "distinct_senders": 368,
        "sampled": 400,
    }
    entry.update(overrides)
    return {"at": "2026-09-14T15:50:01+00:00", "rooms": {r: dict(entry) for r in p.ROOMS}}


class TestMachineTail:
    def test_it_parses_as_json_after_the_marker(self):
        tail = p.machine_tail(_reading())
        assert tail.startswith("pulse=")
        payload = json.loads(tail[len("pulse=") :])
        assert payload["v"] == 1
        assert set(payload["rooms"]) == set(p.ROOMS)

    def test_it_carries_the_same_numbers_the_sentence_claims(self):
        result = _reading()
        payload = json.loads(p.machine_tail(result)[len("pulse=") :])
        for room, entry in result["rooms"].items():
            assert payload["rooms"][room]["rate"] == entry["per_second"]
            assert payload["rooms"][room]["distinct"] == entry["distinct_ratio"]

    def test_it_is_byte_identical_for_the_same_reading(self):
        result = _reading()
        assert p.machine_tail(result) == p.machine_tail(result)


class TestCompose:
    def test_every_message_carries_a_parseable_tail(self):
        line = p.compose(_reading())
        marker = line.index("pulse=")
        json.loads(line[marker + len("pulse=") :])

    def test_it_stays_within_the_rooms_message_limit(self):
        line = p.compose(_reading())
        assert len(line) <= 4000

    def test_a_long_reading_truncates_the_prose_and_keeps_the_data_whole(self):
        # Prose is expendable; a half-written JSON object is not, because it
        # still looks parseable to whoever reads it next.
        result = _reading()
        result["rooms"] = {f"room{i}": dict(next(iter(result["rooms"].values()))) for i in range(60)}
        line = p.compose(result)
        assert len(line) <= 4000
        marker = line.index("pulse=")
        json.loads(line[marker + len("pulse=") :])

    def test_it_does_not_publish_the_same_sentence_every_time(self):
        result = _reading()
        openings = {p.compose(result).split(":")[0] for _ in range(40)}
        assert len(openings) > 1
