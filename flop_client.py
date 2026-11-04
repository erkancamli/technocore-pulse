#!/usr/bin/env python3
"""flop_client: buy inference on FLOP through the official CLI, with guardrails.

The first version of this file guessed the shape of the network: eight REST
endpoints, a wallet linked to a did:key, a POST to open a session. On 3
November 2026 the devnet opened and none of that was right. FLOP is a
Substrate chain with SS58 accounts, an npm CLI (`@flop-labs/cli`, the `flop`
command), on-chain model and channel registries, and payment held in escrow
against a proof. There is no DID anywhere in it, so the Technocore identity
this repository's other agent uses is not the identity here.

What survived the rewrite is the part that was never about FLOP's API shape:

  One participant, one score. Wallets under common control count as one
  participant and fraud forfeits, so this client works from exactly one
  keystore and refuses to start when it can see a second.

  Nothing spends by default. The escrow ceiling and the running total both
  start at zero, and a zero refuses every session. The total is checked
  against the ledger on disk, not a counter in memory, so a restart does not
  reset it.

  A paid session is never allowed to vanish. The record is written before the
  call that spends, not after, so a crash between paying and settling leaves a
  channel id to recover rather than silence.

  The number that matters gets an independent copy. Settled spend is appended
  to receipts/flop-spend.jsonl with the CLI's own output verbatim.

And one guarantee the devnet taught us to add:

  The chain is pinned. A devnet may be reset, and an RPC override could point
  anywhere. The genesis hash is recorded on first use and every later run
  refuses to act if the chain answers with a different one.

Devnet earns nothing toward the airdrop: no rewards, no weight, and the chain
may be reset. That is exactly why it is the right place to run this.

    python flop_client.py doctor     # CLI, chain, genesis, keystore, ledger
    python flop_client.py models     # what is registered, by hash
    python flop_client.py buy --prompt-file q.txt --dry-run
    python flop_client.py recover    # channels left open by an earlier run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

CONFIG_FILE = Path(
    os.environ.get("FLOP_CONFIG", Path.home() / ".technocore/flop.json")
).expanduser()
STATE_DIR = Path(
    os.environ.get("FLOP_STATE_DIR", Path.home() / ".technocore/flop-sessions")
).expanduser()
REPO_DIR = Path(
    os.environ.get("TECHNOCORE_PULSE_REPO", Path.home() / "technocore-pulse")
).expanduser()
LEDGER = REPO_DIR / "receipts" / "flop-spend.jsonl"

CLI = os.environ.get("FLOP_CLI", "flop")
TIMEOUT_SECONDS = float(os.environ.get("FLOP_TIMEOUT", "180"))

# Directories and files whose existence means a second account is on this
# machine. Deliberately not configurable from the environment: a guard an env
# var can switch off is a guard that gets switched off. Tests replace it.
FLEET_ROOTS = (
    Path.home() / ".technocore/close1-fleet",
    Path.home() / ".technocore/flop-fleet",
    # `flop session run --new-wallet` drops an unencrypted wallet here, one per
    # run. Convenient on a disposable devnet and exactly the habit that makes
    # an operator look like several participants, so one is the limit.
    Path.home() / ".flop/session-wallets",
)

CONFIG_TEMPLATE = {
    "network": "devnet",
    "keystore": None,
    "password_env": "FLOP_KEY_PASSWORD",
    "address": None,
    "genesis_hash": None,
    "model": None,
    # How to buy. "chat" sends a one shot prompt that settles on chain under
    # the signer and needs no miner. "session" goes direct to a named miner
    # and needs that miner's reachable session endpoint, which on devnet on
    # 3 November 2026 none of the three active miners published, so chat is
    # the only path that actually works today.
    "buy_via": "chat",
    "miner": None,
    "endpoint": None,
    "max_flop_per_session": "0",
    "max_flop_total": "0",
    "max_tokens": 256,
}


class CliError(RuntimeError):
    """The flop CLI exited non zero, or did not answer with JSON."""


class NotConfigured(RuntimeError):
    """Something the operator has to decide is still unset."""


class SpendRefused(RuntimeError):
    """The session would cost more than the operator allowed."""


class ChainMismatch(RuntimeError):
    """The chain answered with a genesis hash we have not agreed to."""


# ---------------------------------------------------------------- the CLI


def error_detail(stdout: str, stderr: str) -> str:
    """What actually went wrong, out of a failed CLI run.

    On success the CLI puts its JSON on stdout. On failure it puts a JSON
    error object on STDERR, leaves stdout empty, and then appends an RPC
    disconnect line, so neither "parse stdout" nor "take the last line of
    stderr" finds the message: the first gets nothing and the second gets `}`.
    Found when a real faucet call failed and the client reported
    `faucet request 5Cry... exited 1: }`, which says nothing at all.
    """
    for text in (stderr or "", stdout or ""):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            body = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            continue
        if not isinstance(body, dict):
            continue
        parts = [str(body[k]) for k in ("code", "message") if body.get(k)]
        if body.get("next"):
            parts.append(f"next: {body['next']}")
        if body.get("retryable") is not None:
            parts.append(f"retryable={body['retryable']}")
        if parts:
            return " | ".join(parts)
    lines = [
        line.strip()
        for line in ((stderr or "") + "\n" + (stdout or "")).splitlines()
        if line.strip() and line.strip() not in "{}[]"
    ]
    return " / ".join(lines[-3:]) if lines else "no output"


class Cli:
    """Every call to the flop binary, in one place, so a test can replace it.

    The CLI prints its JSON on stdout and its RPC chatter on stderr, including
    a disconnect line after a successful command, so only stdout is parsed and
    stderr is kept for error messages.
    """

    def __init__(self, network: str = "devnet", binary: str = CLI,
                 timeout: float = TIMEOUT_SECONDS, dry_run: bool = False):
        self.network = network
        self.binary = binary
        self.timeout = timeout
        self.dry_run = dry_run
        self.calls = []

    def argv(self, args: list, auth: dict = None) -> list:
        argv = [self.binary, "--network", self.network, "--output", "json"]
        if auth:
            argv += ["--keystore", auth["keystore"],
                     "--password-env", auth["password_env"]]
        return argv + list(args)

    def version(self) -> str:
        """The CLI's own version, which it prints as a bare string.

        Not routed through run(): `--version` ignores `--output json` and
        answers `0.4.0`, so parsing it as JSON fails. Found by running doctor
        against the real binary rather than by reading the help text.
        """
        return self.text(["--version"])

    def text(self, args: list) -> str:
        """A command whose answer is not JSON."""
        argv = [self.binary] + list(args)
        self.calls.append({"argv": argv, "spends": False})
        if self.dry_run:
            return ""
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, timeout=self.timeout
            )
        except FileNotFoundError as error:
            raise CliError(self._install_hint()) from error
        except subprocess.TimeoutExpired as error:
            raise CliError(f"{' '.join(args)} did not finish") from error
        if done.returncode != 0:
            raise CliError(f"{' '.join(args)} exited {done.returncode}")
        return (done.stdout or "").strip()

    def _install_hint(self) -> str:
        return (
            f"{self.binary} is not on PATH. Install it with "
            "`npm install -g @flop-labs/cli@0.4.0` (Node 22 or later), "
            "or set FLOP_CLI to its path."
        )

    def run(self, args: list, auth: dict = None, spends: bool = False) -> dict:
        argv = self.argv(args, auth)
        self.calls.append({"argv": argv, "spends": spends})
        if self.dry_run:
            return {"dry_run": True, "argv": argv}
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, timeout=self.timeout
            )
        except FileNotFoundError as error:
            raise CliError(self._install_hint()) from error
        except subprocess.TimeoutExpired as error:
            # A spending call that times out may still have been accepted
            # upstream, so this is never treated as "it did not happen".
            raise CliError(
                f"{' '.join(args)} did not finish within {self.timeout:.0f}s"
                + (". It may have spent; run recover before retrying." if spends else "")
            ) from error
        if done.returncode != 0:
            raise CliError(
                f"{' '.join(args)} exited {done.returncode}: "
                + error_detail(done.stdout, done.stderr)
            )
        out = (done.stdout or "").strip()
        if not out:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError as error:
            raise CliError(
                f"{' '.join(args)} did not answer with JSON: {out[:200]!r}"
            ) from error


# ---------------------------------------------------------------- config


def load_config(path: Path = None) -> dict:
    target = path or CONFIG_FILE
    if not target.exists():
        raise SystemExit(
            f"no config at {target}. Create it with:\n"
            f"  python {Path(__file__).name} init"
        )
    config = json.loads(target.read_text(encoding="utf-8"))
    for key, value in CONFIG_TEMPLATE.items():
        config.setdefault(key, value)
    return config


def write_config(config: dict, path: Path = None) -> Path:
    target = path or CONFIG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(target, 0o600)
    return target


def required(config: dict, *keys) -> list:
    """The values the operator still has to set, by name."""
    return [k for k in keys if not config.get(k)]


def auth_of(config: dict) -> dict:
    missing = required(config, "keystore", "password_env")
    if missing:
        raise NotConfigured(
            "set " + " and ".join(missing) + f" in {CONFIG_FILE}.\n"
            "  keystore is the encrypted Polkadot JSON keystore for the one\n"
            "  account this agent uses; password_env names the environment\n"
            "  variable holding its password, never the password itself."
        )
    return {"keystore": str(Path(config["keystore"]).expanduser()),
            "password_env": config["password_env"]}


# ------------------------------------------------- one participant, one score


def identity_guard(config: dict, extra_roots: tuple = ()) -> list:
    """Refuse to run when a second account is reachable from this machine."""
    found = []
    for root in FLEET_ROOTS + tuple(Path(r).expanduser() for r in extra_roots):
        if root.exists() and any(root.iterdir()):
            entries = sorted(p.name for p in root.iterdir())
            found.append(f"{root} holds {len(entries)} entr(ies): "
                         + ", ".join(entries[:4]))

    keystore = config.get("keystore")
    if keystore:
        configured = Path(keystore).expanduser()
        if configured.parent.exists():
            siblings = sorted(
                p for p in configured.parent.glob("*.json")
                if p.name != configured.name
            )
            for sibling in siblings:
                if looks_like_keystore(sibling):
                    found.append(f"a second keystore sits beside the configured "
                                 f"one: {sibling}")

    if found:
        raise SystemExit(
            "refusing to run: this machine holds more than one FLOP account.\n  "
            + "\n  ".join(found)
            + "\n\nFLOP counts wallets under common control as one participant and\n"
            "forfeits on fraud, so a second account here is a risk to the first.\n"
            "Move the others off this machine, then run again."
        )
    return found


def looks_like_keystore(path: Path) -> bool:
    """A Polkadot JSON keystore, rather than any other JSON file.

    Checked by content and not by name, because a config file called
    wallet.json next to the keystore would otherwise stop the run for no
    reason, and because the check that cries wolf gets switched off.
    """
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(body, dict):
        return False
    return "encoded" in body and "encoding" in body and "address" in body


# ---------------------------------------------------------------- the chain


def chain_status(cli: Cli) -> dict:
    return cli.run(["network", "status"])


def pin_genesis(config: dict, status: dict, path: Path = None) -> str:
    """Record the chain we agreed to, or refuse if it has changed.

    The devnet is disposable and may be reset, and `--rpc` could point
    anywhere. The CLI already compares the faucet's genesis with the RPC's; we
    compare both against the one this config was set up for, so a reset chain
    is an error the operator sees rather than a ledger that quietly describes
    two different networks.
    """
    rpc = status.get("rpc") or {}
    live = rpc.get("genesis_hash")
    if not live:
        raise ChainMismatch("the chain did not report a genesis hash")
    faucet = status.get("faucet") or {}
    if faucet and faucet.get("matches_rpc") is False:
        raise ChainMismatch(
            f"the faucet at {faucet.get('url')} is on a different chain from "
            f"the RPC at {rpc.get('url')}; do not fund from it"
        )
    pinned = config.get("genesis_hash")
    if not pinned:
        config["genesis_hash"] = live
        write_config(config, path)
        return live
    if pinned != live:
        raise ChainMismatch(
            f"this config was set up for genesis {pinned}\n"
            f"  the chain at {rpc.get('url')} answers {live}\n"
            "  the devnet may have been reset. Start a fresh config and a fresh\n"
            "  ledger rather than mixing two chains in one record."
        )
    return live


# ------------------------------------------------------------------ models

# The model registry is readable but its names arrive corrupted. Verified
# against devnet on 3 November 2026 with @flop-labs/cli 0.4.0: the on-chain
# name `Qwen/Qwen3-8B` is printed as the characters 0,0,5,1,7,7,6,5,6,0,...
# which is what you get by taking the hex string "0x5177656e2f..." and mapping
# each character with parseInt(c, 10) || 0 before String.fromCharCode. So the
# hex digits 0 to 9 survive, every letter a to f becomes 0, and the "0x" prefix
# becomes two NUL characters.
#
# That is lossy: a byte whose low nibble was a letter cannot be recovered, so
# `n`, `/`, `.` and `-` all come back as something else. This repairs what can
# be repaired and marks the rest rather than guessing, and the client never
# sends a repaired name anywhere: the model slug a session uses comes from the
# config, set by a person who read it.
AMBIGUOUS = "?"


def repair_model_name(broken: str) -> dict:
    """Undo as much of the CLI's name corruption as is recoverable."""
    digits = [ord(c) for c in broken]
    if len(digits) >= 2 and digits[0] == 0 and digits[1] == 0:
        digits = digits[2:]  # the "0x" prefix, both characters mapped to zero
    if len(digits) % 2:
        digits.append(0)
    out, lost = [], 0
    for high, low in zip(digits[::2], digits[1::2]):
        if high == 0 and low == 0:
            out.append(AMBIGUOUS)
            lost += 1
            continue
        byte = high * 16 + low
        if low == 0 and not (32 <= byte < 127 and chr(byte).isalnum()):
            # The low nibble may have been a letter; the character is not
            # recoverable from this output alone.
            out.append(AMBIGUOUS)
            lost += 1
            continue
        out.append(chr(byte) if 32 <= byte < 127 else AMBIGUOUS)
        if not 32 <= byte < 127:
            lost += 1
    return {"best_effort": "".join(out), "unrecoverable": lost,
             "corrupted": lost > 0 or broken[:1] == "\x00"}


def list_models(cli: Cli) -> list:
    data = cli.run(["model", "list"])
    models = data.get("models") or []
    for entry in models:
        entry["name_repair"] = repair_model_name(entry.get("name") or "")
    return models


# ---------------------------------------------------------------- the money


def spent_so_far(ledger: Path = None) -> Decimal:
    path = ledger or LEDGER
    if not path.exists():
        return Decimal(0)
    total = Decimal(0)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        total += Decimal(str(json.loads(line).get("amount") or "0"))
    return total


def append_ledger(record: dict, ledger: Path = None) -> Path:
    path = ledger or LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


# -------------------------------------------------------------- the session

STAGES = ("opening", "spending", "settled", "failed", "dry_run")


def session_path(local_id: str) -> Path:
    return STATE_DIR / f"{local_id}.json"


def save_session(record: dict) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    path = session_path(record["local_id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def open_sessions() -> list:
    if not STATE_DIR.exists():
        return []
    records = []
    for path in sorted(STATE_DIR.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if record.get("stage") not in ("settled", "failed", "dry_run"):
            records.append(record)
    return records


CHANNEL_RE = re.compile(r"\b(?:channel[_ ]?id|channel)\D{0,4}([0-9a-fx]{4,})", re.I)


def channel_id_of(payload) -> str:
    """The channel id, from wherever this build of the CLI put it.

    The JSON shape of `session run` is not documented and has moved between
    builds, so the id is looked for in the obvious fields first and then in the
    text of the whole payload. The id is the only handle recovery has, which is
    why this is worth being untidy about.
    """
    if isinstance(payload, dict):
        for key in ("channel_id", "channelId", "channel"):
            value = payload.get(key)
            if isinstance(value, (str, int)):
                return str(value)
            if isinstance(value, dict):
                for inner in ("id", "channel_id", "channelId"):
                    if value.get(inner):
                        return str(value[inner])
    match = CHANNEL_RE.search(json.dumps(payload, ensure_ascii=False))
    return match.group(1) if match else ""


def paid_amount(payload, fallback: str) -> str:
    """What the settlement says was paid, or the escrow we authorised.

    Cooperative settlement pays the reserved escrow, so the ceiling is the
    honest fallback when the payload does not name an amount. It is never
    smaller than what was actually paid, which is the safe direction for a
    running total to err in.
    """
    if isinstance(payload, dict):
        for key in ("miner_paid", "minerPaid", "paid", "amount", "settled_amount"):
            if payload.get(key) not in (None, ""):
                return str(payload[key])
        for value in payload.values():
            if isinstance(value, dict):
                found = paid_amount(value, "")
                if found:
                    return found
    return fallback


class Client:
    def __init__(self, config: dict, cli: Cli = None, ledger: Path = None):
        self.config = config
        self.cli = cli or Cli(network=config.get("network", "devnet"))
        self.ledger = ledger or LEDGER

    # -- reads

    def status(self) -> dict:
        return chain_status(self.cli)

    def balance(self) -> dict:
        if not self.config.get("address"):
            raise NotConfigured(f"set address in {CONFIG_FILE}")
        return self.cli.run(["balance", self.config["address"]])

    def faucet(self, amount: str = None) -> dict:
        if not self.config.get("address"):
            raise NotConfigured(f"set address in {CONFIG_FILE}")
        args = ["faucet", "request", self.config["address"]]
        if amount:
            args += ["--amount", str(amount)]
        return self.cli.run(args)

    def channels(self) -> list:
        data = self.cli.run(["channel", "list"])
        return data.get("channels", data) if isinstance(data, dict) else data

    # -- the one that spends

    def budget_check(self, escrow) -> Decimal:
        escrow = Decimal(str(escrow))
        per = Decimal(str(self.config.get("max_flop_per_session") or "0"))
        total = Decimal(str(self.config.get("max_flop_total") or "0"))
        if per <= 0 or total <= 0:
            raise SpendRefused(
                "max_flop_per_session and max_flop_total are zero in the config.\n"
                "Set them deliberately before this client may spend anything."
            )
        if escrow > per:
            raise SpendRefused(
                f"escrow {escrow} is over the per session ceiling of {per}"
            )
        already = spent_so_far(self.ledger)
        if already + escrow > total:
            raise SpendRefused(
                f"escrow {escrow} plus {already} already spent would pass the "
                f"total of {total}"
            )
        return escrow

    def buy(self, prompt: str, escrow=None) -> dict:
        """One session, recorded before it can cost anything.

        `--max-flop` is the escrow ceiling and cooperative settlement pays all
        of it, so it is not a limit that saves money when the work is cheap: it
        is the price. That is why the record is written first and the ceiling is
        checked against the ledger rather than guessed per run.
        """
        via = (self.config.get("buy_via") or "chat").lower()
        if via not in ("chat", "session"):
            raise NotConfigured(f"buy_via must be 'chat' or 'session', not {via!r}")
        needed = ("model",) if via == "chat" else ("model", "miner", "endpoint")
        missing = required(self.config, *needed)
        if missing:
            raise NotConfigured(
                "set " + ", ".join(missing) + f" in {CONFIG_FILE}.\n"
                "  model is the exact registered id, read by a person: the CLI\n"
                "  corrupts model names, so it is never taken from `model list`.\n"
                "  The gateway at api.flop.network/v1/models reports the real\n"
                "  name under x-flop.registry_model.\n"
                "  miner and endpoint are needed only for buy_via 'session'."
            )
        escrow = self.budget_check(
            escrow if escrow is not None else self.config["max_flop_per_session"]
        )
        auth = auth_of(self.config)

        record = {
            "local_id": uuid.uuid4().hex[:16],
            "stage": "opening",
            "network": self.config.get("network"),
            "genesis_hash": self.config.get("genesis_hash"),
            "address": self.config.get("address"),
            "model": self.config["model"],
            "miner": self.config["miner"],
            "escrow": str(escrow),
            "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "buy_via": via,
            "history": [],
        }
        self._advance(record, "spending")

        if via == "chat":
            # The prompt is a positional argument here, not --prompt, and
            # --max-flop is documented as a per turn cap rather than a
            # reserved escrow, so what is actually paid may be less. The
            # ledger records whatever the settlement reports and falls back
            # to the cap, which errs high rather than low.
            args = [
                "chat", prompt,
                "--model", self.config["model"],
                "--max-flop", str(escrow),
            ]
        else:
            args = [
                "session", "run",
                "--miner", self.config["miner"],
                "--model", self.config["model"],
                "--prompt", prompt,
                "--max-flop", str(escrow),
                "--max-tokens", str(self.config.get("max_tokens") or 256),
                "--via", self.config.get("via") or "auto",
                "--endpoint", self.config["endpoint"],
            ]
        try:
            result = self.cli.run(args, auth=auth, spends=True)
        except CliError as error:
            record["error"] = str(error)
            record["channel_id"] = ""
            self._advance(record, "spending")  # stays open on purpose
            raise
        record["result"] = result
        record["channel_id"] = channel_id_of(result)
        return self._finish(record, result)

    # -- recovery

    def recover(self, record: dict) -> dict:
        """Deal with a session an earlier run left open.

        The documented recovery is permissionless and lives on the chain, not
        in a status call: finalize a force settled channel after its challenge
        window. Without a channel id there is nothing to finalize, and saying so
        is better than pretending.
        """
        channel = record.get("channel_id") or ""
        if not channel:
            record["note"] = (
                "no channel id was recorded, so there is nothing to finalize. "
                "Check `flop channel list` against this account by hand before "
                "spending again."
            )
            self._advance(record, "failed")
            return record
        try:
            result = self.cli.run(["channel", "finalize", channel],
                                  auth=auth_of(self.config), spends=True)
        except CliError as error:
            record["note"] = f"finalize refused: {error}"
            save_session(record)
            return record
        return self._finish(record, result)

    # -- bookkeeping

    def _advance(self, record: dict, stage: str) -> None:
        record["stage"] = stage
        record["history"].append(
            {"stage": stage, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        )
        save_session(record)

    def _finish(self, record: dict, result) -> dict:
        if isinstance(result, dict) and result.get("dry_run"):
            # A run that spent nothing must not move the running total. The
            # first version booked the escrow as spent on every dry run, which
            # would have made the ceiling refuse real work later for sessions
            # that never happened.
            record["result"] = result
            record["amount"] = "0"
            self._advance(record, "dry_run")
            return record
        record["amount"] = paid_amount(result, record["escrow"])
        self._advance(record, "settled")
        append_ledger(
            {
                "local_id": record["local_id"],
                "channel_id": record.get("channel_id"),
                "network": record.get("network"),
                "genesis_hash": record.get("genesis_hash"),
                "address": record.get("address"),
                "model": record.get("model"),
                "miner": record.get("miner"),
                "escrow": record["escrow"],
                "amount": record["amount"],
                "settled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "result": result,
            },
            self.ledger,
        )
        return record


# ---------------------------------------------------------------- commands


def cmd_init(args) -> int:
    if CONFIG_FILE.exists() and not args.force:
        print(f"{CONFIG_FILE} already exists; pass --force to overwrite")
        return 1
    write_config(dict(CONFIG_TEMPLATE))
    print(f"wrote {CONFIG_FILE}\n")
    print("Fill in, in this order:")
    print("  keystore              one encrypted Polkadot JSON keystore, and only one")
    print("  address               its SS58 address")
    print("  model, miner, endpoint  from a person who read them, not from model list")
    print("  max_flop_*            deliberately, before the first run that spends")
    print("\ngenesis_hash fills itself on the first doctor run and is then enforced.")
    return 0


def cmd_doctor(args) -> int:
    config = load_config()
    cli = Cli(network=config.get("network", "devnet"))
    print(f"config      {CONFIG_FILE}")
    print(f"network     {config.get('network')}")
    print(f"cli         {CLI}")

    try:
        print(f"cli version {cli.version()}")
    except CliError as error:
        print(f"\n{error}")
        return 2
    ok = True

    try:
        status = chain_status(cli)
        rpc = status.get("rpc") or {}
        print(f"rpc         {rpc.get('url')}  reachable={rpc.get('reachable')}  "
              f"chain={rpc.get('chain')!r}")
        faucet = status.get("faucet") or {}
        print(f"faucet      {faucet.get('url')}  reachable={faucet.get('reachable')}  "
              f"matches_rpc={faucet.get('matches_rpc')}")
        print(f"genesis     {pin_genesis(config, status)}")
    except (CliError, ChainMismatch) as error:
        print(f"\nchain: {error}")
        ok = False

    for key in ("keystore", "address", "model", "miner", "endpoint"):
        print(f"{key:<12}{config.get(key) or 'not set'}")
    print(f"ceilings    {config.get('max_flop_per_session')} per session, "
          f"{config.get('max_flop_total')} total")
    print(f"spent       {spent_so_far()} across {LEDGER}")

    pending = open_sessions()
    print(f"open        {len(pending)} session(s)")
    for record in pending:
        print(f"  {record['local_id']}  {record['stage']}  "
              f"channel={record.get('channel_id') or 'none'}")

    try:
        identity_guard(config)
        print("identity    one account, as the rules require")
    except SystemExit as error:
        print(f"\n{error}")
        ok = False
    return 0 if ok else 1


def cmd_models(args) -> int:
    config = load_config()
    models = list_models(Cli(network=config.get("network", "devnet")))
    if not models:
        print("no models registered on this chain")
        return 0
    corrupted = 0
    for entry in models:
        repair = entry["name_repair"]
        corrupted += bool(repair["corrupted"])
        print(f"{entry['hash']}")
        print(f"  name (best effort)  {repair['best_effort']!r}"
              + (f"  [{repair['unrecoverable']} character(s) unrecoverable]"
                 if repair["unrecoverable"] else ""))
        print(f"  size {entry.get('size')}  params {entry.get('parameters_billion')}B"
              f"  context {entry.get('context_window')}")
    if corrupted:
        print(f"\n{corrupted} of {len(models)} name(s) arrived corrupted from the CLI.")
        print("`?` marks a character this output cannot recover. Read the real slug")
        print("from the registry owner or the model card, then set it in the config:")
        print("the client will not send a repaired name anywhere.")
    return 0


def cmd_faucet(args) -> int:
    config = load_config()
    identity_guard(config)
    cli = Cli(network=config.get("network", "devnet"), dry_run=args.dry_run)
    pin_genesis(config, chain_status(Cli(network=config.get("network", "devnet"))))
    print(json.dumps(Client(config, cli).faucet(args.amount), indent=2))
    return 0


def cmd_buy(args) -> int:
    config = load_config()
    identity_guard(config)
    probe = Cli(network=config.get("network", "devnet"))
    pin_genesis(config, chain_status(probe))
    cli = Cli(network=config.get("network", "devnet"), dry_run=args.dry_run)
    prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    record = Client(config, cli).buy(prompt, escrow=args.escrow)
    print(json.dumps(record, indent=2))
    return 0


def cmd_recover(args) -> int:
    config = load_config()
    identity_guard(config)
    client = Client(config, Cli(network=config.get("network", "devnet"),
                                dry_run=args.dry_run))
    pending = open_sessions()
    if not pending:
        print("nothing open")
        return 0
    for record in pending:
        print(f"{record['local_id']}: {record['stage']} -> ", end="")
        print(client.recover(record)["stage"])
    return 0


def cmd_ledger(args) -> int:
    if not LEDGER.exists():
        print(f"no ledger yet at {LEDGER}")
        return 0
    lines = [l for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]
    for line in lines:
        entry = json.loads(line)
        print(f"{entry['settled_at']}  {entry['amount']:>14}  "
              f"{entry.get('model')}  channel={entry.get('channel_id') or 'none'}")
    print(f"\n{len(lines)} settled session(s), {spent_so_far()} FLOP")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write the config template")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    sub.add_parser("doctor", help="CLI, chain, genesis, keystore, ledger").set_defaults(
        func=cmd_doctor
    )
    sub.add_parser("models", help="what is registered on this chain").set_defaults(
        func=cmd_models
    )

    faucet = sub.add_parser("faucet", help="request dev FLOP for the configured address")
    faucet.add_argument("--amount")
    faucet.add_argument("--dry-run", action="store_true")
    faucet.set_defaults(func=cmd_faucet)

    buy = sub.add_parser("buy", help="one session, recorded before it can spend")
    buy.add_argument("--prompt-file", required=True)
    buy.add_argument("--escrow", help="override the per session ceiling, downward")
    buy.add_argument("--dry-run", action="store_true")
    buy.set_defaults(func=cmd_buy)

    recover = sub.add_parser("recover", help="finalize channels left open")
    recover.add_argument("--dry-run", action="store_true")
    recover.set_defaults(func=cmd_recover)

    sub.add_parser("ledger", help="what has actually settled").set_defaults(
        func=cmd_ledger
    )

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except NotConfigured as error:
        print(f"\n{error}", file=sys.stderr)
        return 2
    except SpendRefused as error:
        print(f"\nrefused: {error}", file=sys.stderr)
        return 3
    except ChainMismatch as error:
        print(f"\nchain mismatch: {error}", file=sys.stderr)
        return 4
    except CliError as error:
        print(f"\ncli: {error}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
