import io
import re
import threading
import time
from datetime import datetime, timezone

import pytest
from waitress.server import create_server

from pettrack import create_app, db, hardware, sdlog, services
from pettrack.config import Config, ConfigError


def make_cfg(tmp_path, name="a", **kw):
    base = dict(data_dir=tmp_path / name, serial_enabled=False, sync_key="k" * 16)
    base.update(kw)
    return Config(**base)


def token(client, path="/"):
    html = client.get(path, follow_redirects=True).get_data(as_text=True)
    return re.search(r'data-csrf="([^"]+)"', html).group(1)


def post(client, path, data=None, **kw):
    data = dict(data or {})
    data.setdefault("csrf_token", token(client))
    return client.post(path, data=data, **kw)


PET = {"name": "Bruno", "species": "Dog", "breed": "Lab", "owner_name": "Sai", "owner_phone": "+91 98765 43210",
       "owner_city": "Vijayawada", "public_phone": "1", "allergies": "Penicillin"}
UID = "73A2232A"


@pytest.fixture
def app(tmp_path):
    return create_app(make_cfg(tmp_path), start_services=False)


@pytest.fixture
def client(app):
    return app.test_client()


# ------------------------------------------------------------- protocol
def test_frame_roundtrip_and_corruption():
    f = hardware.make_frame("A1", 12, 3, UID)
    assert hardware.parse_frame(f) == {"dev": "A1", "boot": 12, "seq": 3, "uid": UID}
    assert hardware.parse_frame(f[:-2] + "00") is None          # bad checksum
    assert hardware.parse_frame(f.replace("73A2", "73A3")) is None
    assert hardware.parse_frame("garbage") is None
    assert hardware.parse_frame(UID) is None
    assert hardware.parse_frame(UID, allow_legacy=True)["uid"] == UID


def test_known_checksum_matches_firmware_algorithm():
    body = "A1,12,3,73A2232A"
    x = 0
    for ch in body:
        x ^= ord(ch)
    assert hardware.make_frame("A1", 12, 3, UID).endswith(f"*{x:02X}")


# ------------------------------------------------------------- config
def test_cloud_requires_secrets(tmp_path):
    with pytest.raises(ConfigError):
        Config(mode="cloud", data_dir=tmp_path, require_login=True, admin_password="x").validate()
    with pytest.raises(ConfigError):
        Config(mode="cloud", data_dir=tmp_path, sync_key="k", require_login=True).validate()
    Config(mode="cloud", data_dir=tmp_path, sync_key="k", require_login=True, admin_password="x").validate()


# ------------------------------------------------------------- pets
def test_register_view_edit_public(client):
    assert client.get(f"/scan/{UID}").headers["Location"].endswith(f"/register/{UID}")
    r = post(client, f"/register/{UID}", PET)
    assert r.status_code == 302
    page = client.get(f"/pet/{UID}").get_data(as_text=True)
    assert "Bruno" in page and "Penicillin" in page
    assert client.get(f"/scan/{UID}").headers["Location"].endswith(f"/pet/{UID}")
    pub = client.get(f"/p/{UID}").get_data(as_text=True)
    assert "Bruno" in pub and "tel:" in pub and "Penicillin" in pub
    assert "owner_address" not in pub
    r = post(client, f"/pet/{UID}/edit", {**PET, "name": "Bruno II"})
    assert r.status_code == 302
    assert "Bruno II" in client.get(f"/pet/{UID}").get_data(as_text=True)


def test_validation_rejects_bad_input(client):
    r = post(client, f"/register/{UID}", {**PET, "owner_phone": "abc", "name": ""})
    assert r.status_code == 200 and b"Required" in r.data and b"valid phone" in r.data
    assert client.get(f"/pet/{UID}").status_code == 404


def test_bad_uid_is_404(client):
    assert client.get("/pet/ZZZZ").status_code == 404
    assert client.get("/p/not-hex").status_code == 404


def test_hidden_phone_not_on_public_page(client):
    post(client, f"/register/{UID}", {k: v for k, v in PET.items() if k != "public_phone"})
    pub = client.get(f"/p/{UID}").get_data(as_text=True)
    assert "tel:" not in pub and "98765" not in pub


def test_photo_upload_is_shrunk_and_served(client):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2400, 1800), "red").save(buf, "PNG")
    buf.seek(0)
    r = post(client, f"/register/{UID}", {**PET, "photo": (buf, "dog.png")}, content_type="multipart/form-data")
    assert r.status_code == 302
    img = client.get(f"/photo/{UID}")
    assert img.status_code == 200 and img.mimetype == "image/jpeg"
    assert max(Image.open(io.BytesIO(img.data)).size) <= 800
    bad = post(client, f"/pet/{UID}/edit", {**PET, "photo": (io.BytesIO(b"not an image"), "x.png")},
               content_type="multipart/form-data")
    assert bad.status_code == 200 and b"not a valid image" in bad.data


def test_vaccination_status_and_dashboard(client):
    post(client, f"/register/{UID}", PET)
    post(client, f"/pet/{UID}/vaccination", {"vaccine": "Rabies", "given_on": "2024-01-01", "next_due": "2025-01-01"})
    page = client.get(f"/pet/{UID}").get_data(as_text=True)
    assert "overdue" in page
    # newer dose supersedes the old overdue one
    post(client, f"/pet/{UID}/vaccination", {"vaccine": "rabies", "given_on": "2026-01-01", "next_due": "2999-01-01"})
    dash = client.get("/").get_data(as_text=True)
    assert "All vaccinations up to date" in dash
    assert post(client, f"/pet/{UID}/vaccination", {"vaccine": "X", "given_on": "2999-01-01"}).status_code == 302


def test_visits_lost_delete_and_reregister(client):
    post(client, f"/register/{UID}", PET)
    post(client, f"/pet/{UID}/visit", {"visited_on": "2026-01-02", "reason": "Checkup", "notes": "all good"})
    assert "Checkup" in client.get(f"/pet/{UID}").get_data(as_text=True)
    post(client, f"/pet/{UID}/lost", {"action": "lost"})
    assert "is LOST" in client.get(f"/p/{UID}").get_data(as_text=True)
    post(client, f"/pet/{UID}/lost", {"action": "found"})
    assert "is LOST" not in client.get(f"/p/{UID}").get_data(as_text=True)
    post(client, f"/pet/{UID}/delete")
    assert client.get(f"/pet/{UID}").status_code == 404
    post(client, f"/register/{UID}", {**PET, "name": "Rex"})
    page = client.get(f"/pet/{UID}").get_data(as_text=True)
    assert "Rex" in page and "Checkup" not in page


def test_finder_report_and_rate_limit(client):
    post(client, f"/register/{UID}", PET)
    r = post(client, f"/p/{UID}/found", {"finder_name": "Asha", "finder_phone": "9000000000", "location": "Park"})
    assert r.status_code == 302
    assert "Asha" in client.get("/reports").get_data(as_text=True)
    assert "Someone found" in client.get(f"/pet/{UID}").get_data(as_text=True)
    assert post(client, f"/p/{UID}/found", {"message": ""}).status_code == 302   # validation error, redirected
    codes = [post(client, f"/p/{UID}/found", {"message": "hi"}).status_code for _ in range(6)]
    assert 429 in codes


def test_card_has_qr(client):
    post(client, f"/register/{UID}", PET)
    page = client.get(f"/pet/{UID}/card").get_data(as_text=True)
    assert "<svg" in page and f"/p/{UID}" in page


def test_search_and_pagination(client):
    post(client, f"/register/{UID}", PET)
    post(client, "/register/0C7FB002", {**PET, "name": "Mittens", "species": "Cat"})
    assert "Mittens" in client.get("/pets?q=mitt").get_data(as_text=True)
    assert "Bruno" not in client.get("/pets?species=Cat").get_data(as_text=True)


def test_lookup_records_manual_scan(client):
    post(client, f"/register/{UID}", PET)
    r = post(client, "/lookup", {"uid": "73:a2:23:2a"})
    assert r.headers["Location"].endswith(f"/scan/{UID}")
    assert "manual" in client.get("/scans").get_data(as_text=True)
    assert post(client, "/lookup", {"uid": "xyz"}).status_code == 302


# ------------------------------------------------------------- hardware path
def test_hardware_scan_records_and_builds_lcd_text(app):
    hw = app.extensions["pt_hw"]
    sent = []
    hw.send_display = lambda a, b: sent.append((a, b))
    hw._handle_line(hardware.make_frame("A1", 5, 1, UID), False)
    assert sent[-1][0] == "Unknown tag"
    c = app.test_client()
    post(c, f"/register/{UID}", PET)
    hw._handle_line(hardware.make_frame("A1", 5, 2, UID), False)
    assert sent[-1][0] == "Bruno" and sent[-1][1] == "Sai"
    post(c, f"/pet/{UID}/lost", {"action": "lost"})
    hw._handle_line(hardware.make_frame("A1", 5, 3, UID), False)
    assert "LOST" in sent[-1][1]
    ev = hw.events_after(0)
    assert [e["uid"] for e in ev] == [UID] * 3 and ev[0]["known"] is False and ev[1]["known"] is True
    assert c.get("/api/scan-events?after=2").get_json()["events"][0]["id"] == 3
    # corrupted frame is counted, not stored
    hw._handle_line("#A1,5,4," + UID + "*00", False)
    assert hw.snapshot()["bad_frames"] == 1
    assert len(hw.events_after(0)) == 3


def test_heartbeat_updates_status(app):
    hw = app.extensions["pt_hw"]
    hw._handle_line("!HELLO,A1,1.0,7", False)
    hw._handle_line("!HB,A1,7,1", False)
    s = hw.snapshot()
    assert s["device"] == "A1" and s["fw"] == "1.0" and s["boot"] == 7 and s["rfid_ok"] is True
    hw._handle_line("!ERR,RFID", False)
    assert hw.snapshot()["rfid_ok"] is False


# ------------------------------------------------------------- SD import
SD_LOG = """dev,boot,seq,uid,ms,sess
A1,3,1,73A2232A,5000,1
A1,3,2,0C7FB002,65000,1
A1,4,1,73A2232A,2000,2
A1,4,2,73A2232A,62000,2
garbage line
A1,4,3,ZZZZ,63000,2
"""


def test_parse_sd_log():
    entries, bad = sdlog.parse_log(SD_LOG)
    assert len(entries) == 4 and len(bad) == 2
    assert entries[0].scan_id == "A1:3:1"


def test_import_estimates_times_and_dedupes(app):
    c = app.test_client()
    post(c, f"/register/{UID}", PET)
    anchor = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    conn = db.connect(app.config["PT"].db_path)
    res = sdlog.import_entries(conn, sdlog.parse_log(SD_LOG)[0], anchor)
    assert res["new"] == 4 and res["duplicates"] == 0 and res["sessions"] == 2 and res["unknown_time"] == 2
    assert res["unregistered"] == ["0C7FB002"]
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM scans")}
    assert rows["A1:4:2"]["occurred_at"] == "2026-10-12T12:00:00.000Z"
    assert rows["A1:4:1"]["occurred_at"] == "2026-10-12T11:59:00.000Z"      # 60 s earlier
    assert rows["A1:4:1"]["time_quality"] == "estimated"
    assert rows["A1:3:1"]["time_quality"] == "unknown"
    again = sdlog.import_entries(conn, sdlog.parse_log(SD_LOG)[0], anchor)
    assert again["new"] == 0 and again["duplicates"] == 4
    conn.close()


def test_live_scan_is_not_duplicated_by_sd_import(app):
    hw = app.extensions["pt_hw"]
    hw.send_display = lambda a, b: None
    hw._handle_line(hardware.make_frame("A1", 4, 1, UID), False)       # laptop saw it live
    conn = db.connect(app.config["PT"].db_path)
    res = sdlog.import_entries(conn, sdlog.parse_log(SD_LOG)[0], datetime.now(timezone.utc))
    assert res["duplicates"] == 1 and res["new"] == 3
    conn.close()


def test_import_page_upload(client):
    r = post(client, "/import", {"log": (io.BytesIO(SD_LOG.encode()), "PETLOG.TXT"), "tz_offset": "-330",
                                 "anchor": "2026-10-12T17:30"}, content_type="multipart/form-data")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "New taps" in page and "0C7FB002" in page
    assert "Anchor time is invalid" in post(client, "/import", {
        "log": (io.BytesIO(SD_LOG.encode()), "p.txt"), "anchor": "2999-01-01T00:00", "tz_offset": "0"},
        content_type="multipart/form-data").get_data(as_text=True)


# ------------------------------------------------------------- security
def test_csrf_required(client):
    assert client.post(f"/register/{UID}", data=PET).status_code == 400


def test_cloud_mode_needs_login_but_public_page_open(tmp_path):
    cfg = make_cfg(tmp_path, "cloud", mode="cloud", require_login=True, admin_password="s3cret", cloud_url="")
    app = create_app(cfg, start_services=False)
    c = app.test_client()
    assert c.get("/").headers["Location"].startswith("/login")
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/health").status_code == 200
    assert c.get("/p/" + UID).status_code == 404            # reachable, just no pet
    assert b"Wrong password" in post(c, "/login", {"password": "nope"}, follow_redirects=True).data
    r = post(c, "/login", {"password": "s3cret"})
    assert r.status_code == 302 and c.get("/").status_code == 200
    csp = c.get("/").headers["Content-Security-Policy"]
    script_src = [p for p in csp.split(";") if p.strip().startswith("script-src")][0]
    assert script_src.strip() == "script-src 'self'"       # no inline / eval scripts


def test_login_lockout(tmp_path):
    cfg = make_cfg(tmp_path, "lock", mode="cloud", require_login=True, admin_password="s3cret")
    c = create_app(cfg, start_services=False).test_client()
    codes = [post(c, "/login", {"password": "bad"}).status_code for _ in range(7)]
    assert 429 in codes
    assert post(c, "/login", {"password": "s3cret"}).status_code == 429


def test_sync_api_needs_key(client):
    assert client.get("/api/sync/pull").status_code == 401
    assert client.get("/api/sync/pull", headers={"X-PetTrack-Key": "wrong"}).status_code == 401
    assert client.get("/api/sync/pull", headers={"X-PetTrack-Key": "k" * 16}).status_code == 200


def test_malformed_sync_payload_is_ignored(client):
    h = {"X-PetTrack-Key": "k" * 16}
    r = client.post("/api/sync/push", json={"pets": [{"uid": "../../x", "updated_at": "2026-01-01T00:00:00.000Z"},
                                                      "junk", {"uid": UID}]}, headers=h)
    assert r.status_code == 200 and r.get_json()["applied"]["pets"] == 0
    assert client.post("/api/sync/push", data="nope", headers=h).status_code == 400


# ------------------------------------------------------------- sync: two real instances over HTTP
@pytest.fixture
def pair(tmp_path):
    cloud_cfg = make_cfg(tmp_path, "cloud", mode="cloud", require_login=True, admin_password="pw", cloud_url="")
    cloud = create_app(cloud_cfg, start_services=False)
    server = create_server(cloud, host="127.0.0.1", port=0)
    port = server.effective_port
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    local_cfg = make_cfg(tmp_path, "local", cloud_url=f"http://127.0.0.1:{port}", cloud_timeout=1.5)
    local = create_app(local_cfg, start_services=False)
    yield local, cloud, local_cfg
    server.close()


def test_offline_first_then_sync_then_cloud_edit_comes_back(pair):
    local, cloud, _ = pair
    lc, cc = local.test_client(), cloud.test_client()
    lsync = local.extensions["pt_sync"]

    post(lc, f"/register/{UID}", PET)
    post(lc, f"/pet/{UID}/vaccination", {"vaccine": "Rabies", "given_on": "2026-01-01", "next_due": "2999-01-01"})
    assert lsync.run_once() is True and lsync.state["pushed"] >= 2
    # cloud now serves the public page with the same data
    assert "Bruno" in cc.get(f"/p/{UID}").get_data(as_text=True)
    # nothing left dirty locally
    conn = db.connect(local.config["PT"].db_path)
    assert sum(services.unsynced_counts(conn).values()) == 0
    conn.close()

    # edit on the cloud (log in there), pull it down on the laptop
    post(cc, "/login", {"password": "pw"})
    time.sleep(0.01)
    post(cc, f"/pet/{UID}/edit", {**PET, "name": "Bruno Cloud"})
    assert lsync.run_once() is True
    assert "Bruno Cloud" in lc.get(f"/pet/{UID}").get_data(as_text=True)


def test_laptop_works_when_cloud_down_and_catches_up(pair):
    local, cloud, cfg = pair
    lc = local.test_client()
    lsync = local.extensions["pt_sync"]
    good_url = cfg.cloud_url
    cfg.cloud_url = "http://127.0.0.1:1"                        # cloud unreachable
    post(lc, f"/register/{UID}", PET)                           # still saves locally
    assert lsync.run_once() is False and lsync.state["online"] is False
    assert lc.get(f"/scan/{UID}").headers["Location"].endswith(f"/pet/{UID}")   # falls back to local copy
    conn = db.connect(cfg.db_path)
    assert services.unsynced_counts(conn)["pets"] == 1
    conn.close()
    cfg.cloud_url = good_url                                    # back online
    assert lsync.run_once() is True
    conn = db.connect(cfg.db_path)
    assert sum(services.unsynced_counts(conn).values()) == 0
    conn.close()


def test_cloud_first_lookup_caches_locally(pair):
    local, cloud, _ = pair
    # pet exists only on the cloud (e.g. added from a phone)
    cc = cloud.test_client()
    post(cc, "/login", {"password": "pw"})
    post(cc, f"/register/{UID}", PET)
    lc = local.test_client()
    assert lc.get(f"/scan/{UID}").headers["Location"].endswith(f"/pet/{UID}")
    conn = db.connect(local.config["PT"].db_path)
    assert services.get_pet(conn, UID)["name"] == "Bruno"
    conn.close()


def test_photo_and_deletion_sync(pair):
    from PIL import Image
    local, cloud, _ = pair
    lc, cc = local.test_client(), cloud.test_client()
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), "blue").save(buf, "PNG")
    buf.seek(0)
    post(lc, f"/register/{UID}", {**PET, "photo": (buf, "d.png")}, content_type="multipart/form-data")
    lsync = local.extensions["pt_sync"]
    assert lsync.run_once()
    assert cc.get(f"/photo/{UID}").status_code == 200           # photo travelled with the record
    time.sleep(0.01)
    post(lc, f"/pet/{UID}/delete")
    assert lsync.run_once()
    assert cc.get(f"/p/{UID}").status_code == 404               # deletion propagated


def test_cloud_wipe_triggers_full_reupload(tmp_path):
    def boot(name):
        cfg = make_cfg(tmp_path, name, mode="cloud", require_login=True, admin_password="pw")
        app = create_app(cfg, start_services=False)
        srv = create_server(app, host="127.0.0.1", port=0)
        threading.Thread(target=srv.run, daemon=True).start()
        return app, srv

    cloud1, srv1 = boot("cloud1")
    local_cfg = make_cfg(tmp_path, "local", cloud_url=f"http://127.0.0.1:{srv1.effective_port}", cloud_timeout=1.5)
    local = create_app(local_cfg, start_services=False)
    lc, ls = local.test_client(), local.extensions["pt_sync"]
    post(lc, f"/register/{UID}", PET)
    assert ls.run_once()
    srv1.close()

    cloud2, srv2 = boot("cloud2")                               # brand-new empty cloud database
    local_cfg.cloud_url = f"http://127.0.0.1:{srv2.effective_port}"
    try:
        assert ls.run_once()
        assert "Bruno" in cloud2.test_client().get(f"/p/{UID}").get_data(as_text=True)
    finally:
        srv2.close()


def test_status_api(client):
    d = client.get("/api/status").get_json()
    assert d["mode"] == "local" and d["scanner"]["enabled"] is False and d["sync"]["enabled"] is False


def test_pwa_assets(client):
    assert client.get("/manifest.webmanifest").get_json()["short_name"] == "PetTrack"
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.mimetype
    assert client.get("/offline").status_code == 200
    assert client.get("/static/icon-192.png").status_code == 200


def test_exports(client):
    post(client, f"/register/{UID}", PET)
    assert b"Bruno" in client.get("/export/pets.csv").data
    assert b"Bruno" in client.get("/export/backup.json").data


# ------------------------------------------------------------- real serial worker thread (loopback port)
def test_serial_worker_end_to_end(tmp_path, monkeypatch):
    import serial

    made = []

    def fake_serial(port, baud, **kw):
        s = serial.serial_for_url("loop://", baudrate=baud, timeout=kw.get("timeout", 0.5))
        made.append(s)
        return s

    monkeypatch.setattr(serial, "Serial", fake_serial)
    cfg = make_cfg(tmp_path, "hw", serial_enabled=True, serial_port="loop")
    app = create_app(cfg, start_services=False)
    hw = app.extensions["pt_hw"]
    hw.start()
    try:
        for _ in range(50):
            if made:
                break
            time.sleep(0.1)
        assert made, "worker never opened the port"
        ser = made[0]
        ser.write(b"!HELLO,A1,1.0,9\n")
        ser.write((hardware.make_frame("A1", 9, 1, UID) + "\n").encode())
        for _ in range(50):
            if hw.events_after(0):
                break
            time.sleep(0.1)
        assert hw.events_after(0)[0]["uid"] == UID
        s = hw.snapshot()
        assert s["connected"] and s["device"] == "A1" and s["boot"] == 9
        # the LCD command written back by the worker is visible on the loopback line
        time.sleep(0.3)
        echoed = b""
        while ser.in_waiting:
            echoed += ser.read(ser.in_waiting)
        # worker consumed it (it ignores non-protocol lines) or it is still queued: either way scan was stored
        conn = db.connect(cfg.db_path)
        assert conn.execute("SELECT COUNT(*) FROM scans WHERE id='A1:9:1'").fetchone()[0] == 1
        conn.close()
    finally:
        hw.stop()
