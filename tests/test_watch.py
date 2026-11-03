"""Tests for the readiness watcher.

No network. The fetcher is a dict, which is the whole reason it is injected.
The cases that matter are the ones that must NOT fire: a reworded page and a
page that failed to load are not an opening, and an agent that cries testnet
on either of those is worse than no watcher at all.
"""

from __future__ import annotations

import json

import pytest

import flop_watch as fw


CLOSED_PAGE = (
    "<html><body><h1>devnet</h1>"
    "<p>Chain spec: Unavailable \u2014 no verified public value</p>"
    "<p>Public operator enrollment is unavailable until the network identity "
    "and release are verified.</p></body></html>"
)

OPEN_PAGE = (
    "<html><body><h1>devnet</h1>"
    "<p>Chain spec: flop-devnet-7</p><p>RPC: https://rpc.devnet.example</p>"
    "<p>Enrollment is open.</p></body></html>"
)


def fetcher(pages: dict):
    def fetch(url):
        if url not in pages:
            raise RuntimeError("HTTP 404")
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        return value
    return fetch


def all_closed() -> dict:
    return {url: CLOSED_PAGE for _, url, _gated in fw.WATCH}


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "state.json", tmp_path / "log.jsonl"


# ------------------------------------------------------------------ stripping


def test_tags_and_scripts_come_out():
    html = "<p>hello</p><script>var x = 'no verified public value';</script><b>World</b>"
    text = fw.plain(html)
    assert "hello world" in text
    assert "var x" not in text


def test_text_is_lowercased_and_collapsed():
    assert fw.plain("<p>A   \n  B</p>") == "a b"


def test_a_marker_inside_a_script_does_not_count():
    """Otherwise a tracking snippet could hold a page closed forever."""
    html = "<script>'no verified public value'</script><p>RPC: rpc.example</p>"
    assert fw.verdict(fw.plain(html))["closed"] is False


# ------------------------------------------------------------------- verdicts


def test_the_closed_page_reads_as_closed():
    result = fw.verdict(fw.plain(CLOSED_PAGE))
    assert result["closed"] is True
    assert "no verified public value" in result["markers"]


def test_the_open_page_reads_as_open():
    assert fw.verdict(fw.plain(OPEN_PAGE))["closed"] is False


def test_the_evidence_is_recorded_not_just_the_answer():
    assert fw.verdict(fw.plain(CLOSED_PAGE))["markers"]


# ------------------------------------------------------- the first run and on


def test_a_first_run_reports_closed_and_announces_nothing(paths):
    state, log = paths
    report = fw.run(fetcher(all_closed()), state, log)
    assert report["still_closed"] == len(fw.WATCH)
    assert report["opened"] == []
    assert not log.exists()  # nothing happened, so nothing is logged


def test_an_unchanged_second_run_is_silent(paths):
    state, log = paths
    pages = all_closed()
    fw.run(fetcher(pages), state, log)
    report = fw.run(fetcher(pages), state, log)
    assert report["opened"] == []
    assert report["reworded"] == []


def test_a_page_that_opens_is_the_loud_case(paths):
    state, log = paths
    pages = all_closed()
    fw.run(fetcher(pages), state, log)

    pages["https://ask.flop.finance/networks/devnet"] = OPEN_PAGE
    report = fw.run(fetcher(pages), state, log)

    assert [o["label"] for o in report["opened"]] == ["ask-devnet"]
    assert "NO LONGER CLOSED" in "\n".join(fw.lines(report))
    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert entry["event"] == "page_opened"
    assert entry["pages"][0]["label"] == "ask-devnet"


def test_an_opening_is_announced_once_not_every_day(paths):
    state, log = paths
    pages = all_closed()
    fw.run(fetcher(pages), state, log)
    pages["https://ask.flop.finance/networks/devnet"] = OPEN_PAGE
    fw.run(fetcher(pages), state, log)
    report = fw.run(fetcher(pages), state, log)
    assert report["opened"] == []
    events = [json.loads(l)["event"] for l in log.read_text(encoding="utf-8").splitlines()]
    assert events.count("page_opened") == 1


def test_the_opening_time_is_kept(paths):
    state, log = paths
    pages = all_closed()
    fw.run(fetcher(pages), state, log)
    pages["https://ask.flop.finance/networks/devnet"] = OPEN_PAGE
    fw.run(fetcher(pages), state, log)
    stored = fw.load_state(state)["https://ask.flop.finance/networks/devnet"]
    assert stored["opened_at"]
    assert stored["closed"] is False


# ------------------------------------------------- what must not count as news


def test_a_reworded_page_is_not_an_opening(paths):
    state, log = paths
    pages = all_closed()
    fw.run(fetcher(pages), state, log)

    pages["https://flop.finance/testnet/"] = CLOSED_PAGE.replace(
        "devnet", "devnet (updated)"
    )
    report = fw.run(fetcher(pages), state, log)

    assert report["opened"] == []
    assert [r["label"] for r in report["reworded"]] == ["testnet-page"]
    assert not log.exists()


def test_a_page_that_fails_to_load_is_not_an_opening(paths):
    state, log = paths
    pages = all_closed()
    fw.run(fetcher(pages), state, log)

    del pages["https://ask.flop.finance/networks/testnet"]
    report = fw.run(fetcher(pages), state, log)

    assert report["opened"] == []
    assert [e["label"] for e in report["errors"]] == ["ask-testnet"]
    assert not log.exists()


def test_a_failed_load_leaves_the_stored_verdict_alone(paths):
    state, log = paths
    pages = all_closed()
    fw.run(fetcher(pages), state, log)
    before = fw.load_state(state)["https://ask.flop.finance/networks/testnet"]

    del pages["https://ask.flop.finance/networks/testnet"]
    fw.run(fetcher(pages), state, log)
    after = fw.load_state(state)["https://ask.flop.finance/networks/testnet"]

    assert after == before


def test_a_timeout_reads_as_an_error_not_a_change(paths):
    state, log = paths
    pages = all_closed()
    pages["https://flop.finance/llms.txt"] = RuntimeError("unreachable: timed out")
    report = fw.run(fetcher(pages), state, log)
    assert [e["label"] for e in report["errors"]] == ["llms"]
    assert "could not read" in "\n".join(fw.lines(report))


# ---------------------------------------------------------------------- state


def test_corrupt_state_does_not_stop_a_run(paths):
    state, log = paths
    state.write_text("{not json", encoding="utf-8")
    report = fw.run(fetcher(all_closed()), state, log)
    assert report["still_closed"] == len(fw.WATCH)


def test_no_half_written_state_survives(paths):
    state, log = paths
    fw.run(fetcher(all_closed()), state, log)
    assert state.exists()
    assert not state.with_suffix(".tmp").exists()
    json.loads(state.read_text(encoding="utf-8"))


def test_a_page_with_no_gate_text_is_never_reported_as_newly_open(paths):
    """The flaw a live run exposed.

    run-miner and llms.txt never carried the gate text, so comparing them
    against it reported them as newly open on the first run and would have
    done so every morning after. A page that cannot close cannot open.
    """
    state, log = paths
    pages = {url: CLOSED_PAGE for _, url, gated in fw.WATCH if gated}
    pages["https://ask.flop.finance/docs/run-miner"] = "<p>run a miner</p>"
    pages["https://flop.finance/llms.txt"] = "plain index, nothing here"
    report = fw.run(fetcher(pages), state, log)

    assert report["opened"] == []
    rendered = "\n".join(fw.lines(report))
    assert "NO LONGER CLOSED" not in rendered
    assert "watched for change" in rendered


def test_a_signal_word_appearing_on_an_ungated_page_is_news(paths):
    state, log = paths
    pages = {url: CLOSED_PAGE for _, url, gated in fw.WATCH if gated}
    pages["https://ask.flop.finance/docs/run-miner"] = "<p>run a miner</p>"
    pages["https://flop.finance/llms.txt"] = "plain index, nothing here"
    fw.run(fetcher(pages), state, log)

    pages["https://flop.finance/llms.txt"] = "index: faucet at faucet.example"
    report = fw.run(fetcher(pages), state, log)

    assert [s["label"] for s in report["new_signals"]] == ["llms"]
    assert report["new_signals"][0]["new_signals"] == ["faucet"]
    assert "NEW on llms: faucet" in "\n".join(fw.lines(report))
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["event"] == (
        "new_signal_words"
    )


def test_a_signal_word_that_was_always_there_is_not_news(paths):
    state, log = paths
    pages = {url: CLOSED_PAGE for _, url, gated in fw.WATCH if gated}
    pages["https://ask.flop.finance/docs/run-miner"] = "<p>rpc and gateway explained</p>"
    pages["https://flop.finance/llms.txt"] = "plain index"
    fw.run(fetcher(pages), state, log)
    report = fw.run(fetcher(pages), state, log)
    assert report["new_signals"] == []
    assert not log.exists()


def test_every_watched_page_is_stored_under_its_own_url(paths):
    state, log = paths
    fw.run(fetcher(all_closed()), state, log)
    stored = fw.load_state(state)
    assert set(stored) == {url for _, url, _gated in fw.WATCH}


# ----------------------------------------------------------- hostnames found


def test_flop_hostnames_are_picked_out_of_a_page():
    text = fw.plain(
        "<p>api.flop.network and faucet.flop.network and rpc.devnet.flop.network</p>"
    )
    assert fw.hosts(text) == [
        "api.flop.network", "faucet.flop.network", "rpc.devnet.flop.network"
    ]


def test_a_repeated_hostname_is_one_finding_not_forty():
    text = fw.plain("<p>api.flop.network api.flop.network api.flop.network</p>")
    assert fw.hosts(text) == ["api.flop.network"]


def test_an_unrelated_hostname_is_ignored():
    text = fw.plain("<p>example.com and docs.example.org</p>")
    assert fw.hosts(text) == []


def test_a_first_run_treats_no_hostname_as_new(paths):
    """Otherwise the first morning reports every endpoint FLOP ever published."""
    state, log = paths
    pages = {url: CLOSED_PAGE for _, url, _g in fw.WATCH}
    pages["https://flop.finance/official/"] = "<p>api.flop.network</p>"
    report = fw.run(fetcher(pages), state, log)
    assert report["new_hosts"] == []
    assert not log.exists()


def test_a_hostname_that_appears_later_is_the_signal(paths):
    """The case that was missed on 3 November 2026.

    A testnet RPC appearing under a name nobody had guessed in advance is
    exactly what the keyword list could not catch and this can.
    """
    state, log = paths
    pages = {url: CLOSED_PAGE for _, url, _g in fw.WATCH}
    fw.run(fetcher(pages), state, log)

    pages["https://docs.flop.network/networks/testnet"] = (
        "<p>RPC: rpc.testnet.flop.network</p><p>no verified public value</p>"
    )
    report = fw.run(fetcher(pages), state, log)

    assert [h["label"] for h in report["new_hosts"]] == ["docs-testnet"]
    assert report["new_hosts"][0]["new_hosts"] == ["rpc.testnet.flop.network"]
    rendered = "\n".join(fw.lines(report))
    assert "NEW HOST on docs-testnet: rpc.testnet.flop.network" in rendered
    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert entry["event"] == "new_hosts"


def test_a_hostname_already_seen_on_another_page_is_not_new(paths):
    """The same API base on a second page is not an announcement."""
    state, log = paths
    pages = {url: CLOSED_PAGE for _, url, _g in fw.WATCH}
    pages["https://flop.finance/official/"] = "<p>api.flop.network</p>"
    fw.run(fetcher(pages), state, log)

    pages["https://docs.flop.network/"] = "<p>see api.flop.network</p>"
    report = fw.run(fetcher(pages), state, log)
    assert report["new_hosts"] == []


def test_the_docs_domain_is_watched():
    """The gap that made the watcher useless on the day it mattered."""
    watched = {url for _, url, _g in fw.WATCH}
    assert "https://docs.flop.network/networks/testnet" in watched
    assert "https://docs.flop.network/networks/devnet" in watched
    assert "https://flop.finance/official/" in watched
