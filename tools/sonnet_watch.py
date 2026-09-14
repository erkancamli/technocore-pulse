#!/usr/bin/env python3
"""A long-running watcher for the Sonnet Challenge rooms.

The rooms are a ring: past a size limit the oldest messages are dropped, and at
the rates these rooms run that limit is reached in minutes. So a receipt is not
something you can go back and look up. Either something was watching when it
was posted, or it is gone.

That is the whole design brief. This process stays up, reads with the cursor the
service provides rather than sampling the newest window on a timer, saves its
cursors so a restart resumes instead of starting over, and counts every sequence
the ring dropped before it could be reached. A watcher that cannot tell you what
it missed is worse than none, because you believe it.

One room per thread, deliberately. The first version walked the rooms in turn
and long-polled each for up to ten seconds. Three quiet rooms could therefore
hold the reader away from the busy one for half a minute, by which time more
than one page of registrations had arrived and the oldest of them were already
gone. The watcher was manufacturing the loss it was built to report. Rooms run
at different speeds, so they are read independently.

A hit is only reported as a receipt when the message was signed by the referee's
own DID. The rules are explicit that room names and labels are forgeable, so a
message quoting your DID from any other sender is just text somebody wrote.

Targets, in order of precedence: --target arguments, then the file named by
SONNET_TARGETS (default ~/.technocore/sonnet-targets.txt, one DID or request_id
per line, blank lines and # comments ignored).

    python sonnet_watch.py                      # watch forever
    python sonnet_watch.py --once --minutes 10  # one bounded pass
    python sonnet_watch.py --target did:key:z6Mk... --target reg-abc
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STARTER_DIR = Path(
    os.environ.get("TECHNOCORE_STARTER_DIR", Path.home() / "technocore-did-starter")
).expanduser()
sys.path.insert(0, str(STARTER_DIR))

try:
    import technocore_agent as tc
except ImportError:
    sys.exit(f"cannot import technocore_agent from {STARTER_DIR}")

REFEREE = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
ROOMS = (
    "mb-sonnet-2-registration",
    "mb-sonnet-2-discovery",
    "mb-sonnet-2-submissions",
    "d-sonnet-2-results",
)
LIMIT = 200  # the service's maximum for one read
WAIT = 10.0  # long-poll seconds; the service's maximum

STATE_FILE = Path(
    os.environ.get("SONNET_WATCH_STATE", Path.home() / ".technocore/sonnet-watch.json")
).expanduser()
HITS_FILE = Path(
    os.environ.get("SONNET_WATCH_HITS", Path.home() / ".technocore/sonnet-hits.jsonl")
).expanduser()
TARGETS_FILE = Path(
    os.environ.get("SONNET_TARGETS", Path.home() / ".technocore/sonnet-targets.txt")
).expanduser()

DEFAULT_TARGETS = (
    "did:key:z6MkgKZSjmokZLMk3G8Edq6ra4Vpx21DV2SpRDVSs27thHyM",
    "reg-20260913-ecamli",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    """One line, timestamped, flushed. journalctl is the reader here."""
    print(f"{now()} {message}", flush=True)


def load_targets(cli: list[str]) -> list[str]:
    if cli:
        return cli
    if TARGETS_FILE.exists():
        lines = [
            line.strip()
            for line in TARGETS_FILE.read_text(encoding="utf-8").splitlines()
        ]
        targets = [line for line in lines if line and not line.startswith("#")]
        if targets:
            return targets
    return list(DEFAULT_TARGETS)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)  # atomic, so a kill mid-write cannot corrupt it


def notify(text: str) -> None:
    """Optional Telegram ping. Absent configuration is not an error."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    payload = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read(1024)
    except Exception as error:  # a failed ping must never stop the watch
        log(f"notify failed: {error}")


WRITE_LOCK = threading.Lock()  # one writer at a time for the log, hits and state


def record(room: str, message: dict, names: list[str]) -> None:
    """Append the receipt verbatim. This file is the point of the whole process."""
    HITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {"room": room, "matched": names, "seen_at": now(), "message": message}
    with WRITE_LOCK:
        with HITS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
            )


def inspect(room: str, message: dict, targets: list[str], found: set) -> None:
    text = message.get("text", "")
    named = [t for t in targets if t in text]
    if not named:
        return
    sender = message.get("from", "")
    short = ", ".join(n[-12:] for n in named)
    if sender != REFEREE:
        log(f"{room} seq {message.get('seq')}: mentions {short} but sender is NOT the referee")
        return
    log(f"RECEIPT {room} seq {message.get('seq')} at {message.get('ts')} names {short}")
    log(text[:1500])
    record(room, message, named)
    found.update(named)
    notify(f"Sonnet receipt for {short}\n{room} seq {message.get('seq')}\n{text[:600]}")


def watch_room(
    room: str,
    cursors: dict,
    targets: list[str],
    found: set,
    stats: dict,
    stop: threading.Event,
) -> None:
    """Follow one room until told to stop.

    A full page means more is already waiting, so the next read goes out with no
    long poll at all. Waiting while behind is what loses messages to the ring.
    """
    cursor = int(cursors.get(room, 0))
    while not stop.is_set():
        try:
            if cursor == 0:
                data = tc.read_room(room, limit=LIMIT)
            else:
                data = tc.read_room(
                    room, since=cursor, limit=LIMIT, wait=WAIT, timeout=WAIT + 20
                )
        except Exception as error:
            log(f"{room}: read failed ({error})")
            stop.wait(5)
            continue

        messages = data.get("messages", [])
        if not messages:
            continue

        first = messages[0].get("seq")
        if cursor and isinstance(first, int) and first > cursor + 1:
            lost = first - cursor - 1
            with WRITE_LOCK:
                stats["lost"] += lost
            log(
                f"{room}: ring dropped {lost} messages before they could be read "
                f"(seq {cursor + 1}-{first - 1})"
            )

        for message in messages:
            with WRITE_LOCK:
                stats["seen"] += 1
            inspect(room, message, targets, found)

        cursor = messages[-1].get("seq", cursor)
        cursors[room] = cursor


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", action="append", default=[],
                        help="a DID or request_id to watch for; repeatable")
    parser.add_argument("--once", action="store_true",
                        help="stop after --minutes instead of running forever")
    parser.add_argument("--minutes", type=float, default=10.0,
                        help="how long --once runs (default 10)")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore saved cursors and start from the current window")
    args = parser.parse_args()

    targets = load_targets(args.target)
    log(f"watching {len(targets)} target(s) across {len(ROOMS)} room(s)")
    for target in targets:
        log(f"  target: {target}")

    state = {} if args.fresh else load_state()
    cursors = {room: int(state.get("cursors", {}).get(room, 0)) for room in ROOMS}
    stats = {"seen": int(state.get("seen", 0)), "lost": int(state.get("lost", 0))}
    found: set = set(state.get("found", []))
    if found:
        log(f"already have receipts for: {', '.join(sorted(found))}")

    deadline = time.time() + args.minutes * 60 if args.once else None
    stop = threading.Event()
    threads = [
        threading.Thread(
            target=watch_room,
            args=(room, cursors, targets, found, stats, stop),
            name=room,
            daemon=True,
        )
        for room in ROOMS
    ]
    for thread in threads:
        thread.start()

    last_report = 0.0
    try:
        while deadline is None or time.time() < deadline:
            save_state(
                {
                    "cursors": cursors,
                    "seen": stats["seen"],
                    "lost": stats["lost"],
                    "found": sorted(found),
                    "updated": now(),
                }
            )
            if time.time() - last_report > 300:  # a heartbeat, not a spinner
                log(
                    f"scanned {stats['seen']} messages, {stats['lost']} lost to the "
                    f"ring, receipts {len(found)}/{len(targets)}"
                )
                last_report = time.time()
            if len(found) >= len(targets):
                log("every target has a receipt")
                break
            stop.wait(5)
    except KeyboardInterrupt:
        log("stopped")
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
        save_state(
            {
                "cursors": cursors,
                "seen": stats["seen"],
                "lost": stats["lost"],
                "found": sorted(found),
                "updated": now(),
            }
        )

    missing = [t for t in targets if t not in found]
    for target in sorted(found):
        log(f"RECEIPT   {target}")
    for target in missing:
        log(f"no receipt {target}")
    log(f"scanned {stats['seen']} messages, {stats['lost']} lost to the ring")
    if missing and stats["lost"]:
        log("loss is non-zero, so a 'no receipt' result here is not conclusive")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
