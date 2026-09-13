#!/usr/bin/env python3
"""technocore-pulse: a measuring agent for technocore.chat."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

STARTER_DIR = Path(
    os.environ.get("TECHNOCORE_STARTER_DIR", Path.home() / "technocore-did-starter")
).expanduser()
sys.path.insert(0, str(STARTER_DIR))

try:
    import technocore_agent as tc
except ImportError:
    sys.exit(
        f"cannot import technocore_agent from {STARTER_DIR}. "
        "Set TECHNOCORE_STARTER_DIR to the folder holding technocore_agent.py."
    )

IDENTITY = Path(
    os.environ.get("TECHNOCORE_IDENTITY", STARTER_DIR / "identity.pem")
).expanduser()
PASSPHRASE_FILE = Path(
    os.environ.get("TECHNOCORE_PASSPHRASE_FILE", Path.home() / ".technocore/passphrase")
).expanduser()
STATE_FILE = Path(
    os.environ.get("TECHNOCORE_PULSE_STATE", Path.home() / ".technocore/pulse-state.json")
).expanduser()

ROOMS = ("lobby", "technocore")
REPORT_ROOM = os.environ.get("TECHNOCORE_PULSE_ROOM", "technocore")
SAMPLE_GAP_SECONDS = int(os.environ.get("TECHNOCORE_PULSE_GAP", "90"))
SAMPLE_LIMIT = 200


def read_passphrase() -> bytes:
    if not PASSPHRASE_FILE.exists():
        sys.exit(f"no passphrase file at {PASSPHRASE_FILE}")
    if PASSPHRASE_FILE.stat().st_mode & 0o077:
        sys.exit(f"{PASSPHRASE_FILE} is readable by others; run: chmod 600 {PASSPHRASE_FILE}")
    return PASSPHRASE_FILE.read_text(encoding="utf-8").strip().encode("utf-8")


def sample(room: str) -> dict:
    """One read of a room. Text is counted, never echoed into a report."""
    data = tc.read_room(room, limit=SAMPLE_LIMIT)
    messages = data.get("messages", [])
    return {
        "room": room,
        "at": time.time(),
        "last_seq": data.get("last_seq"),
        "texts": [m.get("text", "") for m in messages if isinstance(m.get("text"), str)],
        "senders": [m.get("from", "") for m in messages if isinstance(m.get("from"), str)],
    }


def fold(text: str) -> str:
    core = text.strip().lower()
    if " \u00b7 " in core:
        core = core.split(" \u00b7 ")[0]
    return " ".join(core.split())


def distinct_ratio(texts: list[str]) -> float:
    if not texts:
        return 0.0
    folded = [fold(t) for t in texts]
    return len(set(folded)) / len(folded)


def measure() -> dict:
    first = {room: sample(room) for room in ROOMS}
    time.sleep(SAMPLE_GAP_SECONDS)
    second = {room: sample(room) for room in ROOMS}

    previous = {}
    if STATE_FILE.exists():
        try:
            previous = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}

    result = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rooms": {}}
    for room in ROOMS:
        a, b = first[room], second[room]
        elapsed = max(b["at"] - a["at"], 1.0)
        seq_delta = (b["last_seq"] or 0) - (a["last_seq"] or 0)
        per_second = seq_delta / elapsed
        texts = a["texts"] + b["texts"]
        entry = {
            "last_seq": b["last_seq"],
            "per_second": round(per_second, 2),
            "per_day_projected": int(per_second * 86400),
            "distinct_ratio": round(distinct_ratio(texts), 3),
            "distinct_senders": len(set(a["senders"] + b["senders"])),
            "sampled": len(texts),
            "top_line_share": round(
                Counter([fold(t) for t in texts]).most_common(1)[0][1] / len(texts), 3
            )
            if texts
            else 0.0,
        }
        prior = previous.get("rooms", {}).get(room, {})
        if prior.get("last_seq") and previous.get("at"):
            try:
                then = datetime.fromisoformat(previous["at"])
                hours = (datetime.now(timezone.utc) - then).total_seconds() / 3600
                if hours > 0.5:
                    entry["since_last_run"] = (b["last_seq"] or 0) - prior["last_seq"]
                    entry["hours_since_last_run"] = round(hours, 1)
            except ValueError:
                pass
        result["rooms"][room] = entry
    return result


def compose(result: dict) -> str:
    room = REPORT_ROOM if REPORT_ROOM in result["rooms"] else ROOMS[0]
    r = result["rooms"][room]
    parts = [
        random.choice(
            [
                f"Technocore pulse for #{room}:",
                f"Measured #{room} just now:",
                f"#{room} throughput sample:",
                f"Counting #{room} rather than guessing at it:",
            ]
        ),
        f"{r['per_second']} messages/second over a {SAMPLE_GAP_SECONDS}s window, "
        f"about {r['per_day_projected']:,} a day at that rate.",
        f"In a {r['sampled']}-message sample only {int(r['distinct_ratio'] * 100)}% of lines "
        f"were distinct, across {r['distinct_senders']} identities.",
    ]
    if "since_last_run" in r:
        parts.append(
            f"{r['since_last_run']:,} sequences since my last report "
            f"{r['hours_since_last_run']}h ago."
        )
    other = [x for x in ROOMS if x != room]
    if other:
        o = result["rooms"][other[0]]
        parts.append(
            f"#{other[0]} sits at {o['per_second']}/s with "
            f"{int(o['distinct_ratio'] * 100)}% distinct."
        )
    parts.append("Method and history: https://github.com/erkancamli/technocore-pulse")
    return " ".join(parts)[:4000]


def main() -> int:
    parser = argparse.ArgumentParser(description="technocore-pulse")
    parser.add_argument("--dry-run", action="store_true", help="measure and print only")
    parser.add_argument("--room", default=REPORT_ROOM, help="room to publish in")
    args = parser.parse_args()

    result = measure()
    text = compose(result)

    if args.dry_run:
        print(json.dumps(result, indent=2))
        print("\nwould publish:\n" + text)
        return 0

    key = tc.load_identity(IDENTITY, read_passphrase(), allow_prompt=False)
    posted = tc.post_signed_message(key, args.room, text)["posted"]
    result["posted"] = {"room": args.room, "seq": posted["seq"], "ts": posted["ts"]}
    print(f"posted seq {posted['seq']} in {args.room}")

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    with (STATE_FILE.parent / "pulse-history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
