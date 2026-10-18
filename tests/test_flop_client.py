"""Tests for the FLOP buying client.

No network and no identity. The transport is replaced wholesale, which is the
reason it exists as one class, and every path that could spend money is
exercised against it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

import flop_client as fc


class FakeTransport(fc.Transport):
    """Answers calls from a script keyed by (method, url prefix)."""

    def __init__(self, answers=None, fail_after=None):
        super().__init__()
        self.answers = answers or {}
        self.fail_after = fail_after

    def call(self, method, url, body=None):
        self.calls.append({"method": method, "url": url, "body": body})
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("connection lost")
        for key, value in self.answers.items():
            if url.startswith(key):
                return value
        return {}


ENDPOINTS = {name: f"https://example.test/{name}" for name in fc.ENDPOINTS}


@pytest.fixture
def config(tmp_path):
    identity = tmp_path / "keys" / "identity.pem"
    identity.parent.mkdir(parents=True)
    identity.write_text("not a real key", encoding="utf-8")
    return {
        "did": "did:key:zTest",
        "wallet_address": "flop1test",
        "identity_path": str(identity),
        "endpoints": dict(ENDPOINTS),
        "max_spend_per_session": "100",
        "max_spend_total": "500",
    }


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Nothing in this suite may read the machine it runs on.

    The first version of this fixture left FLEET_ROOTS alone, so the clean
    machine test passed on a laptop with no fleet directory and failed on the
    server that actually had one. A test whose result depends on the host is
    not testing the code.
    """
    monkeypatch.setattr(fc, "STATE_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fc, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(fc, "FLEET_ROOTS", ())


def ledger_of(tmp_path):
    return tmp_path / "ledger.jsonl"


# --------------------------------------------------------- unpublished surface


def test_every_endpoint_is_blank_in_the_template():
    assert set(fc.CONFIG_TEMPLATE["endpoints"]) == set(fc.ENDPOINTS)
    assert all(v is None for v in fc.CONFIG_TEMPLATE["endpoints"].values())


def test_a_missing_endpoint_names_itself_and_what_it_is_for(config):
    config["endpoints"]["faucet"] = None
    with pytest.raises(fc.NotPublishedYet) as error:
        fc.endpoint(config, "faucet")
    assert "faucet" in str(error.value)
    assert "testnet FLOP" in str(error.value)


def test_an_unknown_endpoint_is_a_programming_error(config):
    with pytest.raises(KeyError):
        fc.endpoint(config, "mint_tokens")


# ------------------------------------------------- one participant, one score


def test_a_second_key_beside_the_identity_stops_the_run(config):
    Path(config["identity_path"]).with_name("spare.pem").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        fc.identity_guard(config)
    assert "more than one identity" in str(error.value)


def test_a_leftover_fleet_directory_stops_the_run(config, tmp_path):
    fleet = tmp_path / "close1-fleet" / "pair-01"
    fleet.mkdir(parents=True)
    (fleet / "pair.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        fc.identity_guard(config, extra_roots=(str(tmp_path / "close1-fleet"),))
    assert "fleet" in str(error.value)


def test_the_configured_fleet_roots_stop_the_run(config, tmp_path, monkeypatch):
    fleet = tmp_path / "flop-fleet"
    (fleet / "pair-01").mkdir(parents=True)
    monkeypatch.setattr(fc, "FLEET_ROOTS", (fleet,))
    with pytest.raises(SystemExit):
        fc.identity_guard(config)


def test_an_empty_fleet_root_is_not_a_finding(config, tmp_path, monkeypatch):
    empty = tmp_path / "flop-fleet"
    empty.mkdir()
    monkeypatch.setattr(fc, "FLEET_ROOTS", (empty,))
    assert fc.identity_guard(config) == []


def test_the_guard_is_not_switchable_from_the_environment(monkeypatch):
    """The default roots are a constant, not a setting.

    If this ever becomes env driven, the guard is one stray export away from
    doing nothing, and it exists precisely for the night nobody is watching.
    """
    monkeypatch.setenv("FLOP_FLEET_ROOTS", "")
    import importlib

    reloaded = importlib.reload(fc)
    try:
        assert len(reloaded.FLEET_ROOTS) == 2
        assert all(r.name.endswith("-fleet") for r in reloaded.FLEET_ROOTS)
    finally:
        importlib.reload(fc)


def test_a_clean_machine_passes(config):
    assert fc.identity_guard(config) == []


# ------------------------------------------------------------------- budget


def test_zero_ceilings_refuse_to_spend_anything(config, tmp_path):
    config["max_spend_per_session"] = "0"
    client = fc.Client(config, FakeTransport(), ledger=ledger_of(tmp_path))
    with pytest.raises(fc.SpendRefused):
        client.budget_check("1")


def test_an_offer_over_the_session_ceiling_is_refused(config, tmp_path):
    client = fc.Client(config, FakeTransport(), ledger=ledger_of(tmp_path))
    with pytest.raises(fc.SpendRefused) as error:
        client.budget_check("101")
    assert "per session ceiling" in str(error.value)


def test_the_total_ceiling_counts_what_already_settled(config, tmp_path):
    ledger = ledger_of(tmp_path)
    for _ in range(5):
        fc.append_ledger({"amount": "90"}, ledger)
    client = fc.Client(config, FakeTransport(), ledger=ledger)
    assert fc.spent_so_far(ledger) == Decimal("450")
    with pytest.raises(fc.SpendRefused) as error:
        client.budget_check("60")
    assert "already spent" in str(error.value)
    assert client.budget_check("50") == Decimal("50")


# ---------------------------------------------------------------- a session


def happy_transport():
    return FakeTransport(
        {
            "https://example.test/market": {"offers": [{"id": "a", "price": "20"},
                                                       {"id": "b", "price": "5"}]},
            "https://example.test/session_open": {"session_id": "s-1"},
            "https://example.test/session_submit": {"accepted": True},
            "https://example.test/session_settle": {"state": "settled", "amount": "5"},
        }
    )


def test_a_session_runs_through_and_lands_in_the_ledger(config, tmp_path):
    transport = happy_transport()
    client = fc.Client(config, transport, ledger=ledger_of(tmp_path))
    offer = min(client.market(), key=lambda o: Decimal(o["price"]))
    record = client.buy(offer, {"prompt": "hello"})

    assert offer["id"] == "b"  # cheapest, not first
    assert record["stage"] == "settled"
    assert [h["stage"] for h in record["history"]] == [
        "opened", "submitted", "settling", "settled"
    ]
    assert fc.spent_so_far(ledger_of(tmp_path)) == Decimal("5")
    assert fc.open_sessions() == []


def test_the_settlement_response_is_kept_verbatim(config, tmp_path):
    client = fc.Client(config, happy_transport(), ledger=ledger_of(tmp_path))
    client.buy({"id": "b", "price": "5"}, {"prompt": "hi"})
    entry = json.loads(ledger_of(tmp_path).read_text(encoding="utf-8").splitlines()[0])
    assert entry["response"] == {"state": "settled", "amount": "5"}
    assert entry["session_id"] == "s-1"


def test_the_amount_comes_from_the_settlement_not_the_offer(config, tmp_path):
    transport = happy_transport()
    transport.answers["https://example.test/session_settle"] = {
        "state": "settled", "amount": "4.25"
    }
    client = fc.Client(config, transport, ledger=ledger_of(tmp_path))
    record = client.buy({"id": "b", "price": "5"}, {"prompt": "hi"})
    assert record["amount"] == "4.25"


# ------------------------------------------------------------------- crashes


def test_a_crash_after_submitting_leaves_a_resumable_session(config, tmp_path):
    transport = happy_transport()
    transport.fail_after = 2  # open succeeds, submit succeeds, settle dies
    client = fc.Client(config, transport, ledger=ledger_of(tmp_path))
    with pytest.raises(RuntimeError):
        client.buy({"id": "b", "price": "5"}, {"prompt": "hi"})

    pending = fc.open_sessions()
    assert len(pending) == 1
    assert pending[0]["stage"] == "settling"
    assert pending[0]["session_id"] == "s-1"
    assert fc.spent_so_far(ledger_of(tmp_path)) == Decimal(0)


def test_resume_settles_a_session_that_was_left_hanging(config, tmp_path):
    transport = happy_transport()
    transport.fail_after = 2
    client = fc.Client(config, transport, ledger=ledger_of(tmp_path))
    with pytest.raises(RuntimeError):
        client.buy({"id": "b", "price": "5"}, {"prompt": "hi"})

    again = fc.Client(config, happy_transport(), ledger=ledger_of(tmp_path))
    record = again.resume(fc.open_sessions()[0])
    assert record["stage"] == "settled"
    assert fc.spent_so_far(ledger_of(tmp_path)) == Decimal("5")
    assert fc.open_sessions() == []


def test_resume_takes_an_already_settled_session_from_status(config, tmp_path):
    transport = happy_transport()
    transport.fail_after = 2
    client = fc.Client(config, transport, ledger=ledger_of(tmp_path))
    with pytest.raises(RuntimeError):
        client.buy({"id": "b", "price": "5"}, {"prompt": "hi"})

    # The settlement had in fact gone through upstream before the connection died.
    status_only = FakeTransport(
        {"https://example.test/session_status": {"state": "settled", "amount": "5"}}
    )
    again = fc.Client(config, status_only, ledger=ledger_of(tmp_path))
    record = again.resume(fc.open_sessions()[0])
    assert record["stage"] == "settled"
    # and it did not pay a second time
    assert [c["url"] for c in status_only.calls if "settle" in c["url"]] == []


def test_a_session_that_never_got_an_id_is_not_resumed(config, tmp_path):
    transport = happy_transport()
    transport.answers["https://example.test/session_open"] = {}
    client = fc.Client(config, transport, ledger=ledger_of(tmp_path))
    record = client.buy({"id": "b", "price": "5"}, {"prompt": "hi"})
    record["stage"] = "opened"
    fc.save_session(record)

    again = fc.Client(config, FakeTransport(), ledger=ledger_of(tmp_path))
    assert again.resume(fc.open_sessions()[0])["stage"] == "failed"


def test_a_half_written_record_is_never_left_behind(config, tmp_path):
    client = fc.Client(config, happy_transport(), ledger=ledger_of(tmp_path))
    client.buy({"id": "b", "price": "5"}, {"prompt": "hi"})
    for path in fc.STATE_DIR.glob("*"):
        assert path.suffix == ".json"  # no .tmp survives
        json.loads(path.read_text(encoding="utf-8"))


# -------------------------------------------------------------------- config


def test_a_written_config_is_private(tmp_path, monkeypatch):
    target = tmp_path / "flop.json"
    monkeypatch.setattr(fc, "CONFIG_FILE", target)
    fc.write_config(dict(fc.CONFIG_TEMPLATE), target)
    assert target.stat().st_mode & 0o077 == 0


def test_load_fills_in_endpoints_added_after_the_file_was_written(tmp_path):
    target = tmp_path / "flop.json"
    target.write_text(json.dumps({"did": "x", "endpoints": {"faucet": "u"}}), encoding="utf-8")
    config = fc.load_config(target)
    assert config["endpoints"]["faucet"] == "u"
    assert config["endpoints"]["market"] is None
