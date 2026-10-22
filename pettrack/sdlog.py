"""Parse and import the Arduino 2 SD log (PETLOG.TXT).

Line format:  dev,boot,seq,uid,ms,sess      (header line "dev,boot,seq,uid,ms,sess" is skipped)

dev/boot/seq come from Arduino 1, so they give every tap a stable id. A tap that the laptop
already saw live has the same id and is skipped on import. The SD card has no real clock, only
Arduino 2's uptime (ms), so imported taps get an *estimated* time:
  - newest power-on session in the file: anchor_time - (last_ms - ms)      -> "estimated"
  - older sessions: no usable clock                                        -> "unknown"
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import services
from .db import iso, utcnow

DEV_RE = re.compile(r"^[A-Za-z0-9_-]{1,8}$")
UID_RE = re.compile(r"^[0-9A-Fa-f]{8,20}$")


@dataclass
class Entry:
    dev: str
    boot: int
    seq: int
    uid: str
    ms: int
    sess: int

    @property
    def scan_id(self) -> str:
        return f"{self.dev}:{self.boot}:{self.seq}"


def parse_log(text: str) -> tuple[list[Entry], list[str]]:
    entries: list[Entry] = []
    bad: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().strip("\x00")
        if not line or line.lower().startswith("dev,"):
            continue
        parts = [p.strip() for p in line.split(",")]
        try:
            if len(parts) not in (5, 6):
                raise ValueError
            dev, boot, seq, uid, ms = parts[:5]
            sess = int(parts[5]) if len(parts) == 6 else 0
            if not DEV_RE.match(dev) or not UID_RE.match(uid) or len(uid) % 2:
                raise ValueError
            entries.append(Entry(dev, int(boot), int(seq), uid.upper(), int(ms), sess))
        except ValueError:
            bad.append(line[:80])
    return entries, bad


def import_entries(conn, entries: list[Entry], anchor: datetime) -> dict:
    """anchor = real time at which the newest entry in this log was written (usually: when the card was pulled)."""
    # split into power-on sessions (session id changes, or uptime goes backwards)
    segments: list[list[Entry]] = []
    for e in entries:
        if not segments or segments[-1][-1].sess != e.sess or e.ms < segments[-1][-1].ms:
            segments.append([])
        segments[-1].append(e)

    new = dup = unknown = 0
    seen_uids: set[str] = set()
    for si, seg in enumerate(segments):
        newest = si == len(segments) - 1
        last_ms = seg[-1].ms
        for e in seg:
            if conn.execute("SELECT 1 FROM scans WHERE id=?", (e.scan_id,)).fetchone():
                dup += 1
                continue
            if newest:
                when, quality = iso(anchor - timedelta(milliseconds=last_ms - e.ms)), "estimated"
            else:
                when, quality = iso(anchor), "unknown"
                unknown += 1
            services.record_scan(conn, e.uid, "sd", device=e.dev, boot=e.boot, dev_seq=e.seq,
                                 occurred_at=when, quality=quality, scan_id=e.scan_id)
            seen_uids.add(e.uid)
            new += 1
    unreg = [u for u in sorted(seen_uids) if not services.get_pet(conn, u)]
    return {"total": len(entries), "new": new, "duplicates": dup, "unknown_time": unknown,
            "sessions": len(segments), "unregistered": unreg}
