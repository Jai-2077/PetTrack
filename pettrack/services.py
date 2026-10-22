"""Domain logic: pets, vaccinations, visits, scans, reports."""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime

from .db import new_id, next_seq, normalize_uid, stamp, upsert, utcnow

PET_COLS = (
    "uid,name,species,breed,gender,birth_date,color,weight_kg,allergies,conditions,medications,notes,"
    "owner_name,owner_phone,owner_email,owner_city,owner_address,vet_name,public_phone,is_lost,lost_since,"
    "photo_mime,created_at,updated_at,deleted,dirty,"
    "(photo IS NOT NULL AND length(photo) > 0) AS has_photo"
)

TEXT_LIMITS = {
    "name": 80, "species": 40, "breed": 80, "color": 40, "allergies": 500, "conditions": 500,
    "medications": 500, "notes": 1000, "owner_name": 80, "owner_phone": 24, "owner_email": 120,
    "owner_city": 80, "owner_address": 200, "vet_name": 80,
}
PHONE_RE = re.compile(r"^\+?[\d\s\-()]{6,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GENDERS = ("Male", "Female", "Unknown")


# ---------------------------------------------------------------- helpers
def parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def age_text(birth_date: str) -> str:
    b = parse_date(birth_date)
    if not b:
        return ""
    today = date.today()
    months = (today.year - b.year) * 12 + today.month - b.month - (1 if today.day < b.day else 0)
    if months < 0:
        return ""
    y, m = divmod(months, 12)
    if y == 0:
        return f"{m} mo"
    return f"{y} yr" + (f" {m} mo" if m else "")


def vaccine_status(next_due: str, today: date | None = None) -> str:
    d = parse_date(next_due)
    if not d:
        return ""
    delta = (d - (today or date.today())).days
    return "overdue" if delta < 0 else "soon" if delta <= 30 else "ok"


# ---------------------------------------------------------------- pets
def get_pet(conn, uid: str):
    return conn.execute(f"SELECT {PET_COLS} FROM pets WHERE uid=? AND deleted=0", (uid,)).fetchone()


def clean_pet_form(form) -> tuple[dict, dict]:
    errors: dict[str, str] = {}
    v: dict = {}
    for k, limit in TEXT_LIMITS.items():
        v[k] = (form.get(k) or "").strip()[:limit]
    for k in ("name", "species", "owner_name", "owner_phone"):
        if not v[k]:
            errors[k] = "Required"
    if v["owner_phone"] and not PHONE_RE.match(v["owner_phone"]):
        errors["owner_phone"] = "Enter a valid phone number"
    if v["owner_email"] and not EMAIL_RE.match(v["owner_email"]):
        errors["owner_email"] = "Enter a valid email"
    g = (form.get("gender") or "Unknown").strip().title()
    v["gender"] = g if g in GENDERS else "Unknown"
    bd = (form.get("birth_date") or "").strip()
    if bd:
        d = parse_date(bd)
        if not d or d > date.today():
            errors["birth_date"] = "Use a past date"
        else:
            v["birth_date"] = bd
    else:
        v["birth_date"] = ""
    w = (form.get("weight_kg") or "").strip()
    if w:
        try:
            wf = float(w)
            if not 0 < wf < 300:
                raise ValueError
            v["weight_kg"] = round(wf, 2)
        except ValueError:
            errors["weight_kg"] = "Weight in kg, e.g. 12.5"
    else:
        v["weight_kg"] = None
    v["public_phone"] = 1 if (form.get("public_phone") or "").lower() in {"1", "on", "true"} else 0
    return v, errors


def save_pet(conn, uid: str, values: dict, photo=None, remove_photo=False) -> None:
    existing = conn.execute("SELECT deleted FROM pets WHERE uid=?", (uid,)).fetchone()
    row = dict(values)
    row.update(uid=uid, deleted=0, **stamp(conn))
    if existing is None or existing["deleted"]:
        row["created_at"] = row["updated_at"]
        row["is_lost"] = 0
        row["lost_since"] = ""
    if photo:
        row["photo"], row["photo_mime"] = photo
    elif remove_photo:
        row["photo"], row["photo_mime"] = None, ""
    upsert(conn, "pets", row, "uid")
    conn.commit()


def get_photo(conn, uid: str):
    return conn.execute(
        "SELECT photo, photo_mime, updated_at FROM pets WHERE uid=? AND deleted=0 AND photo IS NOT NULL", (uid,)
    ).fetchone()


def set_lost(conn, uid: str, lost: bool) -> None:
    s = stamp(conn)
    conn.execute(
        "UPDATE pets SET is_lost=?, lost_since=?, updated_at=?, dirty=1, sync_seq=? WHERE uid=? AND deleted=0",
        (1 if lost else 0, utcnow() if lost else "", s["updated_at"], s["sync_seq"], uid),
    )
    conn.commit()


def delete_pet(conn, uid: str) -> None:
    for table, col in (("vaccinations", "pet_uid"), ("visits", "pet_uid"), ("found_reports", "pet_uid")):
        for r in conn.execute(f"SELECT id FROM {table} WHERE {col}=? AND deleted=0", (uid,)).fetchall():
            s = stamp(conn)
            conn.execute(
                f"UPDATE {table} SET deleted=1, updated_at=?, dirty=1, sync_seq=? WHERE id=?",
                (s["updated_at"], s["sync_seq"], r["id"]),
            )
    s = stamp(conn)
    conn.execute(
        "UPDATE pets SET deleted=1, is_lost=0, updated_at=?, dirty=1, sync_seq=? WHERE uid=?",
        (s["updated_at"], s["sync_seq"], uid),
    )
    conn.commit()


def list_pets(conn, q="", species="", lost=False, limit=24, offset=0):
    where, args = ["deleted=0"], []
    if q:
        like = f"%{q}%"
        where.append("(name LIKE ? OR owner_name LIKE ? OR owner_phone LIKE ? OR uid LIKE ? OR breed LIKE ?)")
        args += [like] * 5
    if species:
        where.append("lower(species)=lower(?)")
        args.append(species)
    if lost:
        where.append("is_lost=1")
    w = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM pets WHERE {w}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT {PET_COLS} FROM pets WHERE {w} ORDER BY is_lost DESC, updated_at DESC LIMIT ? OFFSET ?",
        [*args, limit, offset],
    ).fetchall()
    return rows, total


def species_list(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT species FROM pets WHERE deleted=0 ORDER BY 1")]


# ---------------------------------------------------------------- vaccinations / visits
def annotate_vaccinations(rows):
    """Only the newest dose of each vaccine carries a due status."""
    latest: dict[str, str] = {}
    for r in rows:
        k = r["vaccine"].lower()
        if k not in latest or r["given_on"] > latest[k]:
            latest[k] = r["given_on"]
    out = []
    for r in rows:
        d = dict(r)
        current = latest[r["vaccine"].lower()] == r["given_on"]
        d["status"] = vaccine_status(r["next_due"]) if current else ""
        d["superseded"] = not current
        out.append(d)
    return out


def pet_vaccinations(conn, uid):
    rows = conn.execute(
        "SELECT * FROM vaccinations WHERE pet_uid=? AND deleted=0 ORDER BY given_on DESC", (uid,)
    ).fetchall()
    return annotate_vaccinations(rows)


def pet_visits(conn, uid):
    return conn.execute(
        "SELECT * FROM visits WHERE pet_uid=? AND deleted=0 ORDER BY visited_on DESC, created_at DESC", (uid,)
    ).fetchall()


def add_vaccination(conn, uid, form) -> str | None:
    vaccine = (form.get("vaccine") or "").strip()[:80]
    given = (form.get("given_on") or "").strip()
    due = (form.get("next_due") or "").strip()
    if not vaccine:
        return "Vaccine name required"
    gd = parse_date(given)
    if not gd or gd > date.today():
        return "Date given must be a valid past date"
    if due and (not parse_date(due) or parse_date(due) < gd):
        return "Next due must be after the date given"
    row = {"id": new_id(), "pet_uid": uid, "vaccine": vaccine, "given_on": given, "next_due": due,
           "notes": (form.get("notes") or "").strip()[:200], "deleted": 0, **stamp(conn)}
    row["created_at"] = row["updated_at"]
    upsert(conn, "vaccinations", row, "id")
    conn.commit()
    return None


def add_visit(conn, uid, form) -> str | None:
    on = (form.get("visited_on") or "").strip()
    d = parse_date(on)
    if not d or d > date.today():
        return "Visit date must be a valid past date"
    reason = (form.get("reason") or "").strip()[:120]
    if not reason:
        return "Reason required"
    row = {"id": new_id(), "pet_uid": uid, "visited_on": on, "reason": reason,
           "notes": (form.get("notes") or "").strip()[:1000], "vet": (form.get("vet") or "").strip()[:80],
           "deleted": 0, **stamp(conn)}
    row["created_at"] = row["updated_at"]
    upsert(conn, "visits", row, "id")
    conn.commit()
    return None


def soft_delete(conn, table: str, row_id: str) -> None:
    assert table in {"vaccinations", "visits"}
    s = stamp(conn)
    conn.execute(
        f"UPDATE {table} SET deleted=1, updated_at=?, dirty=1, sync_seq=? WHERE id=?",
        (s["updated_at"], s["sync_seq"], row_id),
    )
    conn.commit()


def due_vaccinations(conn, horizon_days: int = 30):
    rows = conn.execute(
        "SELECT v.pet_uid, v.vaccine, v.given_on, v.next_due, p.name AS pet_name FROM vaccinations v "
        "JOIN pets p ON p.uid=v.pet_uid AND p.deleted=0 WHERE v.deleted=0"
    ).fetchall()
    latest: dict[tuple, sqlite3.Row] = {}
    for r in rows:
        k = (r["pet_uid"], r["vaccine"].lower())
        if k not in latest or r["given_on"] > latest[k]["given_on"]:
            latest[k] = r
    today = date.today()
    out = []
    for r in latest.values():
        st = vaccine_status(r["next_due"], today)
        if st in ("overdue", "soon"):
            d = parse_date(r["next_due"])
            out.append({**dict(r), "status": st, "days": (d - today).days})
    return sorted(out, key=lambda x: x["next_due"])


# ---------------------------------------------------------------- scans
def record_scan(conn, uid, source, device="", boot=None, dev_seq=None, occurred_at=None,
                quality="live", scan_id=None) -> str:
    base = scan_id or new_id()
    sid = base
    if conn.execute("SELECT 1 FROM scans WHERE id=?", (sid,)).fetchone():
        sid = f"{base}:{new_id()[:6]}"  # never silently drop a real scan
    now = utcnow()
    row = {"id": sid, "uid": uid, "source": source, "device": device, "boot": boot, "dev_seq": dev_seq,
           "occurred_at": occurred_at or now, "time_quality": quality, "created_at": now, "deleted": 0,
           **stamp(conn)}
    upsert(conn, "scans", row, "id")
    conn.commit()
    return sid


def recent_scans(conn, limit=50, uid="", source=""):
    where, args = ["s.deleted=0"], []
    if uid:
        where.append("s.uid=?")
        args.append(uid)
    if source:
        where.append("s.source=?")
        args.append(source)
    return conn.execute(
        "SELECT s.*, p.name AS pet_name FROM scans s LEFT JOIN pets p ON p.uid=s.uid AND p.deleted=0 "
        f"WHERE {' AND '.join(where)} ORDER BY s.occurred_at DESC, s.created_at DESC LIMIT ?",
        [*args, limit],
    ).fetchall()


def unregistered_tags(conn, limit=10):
    return conn.execute(
        "SELECT s.uid, COUNT(*) AS n, MAX(s.occurred_at) AS last_seen FROM scans s "
        "LEFT JOIN pets p ON p.uid=s.uid AND p.deleted=0 WHERE p.uid IS NULL AND s.deleted=0 "
        "GROUP BY s.uid ORDER BY last_seen DESC LIMIT ?", (limit,)
    ).fetchall()


# ---------------------------------------------------------------- reports
def add_report(conn, uid, form) -> str | None:
    name = (form.get("finder_name") or "").strip()[:80]
    phone = (form.get("finder_phone") or "").strip()[:24]
    msg = (form.get("message") or "").strip()[:500]
    loc = (form.get("location") or "").strip()[:200]
    if not (phone or msg):
        return "Leave a phone number or a message so the owner can respond"
    if phone and not PHONE_RE.match(phone):
        return "That phone number looks wrong"
    row = {"id": new_id(), "pet_uid": uid, "finder_name": name, "finder_phone": phone, "message": msg,
           "location": loc, "resolved": 0, "deleted": 0, **stamp(conn)}
    row["created_at"] = row["updated_at"]
    upsert(conn, "found_reports", row, "id")
    conn.commit()
    return None


def list_reports(conn, include_resolved=False):
    w = "" if include_resolved else "AND r.resolved=0"
    return conn.execute(
        "SELECT r.*, p.name AS pet_name FROM found_reports r LEFT JOIN pets p ON p.uid=r.pet_uid "
        f"WHERE r.deleted=0 {w} ORDER BY r.created_at DESC LIMIT 200"
    ).fetchall()


def resolve_report(conn, rid: str) -> None:
    s = stamp(conn)
    conn.execute(
        "UPDATE found_reports SET resolved=1, updated_at=?, dirty=1, sync_seq=? WHERE id=?",
        (s["updated_at"], s["sync_seq"], rid),
    )
    conn.commit()


# ---------------------------------------------------------------- dashboard
def unsynced_counts(conn) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE dirty=1").fetchone()[0]
            for t in ("pets", "vaccinations", "visits", "scans", "found_reports")}


def dashboard_stats(conn, due) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "pets": one("SELECT COUNT(*) FROM pets WHERE deleted=0"),
        "lost": one("SELECT COUNT(*) FROM pets WHERE deleted=0 AND is_lost=1"),
        "reports": one("SELECT COUNT(*) FROM found_reports WHERE deleted=0 AND resolved=0"),
        "overdue": sum(1 for d in due if d["status"] == "overdue"),
        "soon": sum(1 for d in due if d["status"] == "soon"),
        "unsynced": sum(unsynced_counts(conn).values()),
        "scans": one("SELECT COUNT(*) FROM scans WHERE deleted=0"),
    }


def lcd_lines(conn, uid: str) -> tuple[str, str, bool, str]:
    """(line1, line2, known, name) for the scanner's 16x2 display."""
    pet = get_pet(conn, uid)
    if not pet:
        return "Unknown tag", "Register on PC", False, ""
    if pet["is_lost"]:
        return pet["name"][:16], "** LOST PET **", True, pet["name"]
    due = [d for d in due_vaccinations(conn, 30) if d["pet_uid"] == uid]
    if any(d["status"] == "overdue" for d in due):
        return pet["name"][:16], "Vaccine OVERDUE", True, pet["name"]
    if due:
        return pet["name"][:16], "Vaccine due soon", True, pet["name"]
    return pet["name"][:16], pet["owner_name"][:16], True, pet["name"]
