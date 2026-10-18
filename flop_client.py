#!/usr/bin/env python3
"""flop_client: the buying side of a FLOP testnet agent, written before the testnet.

The airdrop credits an agent for one thing only: compute it purchased in
sessions that settled on chain. Holding tokens earns nothing, and posting in
Technocore rooms earns nothing, which is worth saying plainly because this
repository's other agent does exactly that and will score zero for it.

So this module is not a measuring agent. It is a buyer: link a wallet to the
DID, take testnet funds from the faucet, find a miner, open a session, pay for
real inference, wait for settlement, and keep an independent record of what
settled. Every one of those steps needs an endpoint that FLOP has not published
yet. None are guessed here. Each lives in a config file as null, the client
refuses to call a null endpoint and says which key is missing, and the day the
operator docs go up the work is filling in a JSON file rather than writing a
client.

What is not provisional, and is therefore enforced rather than configured:

  One participant, one score. Wallets under common control count as one
  participant, independent demand only, and fraud forfeits. The multi key
  pattern that was legal in the Close Call contest is disqualifying here, so
  this client refuses to start when it can see a second identity, and there is
  no code path that would drive one.

  A paid session that is lost is money spent for no score. The session is a
  state machine written to disk before each network call, not after, so a crash
  between paying and settling is recoverable and shows up as an open session
  rather than as silence.

Usage:
    python flop_client.py status                  # config, identity, wallet, ledger
    python flop_client.py probe                   # which endpoints exist yet
    python flop_client.py faucet                  # claim testnet funds
    python flop_client.py buy --prompt-file q.txt # one session, end to end
    python flop_client.py resume                  # finish sessions left open
    python flop_client.py ledger                  # what has actually settled
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STARTER_DIR = Path(
    os.environ.get("TECHNOCORE_STARTER_DIR", Path.home() / "technocore-did-starter")
).expanduser()
sys.path.insert(0, str(STARTER_DIR))

try:
    import technocore_agent as tc
except ImportError:  # pragma: no cover - environment problem, not a logic one
    tc = None

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

TIMEOUT_SECONDS = float(os.environ.get("FLOP_TIMEOUT", "30"))

# Directories whose existence means a second set of keys is still on this
# machine. Deliberately not configurable from the environment: a guard that an
# env var can switch off is a guard that gets switched off on the one night it
# matters. Tests replace it directly, which is the only thing that should.
FLEET_ROOTS = (
    Path.home() / ".technocore/close1-fleet",
    Path.home() / ".technocore/flop-fleet",
)

# Every network call this client can make, and the config key that names its
# URL. Nothing here has a default: FLOP has published no endpoint for any of
# them, and a plausible looking guess is worse than a blank, because a blank
# fails loudly on the first run and a guess fails quietly on the hundredth.
ENDPOINTS = {
    "wallet_link": "POST the DID and the wallet address, bind them for scoring",
    "faucet": "POST the wallet address, receive testnet FLOP",
    "balance": "GET the wallet balance",
    "market": "GET the miners currently offering inference",
    "session_open": "POST a chosen offer, receive a session id and a price",
    "session_submit": "POST the work to run in an open session",
    "session_settle": "POST or GET the settlement that records the spend on chain",
    "session_status": "GET one session's current state",
}

# The only two documents confirmed to exist today. probe reads these rather
# than inventing paths, because a 404 on a path nobody published tells us
# nothing, while a change in these two is the actual signal that the operator
# surface has landed.
DISCOVERY = (
    "https://flop.finance/llms.txt",
    "https://flop.finance/testnet/",
)

CONFIG_TEMPLATE = {
    "did": None,
    "wallet_address": None,
    "identity_path": str(STARTER_DIR / "identity.pem"),
    "passphrase_file": str(Path.home() / ".technocore/passphrase"),
    "endpoints": {name: None for name in ENDPOINTS},
    "max_spend_per_session": "0",
    "max_spend_total": "0",
}


class NotPublishedYet(RuntimeError):
    """Raised when a step needs an endpoint FLOP has not announced."""


class SpendRefused(RuntimeError):
    """Raised when a session would cost more than the operator allowed."""


# ---------------------------------------------------------------- config


def load_config(path: Path = CONFIG_FILE) -> dict:
    if not path.exists():
        raise SystemExit(
            f"no config at {path}. Create it with:\n"
            f"  python {Path(__file__).name} init"
        )
    config = json.loads(path.read_text(encoding="utf-8"))
    endpoints = config.setdefault("endpoints", {})
    for name in ENDPOINTS:
        endpoints.setdefault(name, None)
    return config


def write_config(config: dict, path: Path = CONFIG_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def endpoint(config: dict, name: str) -> str:
    """The URL for one step, or a refusal that names what is missing.

    This is the single place the unpublished surface is handled. Every caller
    goes through it, so there is exactly one message to update when FLOP
    publishes, and no step can quietly skip the check.
    """
    if name not in ENDPOINTS:
        raise KeyError(f"unknown endpoint {name!r}")
    url = (config.get("endpoints") or {}).get(name)
    if not url:
        raise NotPublishedYet(
            f"endpoint {name!r} is not set in {CONFIG_FILE}.\n"
            f"  it should {ENDPOINTS[name]}.\n"
            f"  FLOP has not published it yet; set it there once the operator docs name it."
        )
    return url


# ------------------------------------------------- one participant, one score


def identity_guard(config: dict, extra_roots: tuple = ()) -> list:
    """Refuse to run when a second identity is reachable from this machine.

    The rule is "wallets under common control count as one participant", and
    the penalty is forfeiting, not a lower score. In the Close Call contest the
    same operator ran thirteen keys because the rules there allowed it outright.
    Nothing stops that habit carrying over except a check that fails first, so
    this one looks for the directories that habit left behind and for any second
    private key beside the configured one.

    It returns the list of what it found so a caller can report it. It raises
    only on a finding, never on a clean machine.
    """
    configured = Path(config["identity_path"]).expanduser().resolve()
    found = []

    roots = FLEET_ROOTS + tuple(Path(r).expanduser() for r in extra_roots)
    for root in roots:
        if root.exists() and any(root.iterdir()):
            found.append(f"a key fleet directory still exists at {root}")

    if configured.parent.exists():
        siblings = sorted(
            p for p in configured.parent.glob("*.pem") if p.resolve() != configured
        )
        for sibling in siblings:
            found.append(f"a second identity sits beside the configured one: {sibling}")

    if found:
        raise SystemExit(
            "refusing to run: this machine holds more than one identity.\n  "
            + "\n  ".join(found)
            + "\n\nFLOP scores one participant per operator and forfeits on common\n"
            "control, so a second key here is a risk to the score of the first.\n"
            "Move the others off this machine, then run again."
        )
    return found


# ---------------------------------------------------------------- transport


class Transport:
    """Every outbound call, in one place, so a test can replace all of them.

    Signing is not done here. The DID signature on a request body, if FLOP
    requires one, belongs to technocore_agent, and this client imports that
    rather than growing a second implementation of the payload format.
    """

    def __init__(self, timeout: float = TIMEOUT_SECONDS, dry_run: bool = False):
        self.timeout = timeout
        self.dry_run = dry_run
        self.calls = []

    def call(self, method: str, url: str, body: dict = None) -> dict:
        self.calls.append({"method": method, "url": url, "body": body})
        if self.dry_run:
            return {"dry_run": True, "method": method, "url": url, "body": body}
        data = None
        headers = {"accept": "application/json", "user-agent": "flop-client/0.1"}
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["content-type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"{method} {url} returned {error.code}: {detail}") from error
        except URLError as error:
            raise RuntimeError(f"{method} {url} could not be reached: {error.reason}") from error
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"{method} {url} returned a non-JSON response: {raw[:200]!r}")


# ---------------------------------------------------------------- sessions

# The order a session moves through. Written to disk before the call that would
# advance it, so the recorded state is always the last thing known to be true
# rather than the last thing hoped for.
STAGES = ("opened", "submitted", "settling", "settled", "failed")


def session_path(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}.json"


def save_session(record: dict) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    path = session_path(record["local_id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)  # atomic, so a crash mid-write never leaves half a record
    return path


def open_sessions() -> list:
    if not STATE_DIR.exists():
        return []
    records = []
    for path in sorted(STATE_DIR.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("stage") not in ("settled", "failed"):
            records.append(record)
    return records


def spent_so_far(ledger: Path = None) -> Decimal:
    path = ledger or LEDGER
    if not path.exists():
        return Decimal(0)
    total = Decimal(0)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        total += Decimal(str(json.loads(line).get("amount", "0")))
    return total


def append_ledger(record: dict, ledger: Path = None) -> Path:
    """Our own record of what settled, kept beside the pulse receipts.

    The score is computed from settled spend. We learned on this service what
    it costs to let someone else's ring be the only record of your history, so
    the spend that is supposed to earn the airdrop gets an independent copy in
    a git working tree, with the settlement response verbatim.
    """
    path = ledger or LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


class Client:
    def __init__(self, config: dict, transport: Transport = None, ledger: Path = None):
        self.config = config
        self.transport = transport or Transport()
        self.ledger = ledger or LEDGER

    # -- the simple calls

    def link_wallet(self) -> dict:
        url = endpoint(self.config, "wallet_link")
        return self.transport.call(
            "POST", url, {"did": self.config["did"], "wallet": self.config["wallet_address"]}
        )

    def faucet(self) -> dict:
        url = endpoint(self.config, "faucet")
        return self.transport.call("POST", url, {"wallet": self.config["wallet_address"]})

    def balance(self) -> dict:
        url = endpoint(self.config, "balance")
        return self.transport.call("GET", f"{url}?wallet={self.config['wallet_address']}")

    def market(self) -> list:
        url = endpoint(self.config, "market")
        data = self.transport.call("GET", url)
        offers = data.get("offers", data) if isinstance(data, dict) else data
        return offers if isinstance(offers, list) else []

    # -- the one that spends money

    def budget_check(self, price) -> Decimal:
        """Refuse a session that breaks either ceiling, before it opens.

        Two ceilings and not one, because the failure modes differ. A single
        overpriced offer is a bad trade; a slow drain across many cheap sessions
        is the one that empties a faucet balance while nobody is watching the
        total.
        """
        price = Decimal(str(price))
        per = Decimal(str(self.config.get("max_spend_per_session", "0")))
        total = Decimal(str(self.config.get("max_spend_total", "0")))
        if per <= 0 or total <= 0:
            raise SpendRefused(
                "max_spend_per_session and max_spend_total are both zero in the config.\n"
                "Set them deliberately before this client is allowed to spend anything."
            )
        if price > per:
            raise SpendRefused(f"offer costs {price}, over the per session ceiling of {per}")
        already = spent_so_far(self.ledger)
        if already + price > total:
            raise SpendRefused(
                f"offer costs {price}, and {already} is already spent against a total of {total}"
            )
        return price

    def buy(self, offer: dict, work: dict) -> dict:
        """One session from open to settled, recorded at every step.

        The order matters. The record is written before each call, so if the
        process dies the next run knows a session may exist upstream and goes
        looking for it instead of opening a second one and paying twice.
        """
        record = {
            "local_id": uuid.uuid4().hex[:16],
            "stage": "opening",
            "did": self.config["did"],
            "wallet": self.config["wallet_address"],
            "offer": offer,
            "started": datetime.now(timezone.utc).isoformat(),
            "history": [],
        }
        price = self.budget_check(offer.get("price", "0"))
        record["price"] = str(price)
        save_session(record)

        opened = self.transport.call(
            "POST",
            endpoint(self.config, "session_open"),
            {"did": record["did"], "wallet": record["wallet"], "offer": offer.get("id")},
        )
        record["session_id"] = opened.get("session_id") or opened.get("id")
        record["opened"] = opened
        self._advance(record, "opened")

        submitted = self.transport.call(
            "POST",
            endpoint(self.config, "session_submit"),
            {"session_id": record["session_id"], "work": work},
        )
        record["submitted"] = submitted
        self._advance(record, "submitted")

        self._advance(record, "settling")
        settled = self.transport.call(
            "POST",
            endpoint(self.config, "session_settle"),
            {"session_id": record["session_id"]},
        )
        return self._finish(record, settled)

    def resume(self, record: dict) -> dict:
        """Finish a session an earlier run left open.

        Without this, a crash after paying is a silent loss: the spend happened,
        the settlement never did, and the score never sees it. With it, the
        worst case is a session that has to be asked about once.
        """
        if not record.get("session_id"):
            # It died before the open call returned, so nothing upstream exists
            # under an id we hold, and opening another one is the safe move.
            record["note"] = "no session_id was ever recorded; nothing to resume"
            self._advance(record, "failed")
            return record
        status = self.transport.call(
            "GET",
            f"{endpoint(self.config, 'session_status')}?session_id={record['session_id']}",
        )
        if status.get("state") == "settled":
            return self._finish(record, status)
        if record["stage"] in ("opened",):
            record["note"] = "opened but never submitted; left for the operator to cancel"
            self._advance(record, "failed")
            return record
        self._advance(record, "settling")
        settled = self.transport.call(
            "POST",
            endpoint(self.config, "session_settle"),
            {"session_id": record["session_id"]},
        )
        return self._finish(record, settled)

    # -- bookkeeping

    def _advance(self, record: dict, stage: str) -> None:
        record["stage"] = stage
        record["history"].append({"stage": stage, "at": datetime.now(timezone.utc).isoformat()})
        save_session(record)

    def _finish(self, record: dict, settled: dict) -> dict:
        record["settled"] = settled
        amount = settled.get("amount", settled.get("paid", record.get("price", "0")))
        record["amount"] = str(amount)
        self._advance(record, "settled")
        append_ledger(
            {
                "local_id": record["local_id"],
                "session_id": record.get("session_id"),
                "did": record["did"],
                "wallet": record["wallet"],
                "amount": record["amount"],
                "settled_at": datetime.now(timezone.utc).isoformat(),
                "response": settled,
            },
            self.ledger,
        )
        return record


# ---------------------------------------------------------------- commands


def cmd_init(args) -> int:
    if CONFIG_FILE.exists() and not args.force:
        print(f"{CONFIG_FILE} already exists; pass --force to overwrite")
        return 1
    config = dict(CONFIG_TEMPLATE)
    if tc is not None:
        identity = Path(config["identity_path"])
        if identity.exists():
            print(f"identity found at {identity}; set 'did' yourself from technocore_agent did")
    write_config(config)
    print(f"wrote {CONFIG_FILE}")
    print("\nFill in, in this order:")
    print("  did              your existing Technocore DID, not a new one")
    print("  wallet_address   once FLOP publishes the wallet format")
    print("  endpoints.*      once the operator docs name them")
    print("  max_spend_*      deliberately, before the first run that spends")
    return 0


def cmd_status(args) -> int:
    config = load_config()
    print(f"config         {CONFIG_FILE}")
    print(f"did            {config.get('did') or 'not set'}")
    print(f"wallet         {config.get('wallet_address') or 'not set'}")
    ready = [k for k, v in (config.get("endpoints") or {}).items() if v]
    print(f"endpoints      {len(ready)} of {len(ENDPOINTS)} set")
    for name in sorted(ENDPOINTS):
        mark = "set" if (config.get("endpoints") or {}).get(name) else "waiting"
        print(f"  {name:<16} {mark}")
    print(f"\nspend ceiling  {config.get('max_spend_per_session')} per session, "
          f"{config.get('max_spend_total')} total")
    print(f"settled spend  {spent_so_far()} across {LEDGER}")
    pending = open_sessions()
    print(f"open sessions  {len(pending)}")
    for record in pending:
        print(f"  {record['local_id']}  {record['stage']}  {record.get('session_id') or 'no id'}")
    try:
        identity_guard(config)
        print("\nidentity       one, as the rules require")
    except SystemExit as error:
        print(f"\n{error}")
        return 1
    return 0


def cmd_probe(args) -> int:
    """Check whether the operator surface has appeared, without guessing paths."""
    transport = Transport()
    for url in DISCOVERY:
        try:
            transport.call("GET", url)
            print(f"{url}  reachable")
        except RuntimeError as error:
            print(f"{url}  {error}")
    config = load_config()
    missing = [k for k, v in (config.get("endpoints") or {}).items() if not v]
    if missing:
        print(f"\nstill waiting on {len(missing)} endpoint(s): {', '.join(sorted(missing))}")
        print("Nothing is guessed here. Fill them in from the operator docs when they land.")
    return 0


def cmd_faucet(args) -> int:
    config = load_config()
    identity_guard(config)
    print(json.dumps(Client(config, Transport(dry_run=args.dry_run)).faucet(), indent=2))
    return 0


def cmd_buy(args) -> int:
    config = load_config()
    identity_guard(config)
    client = Client(config, Transport(dry_run=args.dry_run))
    offers = client.market()
    if not offers:
        print("no offers returned")
        return 1
    offer = min(offers, key=lambda o: Decimal(str(o.get("price", "0"))))
    work = {"prompt": Path(args.prompt_file).read_text(encoding="utf-8")}
    record = client.buy(offer, work)
    print(json.dumps(record, indent=2))
    return 0


def cmd_resume(args) -> int:
    config = load_config()
    identity_guard(config)
    client = Client(config, Transport(dry_run=args.dry_run))
    pending = open_sessions()
    if not pending:
        print("nothing open")
        return 0
    for record in pending:
        print(f"{record['local_id']}: {record['stage']} -> ", end="")
        print(client.resume(record)["stage"])
    return 0


def cmd_ledger(args) -> int:
    if not LEDGER.exists():
        print(f"no ledger yet at {LEDGER}")
        return 0
    lines = [l for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]
    for line in lines:
        entry = json.loads(line)
        print(f"{entry['settled_at']}  {entry['amount']:>12}  {entry.get('session_id')}")
    print(f"\n{len(lines)} settled session(s), {spent_so_far()} spent")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write the config template")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    sub.add_parser("status", help="config, identity, ledger, open sessions").set_defaults(
        func=cmd_status
    )
    sub.add_parser("probe", help="has the operator surface appeared yet").set_defaults(
        func=cmd_probe
    )

    faucet = sub.add_parser("faucet", help="claim testnet funds")
    faucet.add_argument("--dry-run", action="store_true")
    faucet.set_defaults(func=cmd_faucet)

    buy = sub.add_parser("buy", help="one session, open to settled")
    buy.add_argument("--prompt-file", required=True)
    buy.add_argument("--dry-run", action="store_true")
    buy.set_defaults(func=cmd_buy)

    resume = sub.add_parser("resume", help="finish sessions an earlier run left open")
    resume.add_argument("--dry-run", action="store_true")
    resume.set_defaults(func=cmd_resume)

    sub.add_parser("ledger", help="what has actually settled").set_defaults(func=cmd_ledger)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except NotPublishedYet as error:
        print(f"\n{error}", file=sys.stderr)
        return 2
    except SpendRefused as error:
        print(f"\nrefused: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
