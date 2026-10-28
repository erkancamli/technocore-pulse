#!/usr/bin/env python3
"""Watch the FLOP operator pages for the day they stop saying "unavailable".

The buying client has eight endpoints and all eight are null, not because the
client is unfinished but because FLOP has published no value for any of them.
As of 28 October the official operator pages answer every one of chain spec,
genesis, bootnodes, RPC, gateway and model catalog with the same line:
"Unavailable - no verified public value", and enrollment with "Public operator
enrollment is unavailable until the network identity and release are verified".
The testnet has a working target of 3 November, which is a target and not a
date, and a devnet is said to be opening to miners, validators and agents.

Checking that by hand every morning is the wrong shape of work for an agent
that already runs daily. So this module reads the same pages, decides one
thing about each, and remembers the answer: is this page still saying nothing
is available? The day one of them stops, the run says so, loudly, and writes a
timestamped line that survives.

Two deliberate choices about what counts as news.

A page's wording changes constantly and means nothing. A digest change is
recorded and reported quietly. The only loud signal is a page that was closed
and is no longer closed.

A page that fails to load is not an opening. A 404, a timeout or a proxy error
leaves the stored verdict exactly as it was, because the alternative is an
agent that announces the testnet every time the site hiccups.

    python flop_watch.py            # check, print, remember
    python flop_watch.py --quiet    # print only if something changed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STATE_FILE = Path(
    os.environ.get("FLOP_WATCH_STATE", Path.home() / ".technocore/flop-readiness.json")
).expanduser()
REPO_DIR = Path(
    os.environ.get("TECHNOCORE_PULSE_REPO", Path.home() / "technocore-pulse")
).expanduser()
LOG = REPO_DIR / "receipts" / "flop-readiness.jsonl"

TIMEOUT_SECONDS = float(os.environ.get("FLOP_WATCH_TIMEOUT", "30"))

# The pages to read, and the phrases that mean the page is still telling us
# there is nothing to connect to. These phrases are quoted from the pages as
# they read on 28 October 2026; if FLOP rewords them without opening anything,
# the page will flag and a human will look, which is the safe direction for a
# wrong guess to fail in.
CLOSED_MARKERS = (
    "no verified public value",
    "enrollment is unavailable",
    "unavailable until",
    "not yet final",
)

# Words that would have to appear somewhere before any of this is real. They
# are watched on the pages that carry no gate text of their own, because for
# those a wording change is the only thing there is to notice and most wording
# changes are nothing. A new one of these is not nothing.
SIGNAL_WORDS = ("faucet", "rpc", "bootnode", "gateway", "chain spec", "genesis")

# label, url, and whether the page carries gate text. A page that never said
# "unavailable" cannot stop saying it, so reporting it as newly open would be
# a false alarm every single morning. Found by running this against the real
# pages rather than by reasoning about it.
WATCH = (
    ("devnet", "https://ask.flop.finance/networks/devnet", True),
    ("testnet", "https://ask.flop.finance/networks/testnet", True),
    ("operators", "https://ask.flop.finance/operators", True),
    ("testnet-page", "https://flop.finance/testnet/", True),
    ("run-miner", "https://ask.flop.finance/docs/run-miner", False),
    ("llms", "https://flop.finance/llms.txt", False),
)

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def fetch_text(url: str, timeout: float = TIMEOUT_SECONDS) -> str:
    """One page as text. Raises on anything that is not a readable response."""
    request = Request(
        url,
        headers={"accept": "text/html,text/plain", "user-agent": "flop-watch/0.1"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as error:
        raise RuntimeError(f"HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"unreachable: {error.reason}") from error
    return raw.decode("utf-8", "replace")


def plain(html: str) -> str:
    """Tags out, whitespace collapsed, lowercased.

    Crude on purpose. This text is only ever searched for fixed phrases and
    hashed, never shown to anyone and never parsed as a document, so a real
    HTML parser would buy nothing a regex does not already give.
    """
    without_script = re.sub(
        r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I
    )
    return SPACE_RE.sub(" ", TAG_RE.sub(" ", without_script)).strip().lower()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def verdict(text: str) -> dict:
    """Is this page still saying nothing is available, and on what evidence."""
    found = [marker for marker in CLOSED_MARKERS if marker in text]
    return {"closed": bool(found), "markers": found}


def signals(text: str) -> list:
    return [word for word in SIGNAL_WORDS if word in text]


def check(label: str, url: str, gated: bool = True, fetch=fetch_text) -> dict:
    try:
        text = plain(fetch(url))
    except RuntimeError as error:
        return {"label": label, "url": url, "gated": gated, "error": str(error)}
    result = {
        "label": label,
        "url": url,
        "gated": gated,
        "digest": digest(text),
        "bytes": len(text),
        "signals": signals(text),
    }
    result.update(verdict(text))
    return result


def load_state(path: Path = None) -> dict:
    target = path or STATE_FILE
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict, path: Path = None) -> Path:
    target = path or STATE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return target


def append_log(entry: dict, path: Path = None) -> Path:
    """A timestamped line for an event that will matter in hindsight.

    The day the testnet opens is a fact worth being able to point at later,
    and this agent has already learned what it costs to let somebody else's
    record be the only one.
    """
    target = path or LOG
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    return target


def run(fetch=fetch_text, state_path: Path = None, log_path: Path = None) -> dict:
    """Check every page, compare with what was stored, remember the result."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = load_state(state_path)
    checks, opened, reworded, errors, new_signals = [], [], [], [], []

    for label, url, gated in WATCH:
        result = check(label, url, gated, fetch)
        checks.append(result)
        prior = state.get(url, {})
        result["prior_closed"] = prior.get("closed")

        if result.get("error"):
            errors.append(result)
            continue  # a page that did not load changes nothing

        fresh = [s for s in result["signals"] if s not in (prior.get("signals") or [])]
        result["new_signals"] = fresh

        if gated and prior.get("closed") is True and result["closed"] is False:
            opened.append(result)
            prior["opened_at"] = now
        elif fresh and prior.get("digest"):
            new_signals.append(result)
        elif prior.get("digest") and prior["digest"] != result["digest"]:
            reworded.append(result)

        state[url] = {
            "label": label,
            "gated": gated,
            "closed": result["closed"],
            "markers": result["markers"],
            "signals": result["signals"],
            "digest": result["digest"],
            "first_seen": prior.get("first_seen", now),
            "last_checked": now,
            "last_change": now if prior.get("digest") != result["digest"]
            else prior.get("last_change", now),
            "opened_at": prior.get("opened_at"),
        }

    save_state(state, state_path)
    report = {
        "at": now,
        "checks": checks,
        "opened": opened,
        "reworded": reworded,
        "new_signals": new_signals,
        "errors": errors,
        "still_closed": sum(1 for c in checks if c.get("closed")),
        "gated": sum(1 for c in checks if c.get("gated") and not c.get("error")),
        "read": len(checks) - len(errors),
    }
    if opened:
        append_log({"at": now, "event": "page_opened",
                    "pages": [{"label": o["label"], "url": o["url"]} for o in opened]},
                   log_path)
    if new_signals:
        append_log({"at": now, "event": "new_signal_words",
                    "pages": [{"label": s["label"], "url": s["url"],
                               "words": s["new_signals"]} for s in new_signals]},
                   log_path)
    return report


def lines(report: dict) -> list:
    """What a person reads in the morning log, shortest useful form."""
    out = []
    for result in report["checks"]:
        label = f"  {result['label']:<12}"
        if result.get("error"):
            out.append(f"{label} could not read ({result['error']})")
        elif result["closed"]:
            out.append(f"{label} still closed ({result['markers'][0]})")
        elif result.get("prior_closed") is True:
            out.append(f"{label} NO LONGER CLOSED, go and look")
        elif result["gated"]:
            out.append(f"{label} no gate text (first look)")
        else:
            words = ", ".join(result.get("signals") or []) or "none"
            out.append(f"{label} watched for change; signal words: {words}")
    if report["opened"]:
        names = ", ".join(o["label"] for o in report["opened"])
        out.append("")
        out.append(f"CHANGED: {names} stopped saying nothing is available.")
        out.append("Read the page, then fill flop.json and set the spend ceilings.")
    if report["new_signals"]:
        for result in report["new_signals"]:
            words = ", ".join(result["new_signals"])
            out.append(f"\nNEW on {result['label']}: {words}. Worth reading today.")
    if not report["opened"] and not report["new_signals"] and report["reworded"]:
        names = ", ".join(r["label"] for r in report["reworded"])
        out.append(f"\n{len(report['reworded'])} page(s) reworded, nothing opened: {names}")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quiet", action="store_true",
                        help="print nothing unless something changed")
    args = parser.parse_args(argv)
    report = run()
    quiet_day = not (report["opened"] or report["new_signals"] or report["reworded"])
    if args.quiet and quiet_day:
        return 0
    print(f"flop readiness {report['at']}: {report['still_closed']} of "
          f"{report['gated']} gated page(s) still say nothing is available")
    for line in lines(report):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
