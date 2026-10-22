"""Browser UI routes."""
from __future__ import annotations

import base64
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import segno
from flask import (Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)

from . import db, sdlog, security, services, sync as syncmod
from .web import conn, cfg, hw, process_photo, public_base, sync

bp = Blueprint("main", __name__)
PAGE = 24


def _uid_or_404(uid: str) -> str:
    n = db.normalize_uid(uid)
    if not n:
        abort(404)
    return n


def _pet_or_404(uid: str):
    uid = _uid_or_404(uid)
    pet = services.get_pet(conn(), uid)
    if not pet:
        abort(404)
    return uid, pet


# ------------------------------------------------------------------ auth
@bp.route("/login", methods=["GET", "POST"])
def login():
    c = cfg()
    if not c.require_login:
        return redirect(url_for("main.dashboard"))
    ip = request.remote_addr or "?"
    if request.method == "POST":
        if security.rate_peek(f"login:{ip}", 5, 300):
            flash("Too many attempts. Wait a few minutes.", "err")
            return render_template("login.html"), 429
        if security.check_password(c, request.form.get("password", "")):
            session.clear()
            session["auth"] = True
            session.permanent = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("main.dashboard"))
        security.rate_hit(f"login:{ip}", 5, 300)
        flash("Wrong password.", "err")
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


# ------------------------------------------------------------------ dashboard / lookup
@bp.get("/")
def dashboard():
    c = conn()
    due = services.due_vaccinations(c)
    pets, _ = services.list_pets(c, limit=6)
    return render_template(
        "dashboard.html", stats=services.dashboard_stats(c, due), due=due[:8],
        unregistered=services.unregistered_tags(c), scans=services.recent_scans(c, 8), pets=pets,
        last_event=hw().last_event_id,
    )


@bp.post("/lookup")
def lookup():
    uid = db.normalize_uid(request.form.get("uid"))
    if not uid:
        flash("UID must be 4-32 hex characters (0-9, A-F).", "err")
        return redirect(url_for("main.dashboard"))
    c = conn()
    services.record_scan(c, uid, "manual")
    sync().trigger()
    return redirect(url_for("main.scan", uid=uid))


@bp.get("/scan/<uid>")
def scan(uid):
    """Resolver for a tag: cloud-first refresh, then record page or registration."""
    uid = _uid_or_404(uid)
    sync().refresh_pet(uid)
    if services.get_pet(conn(), uid):
        return redirect(url_for("main.pet", uid=uid))
    return redirect(url_for("main.register", uid=uid))


# ------------------------------------------------------------------ pets
@bp.get("/pets")
def pets():
    c = conn()
    q = request.args.get("q", "").strip()[:60]
    species = request.args.get("species", "").strip()[:40]
    lost = request.args.get("lost") == "1"
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    rows, total = services.list_pets(c, q, species, lost, PAGE, (page - 1) * PAGE)
    return render_template("pets.html", pets=rows, total=total, q=q, species=species, lost=lost, page=page,
                           pages=max(1, -(-total // PAGE)), species_list=services.species_list(c))


@bp.get("/pet/<uid>")
def pet(uid):
    uid, p = _pet_or_404(uid)
    c = conn()
    vax = services.pet_vaccinations(c, uid)
    return render_template(
        "pet.html", pet=p, vax=vax, visits=services.pet_visits(c, uid),
        scans=services.recent_scans(c, 8, uid=uid), reports=[r for r in services.list_reports(c) if r["pet_uid"] == uid],
        today=datetime.now().date().isoformat(), public_url=f"{public_base()}/p/{uid}",
    )


def _pet_form(uid: str, creating: bool):
    c = conn()
    existing = services.get_pet(c, uid)
    errors: dict = {}
    form = dict(existing) if existing else {"public_phone": 1}
    if request.method == "POST":
        values, errors = services.clean_pet_form(request.form)
        photo = None
        f = request.files.get("photo")
        if f and f.filename:
            try:
                photo = process_photo(f)
            except ValueError as e:
                errors["photo"] = str(e)
        if not errors:
            services.save_pet(c, uid, values, photo, remove_photo=bool(request.form.get("remove_photo")))
            sync().trigger()
            flash("Saved." if existing else f"{values['name']} registered.", "ok")
            return redirect(url_for("main.pet", uid=uid))
        form = request.form.to_dict()
        form.setdefault("public_phone", "0")
        flash("Fix the highlighted fields.", "err")
    return render_template("pet_form.html", uid=uid, pet=existing, form=form, errors=errors, creating=creating)


@bp.route("/register/<uid>", methods=["GET", "POST"])
def register(uid):
    uid = _uid_or_404(uid)
    if request.method == "GET" and services.get_pet(conn(), uid):
        return redirect(url_for("main.pet", uid=uid))
    return _pet_form(uid, creating=True)


@bp.route("/pet/<uid>/edit", methods=["GET", "POST"])
def pet_edit(uid):
    uid, _ = _pet_or_404(uid)
    return _pet_form(uid, creating=False)


@bp.post("/pet/<uid>/lost")
def pet_lost(uid):
    uid, p = _pet_or_404(uid)
    lost = request.form.get("action") == "lost"
    services.set_lost(conn(), uid, lost)
    sync().trigger()
    flash(f"{p['name']} marked {'LOST - public page now shows an alert' if lost else 'found. Welcome home!'}", "ok")
    return redirect(url_for("main.pet", uid=uid))


@bp.post("/pet/<uid>/delete")
def pet_delete(uid):
    uid, p = _pet_or_404(uid)
    services.delete_pet(conn(), uid)
    sync().trigger()
    flash(f"{p['name']} deleted. The tag can be registered again.", "ok")
    return redirect(url_for("main.pets"))


@bp.post("/pet/<uid>/vaccination")
def vaccination_add(uid):
    uid, _ = _pet_or_404(uid)
    err = services.add_vaccination(conn(), uid, request.form)
    flash(err or "Vaccination added.", "err" if err else "ok")
    sync().trigger()
    return redirect(url_for("main.pet", uid=uid) + "#vaccinations")


@bp.post("/vaccination/<rid>/delete")
def vaccination_delete(rid):
    row = conn().execute("SELECT pet_uid FROM vaccinations WHERE id=? AND deleted=0", (rid,)).fetchone()
    if not row:
        abort(404)
    services.soft_delete(conn(), "vaccinations", rid)
    sync().trigger()
    return redirect(url_for("main.pet", uid=row["pet_uid"]) + "#vaccinations")


@bp.post("/pet/<uid>/visit")
def visit_add(uid):
    uid, _ = _pet_or_404(uid)
    err = services.add_visit(conn(), uid, request.form)
    flash(err or "Visit logged.", "err" if err else "ok")
    sync().trigger()
    return redirect(url_for("main.pet", uid=uid) + "#visits")


@bp.post("/visit/<rid>/delete")
def visit_delete(rid):
    row = conn().execute("SELECT pet_uid FROM visits WHERE id=? AND deleted=0", (rid,)).fetchone()
    if not row:
        abort(404)
    services.soft_delete(conn(), "visits", rid)
    sync().trigger()
    return redirect(url_for("main.pet", uid=row["pet_uid"]) + "#visits")


@bp.get("/pet/<uid>/card")
def pet_card(uid):
    uid, p = _pet_or_404(uid)
    url = f"{public_base()}/p/{uid}"
    qr = segno.make(url, error="m").svg_inline(scale=5, border=1, dark="#1C2420", light="#ffffff")
    return render_template("card.html", pet=p, qr=qr, url=url)


@bp.get("/photo/<uid>")
def photo(uid):
    uid = _uid_or_404(uid)
    row = services.get_photo(conn(), uid)
    if not row:
        abort(404)
    resp = Response(row["photo"], mimetype=row["photo_mime"] or "image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# ------------------------------------------------------------------ public finder page
@bp.get("/p/<uid>")
def public_view(uid):
    uid = db.normalize_uid(uid) or ""
    p = services.get_pet(conn(), uid) if uid else None
    if not p:
        return render_template("public_missing.html"), 404
    return render_template("public.html", pet=p)


@bp.post("/p/<uid>/found")
def public_found(uid):
    uid = db.normalize_uid(uid) or ""
    p = services.get_pet(conn(), uid) if uid else None
    if not p:
        abort(404)
    if security.rate_hit(f"found:{request.remote_addr}:{uid}", 5, 3600):
        abort(429, "Too many reports from this device. Please call the owner instead.")
    err = services.add_report(conn(), uid, request.form)
    if err:
        flash(err, "err")
        return redirect(url_for("main.public_view", uid=uid) + "#report")
    services.record_scan(conn(), uid, "web")
    sync().trigger()
    flash("Thank you! The owner has been notified.", "ok")
    return redirect(url_for("main.public_view", uid=uid))


@bp.get("/reports")
def reports():
    show_all = request.args.get("all") == "1"
    return render_template("reports.html", reports=services.list_reports(conn(), show_all), show_all=show_all)


@bp.post("/reports/<rid>/resolve")
def report_resolve(rid):
    services.resolve_report(conn(), rid)
    sync().trigger()
    return redirect(url_for("main.reports"))


# ------------------------------------------------------------------ scan log + SD import
@bp.get("/scans")
def scans():
    uid = db.normalize_uid(request.args.get("uid")) or ""
    source = request.args.get("source", "")
    if source not in {"live", "sd", "manual", "web"}:
        source = ""
    return render_template("scans.html", scans=services.recent_scans(conn(), 300, uid, source), uid=uid, source=source)


@bp.route("/import", methods=["GET", "POST"])
def import_log():
    result = None
    if request.method == "POST":
        f = request.files.get("log")
        raw = f.read(2_000_001) if f and f.filename else b""
        if not raw:
            flash("Choose PETLOG.TXT from the SD card.", "err")
        elif len(raw) > 2_000_000:
            flash("Log file is over 2 MB.", "err")
        else:
            now = datetime.now(timezone.utc)
            anchor = now
            val = (request.form.get("anchor") or "").strip()
            if val:
                try:
                    off = int(request.form.get("tz_offset") or 0)
                    anchor = (datetime.fromisoformat(val) + timedelta(minutes=off)).replace(tzinfo=timezone.utc)
                except ValueError:
                    anchor = None
            if anchor is None or anchor > now + timedelta(minutes=5):
                flash("Anchor time is invalid or in the future.", "err")
            else:
                entries, bad = sdlog.parse_log(raw.decode("utf-8", "ignore"))
                result = sdlog.import_entries(conn(), entries, anchor)
                result["invalid"] = len(bad)
                result["bad_samples"] = bad[:5]
                sync().trigger()
                flash(f"Imported {result['new']} new tap(s), skipped {result['duplicates']} already known.", "ok")
    return render_template("import.html", result=result)


# ------------------------------------------------------------------ sync page
@bp.get("/sync")
def sync_page():
    s = sync()
    return render_template("sync.html", state=s.state, enabled=s.enabled, counts=services.unsynced_counts(conn()),
                           cursor=db.get_meta(conn(), "pull_cursor", "0"))


@bp.post("/sync/now")
def sync_now():
    s = sync()
    if not s.enabled:
        flash("No cloud configured. Set PETTRACK_CLOUD_URL and PETTRACK_SYNC_KEY.", "err")
    else:
        ok = s.run_once()
        flash("Synced." if ok else f"Sync failed: {s.state['last_error'] or 'busy'}", "ok" if ok else "err")
    return redirect(url_for("main.sync_page"))


@bp.post("/sync/resend")
def sync_resend():
    sync().resend_all()
    flash("Everything is queued for upload; it goes up on the next sync.", "ok")
    return redirect(url_for("main.sync_page"))


@bp.post("/sync/reset")
def sync_reset():
    sync().reset_cursor()
    flash("Pull cursor reset. Next sync re-downloads everything from the cloud.", "ok")
    return redirect(url_for("main.sync_page"))


# ------------------------------------------------------------------ export
@bp.get("/export/pets.csv")
def export_csv():
    rows, _ = services.list_pets(conn(), limit=100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["uid", "name", "species", "breed", "gender", "birth_date", "weight_kg", "allergies", "conditions",
            "medications", "owner_name", "owner_phone", "owner_email", "owner_city", "vet_name", "is_lost"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r[c] for c in cols])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=pettrack-pets.csv"})


@bp.get("/export/backup.json")
def export_backup():
    c = conn()
    dump = {}
    for t in syncmod.ORDER:
        pk, cols = syncmod.TABLES[t]
        dump[t] = [syncmod._ser(t, r) for r in c.execute(f"SELECT {','.join(cols)} FROM {t}").fetchall()]
    dump["exported_at"] = db.utcnow()
    return Response(json.dumps(dump), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=pettrack-backup.json"})


# ------------------------------------------------------------------ PWA
@bp.get("/manifest.webmanifest")
def manifest():
    return jsonify(
        name="PetTrack", short_name="PetTrack", start_url="/", display="standalone",
        background_color="#F7F4EE", theme_color="#211E1A",
        icons=[{"src": url_for("static", filename="icon-192.png"), "sizes": "192x192", "type": "image/png"},
               {"src": url_for("static", filename="icon-512.png"), "sizes": "512x512", "type": "image/png"}],
    )


@bp.get("/sw.js")
def service_worker():
    resp = send_from_directory(Path(current_app.root_path) / "static", "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.get("/offline")
def offline():
    return render_template("offline.html")
