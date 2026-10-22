"""Laptop <-> cloud synchronisation.

Model: every row has updated_at (last write wins), dirty (needs push) and sync_seq (pull cursor).
Cycle:  push all dirty rows  ->  pull everything with remote sync_seq > cursor  ->  store new cursor.
Reads on scan are cloud-first: try the cloud copy of that pet (short timeout), cache it locally,
fall back to the local SQLite copy if the cloud is unreachable.
"""
from __future__ import annotations

import base64
import logging
import re
import threading

import requests

from . import db

log = logging.getLogger("pettrack.sync")

_COMMON = ["created_at", "updated_at", "deleted"]
TABLES: dict[str, tuple[str, list[str]]] = {
    "pets": ("uid", [
        "uid", "name", "species", "breed", "gender", "birth_date", "color", "weight_kg", "allergies",
        "conditions", "medications", "notes", "owner_name", "owner_phone", "owner_email", "owner_city",
        "owner_address", "vet_name", "public_phone", "is_lost", "lost_since", "photo", "photo_mime",
        *_COMMON]),
    "vaccinations": ("id", ["id", "pet_uid", "vaccine", "given_on", "next_due", "notes", *_COMMON]),
    "visits": ("id", ["id", "pet_uid", "visited_on", "reason", "notes", "vet", *_COMMON]),
    "scans": ("id", ["id", "uid", "source", "device", "boot", "dev_seq", "occurred_at", "time_quality", *_COMMON]),
    "found_reports": ("id", ["id", "pet_uid", "finder_name", "finder_phone", "message", "location",
                             "resolved", *_COMMON]),
}
ORDER = ["pets", "vaccinations", "visits", "scans", "found_reports"]
ID_RE = re.compile(r"^[A-Za-z0-9:_\-]{1,64}$")
MAX_PHOTO = 2 * 1024 * 1024


# ---------------------------------------------------------------- (de)serialisation
def _ser(table: str, row) -> dict:
    out = {}
    for c in TABLES[table][1]:
        v = row[c]
        if c == "photo":
            v = base64.b64encode(v).decode() if v else None
        out[c] = v
    return out


def collect_dirty(conn, limit: int = 50):
    payload: dict[str, list] = {}
    marks = []
    for t in ORDER:
        pk, cols = TABLES[t]
        sel = ",".join(cols)
        rows = conn.execute(f"SELECT {sel} FROM {t} WHERE dirty=1 ORDER BY sync_seq LIMIT ?", (limit,)).fetchall()
        if rows:
            payload[t] = [_ser(t, r) for r in rows]
            marks += [(t, pk, r[pk], r["updated_at"]) for r in rows]
    return payload, marks


def mark_pushed(conn, marks) -> None:
    for t, pk, key, updated in marks:
        # only clear if the row wasn't edited again while the push was in flight
        conn.execute(f"UPDATE {t} SET dirty=0 WHERE {pk}=? AND updated_at=?", (key, updated))
    conn.commit()


def collect_since(conn, since: int, limit: int):
    found = []
    for t in ORDER:
        pk, cols = TABLES[t]
        sel = ",".join(cols) + ",sync_seq"
        for r in conn.execute(
            f"SELECT {sel} FROM {t} WHERE sync_seq > ? ORDER BY sync_seq LIMIT ?", (since, limit + 1)
        ).fetchall():
            found.append((r["sync_seq"], t, _ser(t, r)))
    found.sort(key=lambda x: x[0])
    page = found[:limit]
    rows: dict[str, list] = {}
    for _, t, r in page:
        rows.setdefault(t, []).append(r)
    return rows, (page[-1][0] if page else since), len(found) > limit


def apply_rows(conn, rows: dict) -> dict:
    """Merge rows from the other side. Newer updated_at wins; never marks rows dirty."""
    counts = {t: 0 for t in ORDER}
    for t in ORDER:
        pk, cols = TABLES[t]
        for r in rows.get(t, []) if isinstance(rows, dict) else []:
            if not isinstance(r, dict):
                continue
            key, upd = r.get(pk), r.get("updated_at")
            if not isinstance(key, str) or not ID_RE.match(key) or not isinstance(upd, str) or len(upd) < 20:
                continue
            if t == "pets" and not db.UID_RE.match(key):
                continue
            cur = conn.execute(f"SELECT updated_at FROM {t} WHERE {pk}=?", (key,)).fetchone()
            if cur and (cur["updated_at"] or "") >= upd:
                continue
            row = {c: r[c] for c in cols if c in r}
            if "photo" in row:
                try:
                    p = base64.b64decode(row["photo"]) if row["photo"] else None
                except Exception:
                    p = None
                row["photo"] = p if p and len(p) <= MAX_PHOTO else None
            try:
                row.update(dirty=0, sync_seq=db.next_seq(conn))
                db.upsert(conn, t, row, pk)
                counts[t] += 1
            except Exception as e:  # malformed row: skip, keep going
                log.warning("skip bad %s row %s: %s", t, key, e)
                conn.rollback()
    conn.commit()
    return counts


def bundle_for(conn, uid: str):
    pet = conn.execute(f"SELECT {','.join(TABLES['pets'][1])} FROM pets WHERE uid=?", (uid,)).fetchone()
    if not pet:
        return None
    out = {"pets": [_ser("pets", pet)]}
    for t, col in (("vaccinations", "pet_uid"), ("visits", "pet_uid")):
        rows = conn.execute(f"SELECT {','.join(TABLES[t][1])} FROM {t} WHERE {col}=?", (uid,)).fetchall()
        out[t] = [_ser(t, r) for r in rows]
    return out


# ---------------------------------------------------------------- client side
class SyncService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_ev = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = {"online": None, "last_ok": "", "last_error": "", "pushed": 0, "pulled": 0, "running": False}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.cloud_url and self.cfg.mode == "local")

    def _headers(self):
        return {"X-PetTrack-Key": self.cfg.sync_key}

    def _url(self, path: str) -> str:
        return self.cfg.cloud_url + path

    def trigger(self) -> None:
        self.wake.set()

    def start(self) -> None:
        if not self.enabled or self.thread:
            return
        self.thread = threading.Thread(target=self._loop, name="pettrack-sync", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_ev.set()
        self.wake.set()

    def _loop(self) -> None:
        while not self.stop_ev.is_set():
            self.wake.wait(self.cfg.sync_interval)
            self.wake.clear()
            if self.stop_ev.is_set():
                break
            self.run_once()

    def run_once(self) -> bool:
        if not self.enabled or not self.lock.acquire(blocking=False):
            return False
        conn = db.connect(self.cfg.db_path)
        pushed = pulled = 0
        try:
            self.state["running"] = True
            self._check_instance(conn)
            for _ in range(500):
                payload, marks = collect_dirty(conn)
                if not marks:
                    break
                r = requests.post(self._url("/api/sync/push"), json=payload, headers=self._headers(),
                                  timeout=max(self.cfg.cloud_timeout, 15))
                r.raise_for_status()
                mark_pushed(conn, marks)
                pushed += len(marks)
            cursor = int(db.get_meta(conn, "pull_cursor", "0"))
            while True:
                r = requests.get(self._url("/api/sync/pull"), params={"since": cursor, "limit": 100},
                                 headers=self._headers(), timeout=max(self.cfg.cloud_timeout, 15))
                r.raise_for_status()
                data = r.json()
                pulled += sum(apply_rows(conn, data.get("rows", {})).values())
                cursor = int(data.get("next", cursor))
                db.set_meta(conn, "pull_cursor", str(cursor))
                conn.commit()
                if not data.get("has_more"):
                    break
            self.state.update(online=True, last_ok=db.utcnow(), last_error="", pushed=pushed, pulled=pulled)
            return True
        except requests.RequestException as e:
            self.state.update(online=False, last_error=str(e)[:200])
            return False
        except Exception as e:  # never kill the sync thread
            log.exception("sync failed")
            self.state.update(last_error=f"{type(e).__name__}: {e}"[:200])
            return False
        finally:
            self.state["running"] = False
            conn.close()
            self.lock.release()

    def _check_instance(self, conn) -> None:
        """If the cloud database was replaced (fresh/wiped disk), its sequence numbers restart and it has
        lost our data: reset the pull cursor and mark every local row for re-upload."""
        r = requests.get(self._url("/api/health"), timeout=self.cfg.cloud_timeout)
        r.raise_for_status()
        inst = (r.json() or {}).get("instance", "")
        known = db.get_meta(conn, "cloud_instance")
        if inst and known and inst != known:
            log.warning("cloud database changed (%s -> %s): re-uploading everything", known, inst)
            self.mark_all_dirty(conn)
            db.set_meta(conn, "pull_cursor", "0")
        if inst:
            db.set_meta(conn, "cloud_instance", inst)
        conn.commit()

    @staticmethod
    def mark_all_dirty(conn) -> None:
        for t in ORDER:
            conn.execute(f"UPDATE {t} SET dirty=1")
        conn.commit()

    def resend_all(self) -> None:
        conn = db.connect(self.cfg.db_path)
        try:
            self.mark_all_dirty(conn)
            db.set_meta(conn, "pull_cursor", "0")
            conn.commit()
        finally:
            conn.close()
        self.trigger()

    def reset_cursor(self) -> None:
        conn = db.connect(self.cfg.db_path)
        try:
            db.set_meta(conn, "pull_cursor", "0")
            conn.commit()
        finally:
            conn.close()

    def refresh_pet(self, uid: str) -> bool:
        """Cloud-first lookup. True if the cloud answered (cached locally), False = use local copy."""
        if not self.enabled:
            return False
        try:
            r = requests.get(self._url(f"/api/pet/{uid}"), headers=self._headers(), timeout=self.cfg.cloud_timeout)
        except requests.RequestException as e:
            self.state.update(online=False, last_error=str(e)[:200])
            return False
        self.state["online"] = True
        if r.status_code != 200:
            return False
        conn = db.connect(self.cfg.db_path)
        try:
            apply_rows(conn, r.json())
        finally:
            conn.close()
        return True
