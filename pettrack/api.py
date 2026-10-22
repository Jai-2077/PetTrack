"""JSON API: machine sync (shared key) + UI polling (session)."""
from __future__ import annotations

import hmac

from flask import Blueprint, jsonify, request

from . import __version__, db, services, sync as syncmod
from .web import cfg, conn, hw, sync

bp = Blueprint("api", __name__, url_prefix="/api")


def _key_ok() -> bool:
    key = cfg().sync_key
    given = request.headers.get("X-PetTrack-Key", "")
    return bool(key) and hmac.compare_digest(given.encode(), key.encode())


def _deny():
    return jsonify(error="unauthorized"), 401


@bp.get("/health")
def health():
    return jsonify(ok=True, mode=cfg().mode, version=__version__, time=db.utcnow(),
                   instance=db.get_meta(conn(), "instance_id"))


@bp.get("/pet/<uid>")
def pet_bundle(uid):
    if not _key_ok():
        return _deny()
    uid = db.normalize_uid(uid)
    data = syncmod.bundle_for(conn(), uid) if uid else None
    return (jsonify(data), 200) if data else (jsonify(error="not found"), 404)


@bp.post("/sync/push")
def sync_push():
    if not _key_ok():
        return _deny()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="bad payload"), 400
    counts = syncmod.apply_rows(conn(), body)
    return jsonify(ok=True, applied=counts)


@bp.get("/sync/pull")
def sync_pull():
    if not _key_ok():
        return _deny()
    try:
        since = max(0, int(request.args.get("since", 0)))
        limit = min(200, max(1, int(request.args.get("limit", 100))))
    except ValueError:
        return jsonify(error="bad params"), 400
    rows, nxt, more = syncmod.collect_since(conn(), since, limit)
    return jsonify(rows=rows, next=nxt, has_more=more)


@bp.get("/status")
def status():
    c = conn()
    s = sync()
    return jsonify(
        mode=cfg().mode,
        scanner=hw().snapshot(),
        sync={**s.state, "enabled": s.enabled},
        unsynced=sum(services.unsynced_counts(c).values()),
    )


@bp.get("/scan-events")
def scan_events():
    try:
        after = int(request.args.get("after", 0))
    except ValueError:
        after = 0
    return jsonify(events=hw().events_after(after), last=hw().last_event_id)
