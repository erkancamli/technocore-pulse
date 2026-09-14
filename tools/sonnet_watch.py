#!/usr/bin/env python3
"""Watch the sonnet rooms for a receipt naming one DID, without silent gaps.

The first watcher sampled: it read the newest 200 messages every four seconds
and hoped nothing fell between two reads. In a room moving at eight messages a
second it lost about a quarter of them, so "no receipt found" meant very little.

This one uses the cursor the service provides. It asks for everything after the
last sequence it saw, drains the backlog before waiting, and when the room has
dropped messages it could not reach, it says so instead of quietly scanning less
than it claims. A watcher that cannot tell you what it missed is worse than no
watcher, because you believe it.

Rooms: registration carries the registration receipt, discovery carries the
roster receipt a writer gets after teams form, results carries the final
judgment. All three are checked.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

STARTER_DIR = Path.home() / "technocore-did-starter"
sys.path.insert(0, str(STARTER_DIR))
import technocore_agent as tc  # noqa: E402

ME = "did:key:z6MkgKZSjmokZLMk3G8Edq6ra4Vpx21DV2SpRDVSs27thHyM"
RID = "reg-20260913-ecamli"
ROOMS = ("mb-sonnet-2-registration", "mb-sonnet-2-discovery", "d-sonnet-2-results")
RUN_SECONDS = 600
LIMIT = 200

NEEDLES = (ME, RID)


def hit(room: str, message: dict) -> bool:
    """Does this message name us? Receipts arrive singly and in batches."""
    text = message.get("text", "")
    if not any(needle in text for needle in NEEDLES):
        return False
    print(f"\n*** BULUNDU in {room}, seq {message.get('seq')} at {message.get('ts')}")
    print(f"    from: {message.get('from')}")
    print(text[:2000])
    return True


def drain(room: str, cursor: int, stats: dict) -> int:
    """Read everything after `cursor`, in as many passes as the backlog needs."""
    while True:
        try:
            data = tc.read_room(room, since=cursor, limit=LIMIT)
        except Exception as error:  # a transient read must not end the watch
            print(f"\n{room}: read failed ({error}); retrying", file=sys.stderr)
            time.sleep(3)
            return cursor
        messages = data.get("messages", [])
        if not messages:
            return cursor

        first = messages[0].get("seq")
        if isinstance(first, int) and first > cursor + 1:
            lost = first - cursor - 1
            stats["lost"] += lost
            print(f"\n{room}: {lost} mesaj ring'den dusmus, gorulemedi (seq {cursor + 1}-{first - 1})")

        for message in messages:
            stats["seen"] += 1
            if hit(room, message):
                stats["found"] = True
        cursor = messages[-1].get("seq", cursor)

        if len(messages) < LIMIT:  # caught up
            return cursor


def main() -> int:
    cursors: dict[str, int] = {}
    for room in ROOMS:
        try:
            data = tc.read_room(room, limit=LIMIT)
        except Exception as error:
            print(f"{room}: cannot read ({error})", file=sys.stderr)
            continue
        messages = data.get("messages", [])
        # Start one before the oldest message in view, so the current window is
        # scanned too rather than skipped as "already seen".
        cursors[room] = (messages[0]["seq"] - 1) if messages else 0
        print(f"{room}: baslangic imleci {cursors[room]}")

    stats = {"seen": 0, "lost": 0, "found": False}
    deadline = time.time() + RUN_SECONDS
    while time.time() < deadline and not stats["found"]:
        for room in list(cursors):
            cursors[room] = drain(room, cursors[room], stats)
            if stats["found"]:
                break
        print(
            f"\rtarandi: {stats['seen']} mesaj, kacan: {stats['lost']}, "
            f"kalan: {int(deadline - time.time())}s   ",
            end="",
        )
        time.sleep(2)

    print()
    if stats["found"]:
        return 0
    print(
        f"Makbuz yok. {stats['seen']} mesaj tarandi, {stats['lost']} mesaj "
        f"ring'den dustugu icin gorulemedi."
    )
    print("Kacan sayisi sifir degilse bu sonuc kesin degildir.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
