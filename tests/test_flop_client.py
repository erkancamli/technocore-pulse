"""Tests for the FLOP buying client.

No network, no chain, no keystore. The CLI is replaced by a scripted stand in,
which is why it exists as one class, and every path that could spend is
exercised against it.

The model name case is not invented. The byte sequence in CORRUPTED_NAME is
what @flop-labs/cli 0.4.0 printed for the devnet model whose real name is
Qwen/Qwen3-8B on 3 November 2026.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

import flop_client as fc


# What the CLI printed, character for character.
CORRUPTED_NAME = (
    "\u0000\u0000\u0005\u0001\u0007\u0007\u0006\u0005\u0006\u0000\u0002\u0000"
    "\u0005\u0001\u0007\u0007\u0006\u0005\u0006\u0000\u0003\u0003\u0002\u0000"
    "\u0003\u0008\u0004\u0002"
)
CORRUPTED_INSTRUCT = (
    "\u0000\u0000\u0005\u0001\u0007\u0007\u0006\u0005\u0006\u0000\u0002\u0000"
    "\u0005\u0001\u0007\u0007\u0006\u0005\u0006\u0000\u0003\u0002\u0002\u0000"
    "\u0003\u0005\u0002\u0000\u0003\u0000\u0002\u0000\u0003\u0005\u0004\u0002"
    "\u0002\u0000\u0004\u0009\u0006\u0000\u0007\u0003\u0007\u0004\u0007\u0002"
    "\u0007\u0005\u0006\u0003\u0007\u0004"
)

GENESIS = "0x99c172c095340ace980631f9123544c6bebd4839f3437e463533422d8be2d631"

STATUS = {
    "network": "devnet",
    "profile_status": "live",
    "rpc": {"url": "wss://rpc.devnet.flop.network", "reachable": True,
            "chain": "FLOP Devnet", "genesis_hash": GENESIS},
    "gateway": "https://api.flop.network",
    "faucet": {"url": "https://faucet.flop.network", "reachable": True,
               "genesis_hash": GENESIS, "matches_rpc": True},
}


class FakeCli(fc.Cli):
    """Answers by first argument, and records what it was asked to do."""

    def __init__(self, answers=None, fail=None):
        super().__init__(network="devnet", binary="flop")
        self.answers = answers or {}
        self.fail = fail or {}

    def run(self, args, auth=None, spends=False):
        key = " ".join(args[:2])
        self.calls.append({"args": list(args), "auth": auth, "spends": spends})
        if key in self.fail:
            raise fc.CliError(self.fail[key])
        return self.answers.get(key, {})


def happy(**extra) -> FakeCli:
    answers = {
        "network status": STATUS,
        "session run": {"channel_id": "0x7f3a", "miner_paid": "0.004",
                        "reply": "hello", "turns": 1},
        "channel finalize": {"finalized": True, "miner_paid": "0.004"},
        "channel list": {"channels": []},
        "faucet request": {"amount": "25", "address": "5Fake"},
        "model list": {"count": 2, "models": [
            {"hash": "0xfab8", "name": CORRUPTED_NAME, "size": "medium",
             "parameters_billion": 8, "context_window": 8192},
            {"hash": "0x1a52", "name": CORRUPTED_INSTRUCT, "size": "small",
             "parameters_billion": 1, "context_window": 4096},
        ]},
    }
    answers.update(extra)
    return FakeCli(answers)


@pytest.fixture
def config(tmp_path):
    keystore = tmp_path / "keys" / "agent.json"
    keystore.parent.mkdir(parents=True)
    keystore.write_text(json.dumps(
        {"encoded": "x", "encoding": {}, "address": "5Fake"}), encoding="utf-8")
    return {
        "network": "devnet",
        "keystore": str(keystore),
        "password_env": "FLOP_KEY_PASSWORD",
        "address": "5Fake",
        "genesis_hash": GENESIS,
        "model": "Qwen/Qwen3-8B",
        "buy_via": "session",  # these cases exercise the direct to miner path
        "miner": "5Miner",
        "endpoint": "http://127.0.0.1:8645",
        "max_flop_per_session": "0.01",
        "max_flop_total": "1",
        "max_tokens": 256,
    }


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "STATE_DIR", tmp_path / "sessions")
    monkeypatch.setattr(fc, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(fc, "CONFIG_FILE", tmp_path / "flop.json")
    monkeypatch.setattr(fc, "FLEET_ROOTS", ())


def ledger_of(tmp_path):
    return tmp_path / "ledger.jsonl"


# ------------------------------------------------------- the model name bug


def test_the_corrupted_devnet_name_repairs_to_what_is_recoverable():
    """Qwen/Qwen3-8B, with the characters the CLI destroyed marked.

    n, / and - each have a hex letter in their low nibble, and the CLI maps
    every letter to zero, so they cannot come back from this output. What is
    recoverable is recovered and the rest is marked, because a plausible guess
    in a model slug would be worse than a gap.
    """
    repair = fc.repair_model_name(CORRUPTED_NAME)
    assert repair["best_effort"] == "Qwe??Qwe?3?8B"  # 13 chars, as Qwen/Qwen3-8B is
    assert repair["unrecoverable"] == 4
    assert repair["corrupted"] is True


def test_the_instruct_model_repairs_the_same_way():
    repair = fc.repair_model_name(CORRUPTED_INSTRUCT)
    # Qwen/Qwen2.5-0.5B-Instruct
    assert repair["best_effort"] == "Qwe??Qwe?2?5?0?5B?I?struct"  # 26 chars
    assert repair["corrupted"] is True


def test_a_clean_name_is_left_alone():
    clean = fc.repair_model_name("")
    assert clean["corrupted"] is False
    assert clean["unrecoverable"] == 0


def test_listing_models_attaches_a_repair_to_each(tmp_path):
    models = fc.list_models(happy())
    assert len(models) == 2
    assert all("name_repair" in m for m in models)
    assert models[0]["hash"] == "0xfab8"


# ------------------------------------------------------------ the chain pin


def test_the_first_run_records_the_genesis(config, tmp_path):
    config["genesis_hash"] = None
    target = tmp_path / "flop.json"
    assert fc.pin_genesis(config, STATUS, target) == GENESIS
    assert json.loads(target.read_text(encoding="utf-8"))["genesis_hash"] == GENESIS


def test_a_reset_chain_is_refused(config):
    other = dict(STATUS, rpc=dict(STATUS["rpc"], genesis_hash="0xdeadbeef"))
    with pytest.raises(fc.ChainMismatch) as error:
        fc.pin_genesis(config, other)
    assert "may have been reset" in str(error.value)


def test_a_faucet_on_another_chain_is_refused(config):
    other = dict(STATUS, faucet=dict(STATUS["faucet"], matches_rpc=False))
    with pytest.raises(fc.ChainMismatch) as error:
        fc.pin_genesis(config, other)
    assert "do not fund from it" in str(error.value)


def test_a_chain_with_no_genesis_is_refused(config):
    with pytest.raises(fc.ChainMismatch):
        fc.pin_genesis(config, {"rpc": {"url": "x"}})


# ------------------------------------------------- one participant, one score


def test_a_second_keystore_beside_the_first_stops_the_run(config):
    sibling = Path(config["keystore"]).with_name("spare.json")
    sibling.write_text(json.dumps(
        {"encoded": "y", "encoding": {}, "address": "5Other"}), encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        fc.identity_guard(config)
    assert "more than one FLOP account" in str(error.value)


def test_an_ordinary_json_file_beside_the_keystore_is_not_an_account(config):
    """The check that cries wolf gets switched off, so it reads the file."""
    Path(config["keystore"]).with_name("settings.json").write_text(
        json.dumps({"theme": "dark"}), encoding="utf-8")
    assert fc.identity_guard(config) == []


def test_the_new_wallet_directory_stops_the_run(config, tmp_path, monkeypatch):
    """`session run --new-wallet` drops one unencrypted wallet per run there."""
    wallets = tmp_path / "session-wallets"
    wallets.mkdir()
    (wallets / "w1.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fc, "FLEET_ROOTS", (wallets,))
    with pytest.raises(SystemExit) as error:
        fc.identity_guard(config)
    assert "session-wallets" in str(error.value)


def test_a_clean_machine_passes(config):
    assert fc.identity_guard(config) == []


# ------------------------------------------------------------------ budget


def test_zero_ceilings_refuse_everything(config, tmp_path):
    config["max_flop_per_session"] = "0"
    client = fc.Client(config, happy(), ledger=ledger_of(tmp_path))
    with pytest.raises(fc.SpendRefused):
        client.budget_check("0.001")


def test_an_escrow_over_the_session_ceiling_is_refused(config, tmp_path):
    client = fc.Client(config, happy(), ledger=ledger_of(tmp_path))
    with pytest.raises(fc.SpendRefused) as error:
        client.budget_check("0.02")
    assert "per session ceiling" in str(error.value)


def test_the_total_counts_what_the_ledger_already_holds(config, tmp_path):
    ledger = ledger_of(tmp_path)
    for _ in range(99):
        fc.append_ledger({"amount": "0.01"}, ledger)
    client = fc.Client(config, happy(), ledger=ledger)
    assert fc.spent_so_far(ledger) == Decimal("0.99")
    assert client.budget_check("0.01") == Decimal("0.01")
    with pytest.raises(fc.SpendRefused):
        client.budget_check("0.011")


# ---------------------------------------------------------------- a session


def test_a_session_runs_and_lands_in_the_ledger(config, tmp_path):
    cli = happy()
    record = fc.Client(config, cli, ledger=ledger_of(tmp_path)).buy("hello")

    assert record["stage"] == "settled"
    assert record["channel_id"] == "0x7f3a"
    assert record["amount"] == "0.004"
    assert fc.spent_so_far(ledger_of(tmp_path)) == Decimal("0.004")
    assert fc.open_sessions() == []


def test_the_escrow_and_the_keystore_reach_the_command_line(config, tmp_path):
    cli = happy()
    fc.Client(config, cli, ledger=ledger_of(tmp_path)).buy("hello")
    run = [c for c in cli.calls if c["args"][:2] == ["session", "run"]][0]
    assert "--max-flop" in run["args"]
    assert run["args"][run["args"].index("--max-flop") + 1] == "0.01"
    assert run["auth"]["password_env"] == "FLOP_KEY_PASSWORD"
    assert run["spends"] is True


def test_the_prompt_is_passed_as_one_argument_not_a_shell_string(config, tmp_path):
    """A prompt with quotes and semicolons is data, not a command."""
    cli = happy()
    nasty = "hello; rm -rf / && echo \"$(whoami)\""
    fc.Client(config, cli, ledger=ledger_of(tmp_path)).buy(nasty)
    run = [c for c in cli.calls if c["args"][:2] == ["session", "run"]][0]
    assert nasty in run["args"]


def test_the_settlement_output_is_kept_verbatim(config, tmp_path):
    fc.Client(config, happy(), ledger=ledger_of(tmp_path)).buy("hello")
    entry = json.loads(ledger_of(tmp_path).read_text(encoding="utf-8").splitlines()[0])
    assert entry["result"]["reply"] == "hello"
    assert entry["genesis_hash"] == GENESIS
    assert entry["escrow"] == "0.01"


def test_an_unconfigured_model_refuses_before_spending(config, tmp_path):
    config["model"] = None
    cli = happy()
    with pytest.raises(fc.NotConfigured) as error:
        fc.Client(config, cli, ledger=ledger_of(tmp_path)).buy("hello")
    assert "model" in str(error.value)
    assert cli.calls == []


def test_a_missing_keystore_refuses_before_spending(config, tmp_path):
    config["keystore"] = None
    cli = happy()
    with pytest.raises(fc.NotConfigured):
        fc.Client(config, cli, ledger=ledger_of(tmp_path)).buy("hello")
    assert cli.calls == []


# ------------------------------------------------------------------ crashes


def test_a_failed_session_run_leaves_the_record_open(config, tmp_path):
    cli = happy()
    cli.fail["session run"] = "channel 0x9ab1 opened, then the RPC dropped"
    with pytest.raises(fc.CliError):
        fc.Client(config, cli, ledger=ledger_of(tmp_path)).buy("hello")

    pending = fc.open_sessions()
    assert len(pending) == 1
    assert pending[0]["stage"] == "spending"
    assert fc.spent_so_far(ledger_of(tmp_path)) == Decimal(0)


def test_recover_finalizes_a_channel_and_books_it(config, tmp_path):
    cli = happy()
    cli.fail["session run"] = "the RPC dropped"
    with pytest.raises(fc.CliError):
        fc.Client(config, cli, ledger=ledger_of(tmp_path)).buy("hello")

    record = fc.open_sessions()[0]
    record["channel_id"] = "0x9ab1"  # read from `flop channel list` by the operator
    fc.save_session(record)

    done = fc.Client(config, happy(), ledger=ledger_of(tmp_path)).recover(record)
    assert done["stage"] == "settled"
    assert fc.spent_so_far(ledger_of(tmp_path)) == Decimal("0.004")


def test_a_session_with_no_channel_id_is_not_silently_closed(config, tmp_path):
    cli = happy()
    cli.fail["session run"] = "refused before any channel existed"
    with pytest.raises(fc.CliError):
        fc.Client(config, cli, ledger=ledger_of(tmp_path)).buy("hello")

    record = fc.Client(config, happy(), ledger=ledger_of(tmp_path)).recover(
        fc.open_sessions()[0]
    )
    assert record["stage"] == "failed"
    assert "nothing to finalize" in record["note"]
    assert "channel list" in record["note"]


def test_a_refused_finalize_leaves_the_session_open_for_another_try(config, tmp_path):
    cli = happy()
    cli.fail["session run"] = "dropped"
    with pytest.raises(fc.CliError):
        fc.Client(config, cli, ledger=ledger_of(tmp_path)).buy("hello")
    record = fc.open_sessions()[0]
    record["channel_id"] = "0x9ab1"
    fc.save_session(record)

    stubborn = happy()
    stubborn.fail["channel finalize"] = "challenge window has not closed"
    again = fc.Client(config, stubborn, ledger=ledger_of(tmp_path)).recover(record)
    assert again["stage"] != "settled"
    assert fc.open_sessions()
    assert "challenge window" in again["note"]


def test_no_half_written_session_record_survives(config, tmp_path):
    fc.Client(config, happy(), ledger=ledger_of(tmp_path)).buy("hello")
    for path in fc.STATE_DIR.glob("*"):
        assert path.suffix == ".json"
        json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------- reading the output


def test_the_channel_id_is_found_in_a_nested_payload():
    assert fc.channel_id_of({"settlement": {"channelId": "0xabc1"}}) == "0xabc1"


def test_the_channel_id_is_found_in_prose_when_the_shape_moves():
    """The JSON shape of session run is undocumented and has moved."""
    assert fc.channel_id_of({"message": "opened channel_id 0x5f2e ok"}) == "0x5f2e"


def test_no_channel_id_reads_as_empty_not_as_a_guess():
    assert fc.channel_id_of({"reply": "hello"}) == ""


def test_the_escrow_is_the_fallback_amount_when_none_is_reported():
    """Cooperative settlement pays the reserved escrow, so this is not low."""
    assert fc.paid_amount({"reply": "hi"}, "0.01") == "0.01"


def test_a_reported_amount_wins_over_the_escrow():
    assert fc.paid_amount({"miner_paid": "0.004"}, "0.01") == "0.004"


# ---------------------------------------------------------- the CLI wrapper


def test_a_missing_binary_says_how_to_install_it(monkeypatch):
    cli = fc.Cli(binary="definitely-not-a-real-binary-xyz")
    with pytest.raises(fc.CliError) as error:
        cli.run(["network", "status"])
    assert "@flop-labs/cli" in str(error.value)


def test_the_network_and_output_mode_are_always_passed():
    argv = fc.Cli(network="devnet").argv(["model", "list"])
    assert argv[1:5] == ["--network", "devnet", "--output", "json"]


def test_the_keystore_is_only_passed_when_there_is_one():
    plain = fc.Cli().argv(["model", "list"])
    assert "--keystore" not in plain
    signed = fc.Cli().argv(["session", "run"],
                           {"keystore": "/k/agent.json", "password_env": "P"})
    assert signed[signed.index("--keystore") + 1] == "/k/agent.json"
    assert signed[signed.index("--password-env") + 1] == "P"


def test_a_dry_run_reports_the_command_and_calls_nothing():
    cli = fc.Cli(dry_run=True)
    out = cli.run(["session", "run", "--prompt", "hi"])
    assert out["dry_run"] is True
    assert "--output" in out["argv"]


def test_the_version_is_not_parsed_as_json(monkeypatch):
    """`--version` ignores --output json and prints a bare string.

    Found by running doctor against the real binary: routing it through run()
    failed with "did not answer with JSON: '0.4.0'".
    """
    cli = fc.Cli()
    monkeypatch.setattr(cli, "text", lambda args: "0.4.0")
    assert cli.version() == "0.4.0"


def test_the_version_call_does_not_pass_the_network_flags():
    cli = fc.Cli(dry_run=True)
    cli.version()
    assert cli.calls[-1]["argv"] == ["flop", "--version"]


# ------------------------------------------------- reading a failed CLI run


REAL_FAUCET_STDERR = """{
  "ok": false,
  "code": "FAUCET_REQUEST_FAILED",
  "message": "Invalid 'address'",
  "retryable": false,
  "next": "check `flop network status` and retry"
}
2026-11-03 17:00:46        RPC-CORE: subscribeRuntimeVersion(): RuntimeVersion:: disconnected from wss://rpc.devnet.flop.network: 1000:: Normal Closure
"""


def test_a_failed_run_reports_the_message_not_the_closing_brace():
    """The first version printed `exited 1: }` for a real faucet failure.

    On failure the CLI writes its JSON error to stderr, leaves stdout empty,
    and appends an RPC disconnect line, so the last line of stderr is noise
    and the line before it is a brace.
    """
    detail = fc.error_detail("", REAL_FAUCET_STDERR)
    assert "FAUCET_REQUEST_FAILED" in detail
    assert "Invalid 'address'" in detail
    assert "retryable=False" in detail
    assert detail.strip() != "}"


def test_the_suggested_next_step_is_kept():
    assert "network status" in fc.error_detail("", REAL_FAUCET_STDERR)


def test_a_json_error_on_stdout_is_read_too():
    detail = fc.error_detail('{"code":"X","message":"y"}', "")
    assert "X" in detail and "y" in detail


def test_plain_text_failure_falls_back_to_the_last_lines():
    detail = fc.error_detail("", "something broke\nand then\nit stopped\n")
    assert "it stopped" in detail


def test_an_empty_failure_says_so():
    assert fc.error_detail("", "") == "no output"


def test_braces_alone_are_not_reported_as_the_reason():
    assert fc.error_detail("", "{\n}\n") == "no output"


# ------------------------------------------------------------ the chat path


def chat_cli() -> FakeCli:
    cli = happy()
    cli.answers["chat hello"] = {"conversation": "c-1", "reply": "hi",
                                 "settlement": {"channelId": "0x41b2"},
                                 "paid": "0.002"}
    return cli


def test_chat_is_the_default_way_to_buy(config, tmp_path):
    """No miner on devnet published a reachable endpoint, so chat is the path."""
    assert fc.CONFIG_TEMPLATE["buy_via"] == "chat"


def test_chat_needs_no_miner_or_endpoint(config, tmp_path):
    config["buy_via"] = "chat"
    config["miner"] = None
    config["endpoint"] = None
    cli = chat_cli()
    record = fc.Client(config, cli, ledger=ledger_of(tmp_path)).buy("hello")
    assert record["stage"] == "settled"
    assert record["buy_via"] == "chat"


def test_the_chat_prompt_is_positional_not_a_flag(config, tmp_path):
    config["buy_via"] = "chat"
    cli = chat_cli()
    fc.Client(config, cli, ledger=ledger_of(tmp_path)).buy("hello")
    args = cli.calls[0]["args"]
    assert args[0] == "chat"
    assert args[1] == "hello"
    assert "--prompt" not in args
    assert args[args.index("--max-flop") + 1] == "0.01"


def test_session_mode_still_requires_a_miner_and_endpoint(config, tmp_path):
    config["buy_via"] = "session"
    config["endpoint"] = None
    cli = happy()
    with pytest.raises(fc.NotConfigured) as error:
        fc.Client(config, cli, ledger=ledger_of(tmp_path)).buy("hello")
    assert "endpoint" in str(error.value)
    assert cli.calls == []


def test_an_unknown_buy_mode_is_refused(config, tmp_path):
    config["buy_via"] = "telepathy"
    with pytest.raises(fc.NotConfigured):
        fc.Client(config, happy(), ledger=ledger_of(tmp_path)).buy("hello")


def test_the_missing_model_message_points_at_the_gateway(config, tmp_path):
    config["model"] = None
    with pytest.raises(fc.NotConfigured) as error:
        fc.Client(config, happy(), ledger=ledger_of(tmp_path)).buy("hello")
    assert "api.flop.network/v1/models" in str(error.value)


def test_a_dry_run_does_not_move_the_running_total(config, tmp_path):
    """A run that spent nothing must not count against the ceiling.

    The first version booked the escrow on every dry run, so rehearsing a
    session would have used up the budget for a real one.
    """
    config["buy_via"] = "chat"
    cli = fc.Cli(dry_run=True)
    record = fc.Client(config, cli, ledger=ledger_of(tmp_path)).buy("hello")
    assert record["stage"] == "dry_run"
    assert record["amount"] == "0"
    assert fc.spent_so_far(ledger_of(tmp_path)) == Decimal(0)
    assert not ledger_of(tmp_path).exists()


def test_a_dry_run_is_not_left_looking_like_an_open_session(config, tmp_path):
    config["buy_via"] = "chat"
    fc.Client(config, fc.Cli(dry_run=True), ledger=ledger_of(tmp_path)).buy("hello")
    assert fc.open_sessions() == []
