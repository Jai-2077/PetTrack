"""Serial link to Arduino 1 (USB).

Arduino 1 -> laptop lines:
  #A1,<boot>,<seq>,<UID>*<XOR>    a tap (same frame also goes to Arduino 2 for the SD log)
  !HELLO,A1,<fw>,<boot>           sent at boot
  !HB,A1,<boot>,<rfid_ok>         heartbeat every 5 s
  !ERR,RFID / !OK,RFID            reader health changes
Laptop -> Arduino 1:
  @<line1>|<line2>                show text on the LCD for a few seconds
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque

from . import db, services

log = logging.getLogger("pettrack.hw")

FRAME_RE = re.compile(r"^#([A-Za-z0-9_-]{1,8}),(\d{1,10}),(\d{1,10}),([0-9A-F]{8,20})\*([0-9A-F]{2})$")
LEGACY_RE = re.compile(r"^[0-9A-Fa-f]{8,20}$")
KNOWN_VIDS = {0x2341, 0x2A03, 0x1A86, 0x0403, 0x10C4}
PROBE_SECONDS = 8
HEARTBEAT_TIMEOUT = 20


def checksum(body: str) -> int:
    x = 0
    for b in body.encode():
        x ^= b
    return x


def make_frame(dev: str, boot: int, seq: int, uid: str) -> str:
    body = f"{dev},{boot},{seq},{uid}"
    return f"#{body}*{checksum(body):02X}"


def parse_frame(line: str, allow_legacy: bool = False):
    line = line.strip()
    m = FRAME_RE.match(line)
    if m:
        body = line[1:line.rindex("*")]
        if f"{checksum(body):02X}" != m.group(5) or len(m.group(4)) % 2:
            return None
        return {"dev": m.group(1), "boot": int(m.group(2)), "seq": int(m.group(3)), "uid": m.group(4)}
    if allow_legacy and LEGACY_RE.match(line) and len(line) % 2 == 0:
        return {"dev": "", "boot": None, "seq": None, "uid": line.upper()}
    return None


def lcd_safe(s: str) -> str:
    return "".join(c if 32 <= ord(c) < 127 and c not in "|@" else "?" for c in s)[:16]


class HardwareService:
    def __init__(self, cfg, on_scan):
        self.cfg = cfg
        self.on_scan = on_scan
        self.events: deque = deque(maxlen=50)
        self.last_event_id = 0
        self.lock = threading.Lock()
        self._wlock = threading.Lock()
        self._ser = None
        self._locked: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = {"enabled": cfg.serial_enabled, "connected": False, "port": "", "device": "", "fw": "",
                       "boot": None, "rfid_ok": None, "last_seen": "", "error": "", "bad_frames": 0}

    # -- lifecycle
    def start(self) -> None:
        if not self.cfg.serial_enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="pettrack-serial", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.status)

    def _set(self, **kw) -> None:
        with self.lock:
            self.status.update(kw)

    # -- events for the browser
    def events_after(self, after: int) -> list:
        with self.lock:
            return [e for e in self.events if e["id"] > after]

    def _push_event(self, uid, known, name) -> None:
        with self.lock:
            self.last_event_id += 1
            self.events.append({"id": self.last_event_id, "uid": uid, "known": known, "name": name, "ts": db.utcnow()})

    # -- LCD
    def send_display(self, l1: str, l2: str) -> bool:
        ser = self._ser
        if not ser:
            return False
        try:
            with self._wlock:
                ser.write(f"@{lcd_safe(l1)}|{lcd_safe(l2)}\n".encode("ascii", "replace"))
            return True
        except Exception as e:
            log.warning("LCD write failed: %s", e)
            return False

    # -- port discovery
    def _candidates(self) -> list[str]:
        want = self.cfg.serial_port
        if want.lower() != "auto":
            return [want]
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        pref = [p.device for p in ports if p.vid in KNOWN_VIDS or any(
            k in (p.description or "").lower() for k in ("arduino", "ch340", "usb serial", "usb-serial"))]
        if not pref:
            pref = [p.device for p in ports if re.search(r"(COM\d+|ttyUSB|ttyACM|cu\.usb)", p.device)]
        if self._locked in pref:
            pref.remove(self._locked)
            pref.insert(0, self._locked)
        return pref

    # -- worker
    def _run(self) -> None:
        while not self._stop.is_set():
            ports = self._candidates()
            if not ports:
                self._set(connected=False, error="No serial port found. Plug in Arduino 1.")
            for port in ports:
                if self._stop.is_set():
                    break
                try:
                    self._session(port)
                except Exception as e:
                    self._set(error=f"{port}: {e}"[:160])
                    log.warning("serial %s: %s", port, e)
                self._ser = None
                self._set(connected=False)
                self._stop.wait(2)
            self._stop.wait(3)

    def _session(self, port: str) -> None:
        import serial
        explicit = self.cfg.serial_port.lower() != "auto"
        ser = serial.Serial(port, self.cfg.serial_baud, timeout=0.5, write_timeout=1)
        try:
            started = last_rx = time.time()
            verified = False
            seen_proto = False
            buf = b""
            while not self._stop.is_set():
                chunk = ser.read(128)
                now = time.time()
                if chunk:
                    buf = (buf + chunk)[-2048:]
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        kind = self._handle_line(raw.decode("ascii", "ignore").strip(), explicit)
                        if kind:
                            last_rx = now
                            if not verified:
                                verified = True
                                self._ser = ser
                                self._locked = port
                                self._set(connected=True, port=port, error="")
                            if kind == "proto":
                                seen_proto = True
                if not verified and explicit and now - started > 2:
                    verified = True  # explicit port: trust it (old firmware has no heartbeat)
                    self._ser = ser
                    self._set(connected=True, port=port, error="")
                if not verified and now - started > PROBE_SECONDS:
                    raise RuntimeError("no PetTrack scanner on this port")
                if seen_proto and now - last_rx > HEARTBEAT_TIMEOUT:
                    raise RuntimeError("scanner stopped responding")
        finally:
            ser.close()

    def _handle_line(self, line: str, allow_legacy: bool):
        if not line:
            return None
        now = db.utcnow()
        if line.startswith("!"):
            p = line[1:].split(",")
            kind = p[0]
            if kind == "HELLO" and len(p) >= 4:
                self._set(device=p[1], fw=p[2], boot=int(p[3]) if p[3].isdigit() else None, last_seen=now)
            elif kind == "HB":
                self._set(last_seen=now, rfid_ok=(p[3] == "1") if len(p) > 3 else None)
            elif kind in ("ERR", "OK") and len(p) > 1 and p[1] == "RFID":
                self._set(rfid_ok=(kind == "OK"), last_seen=now)
            return "proto"
        if line.startswith("#"):
            frame = parse_frame(line)
            if frame:
                self._set(last_seen=now)
                self.dispatch(frame)
            else:
                with self.lock:
                    self.status["bad_frames"] += 1
            return "proto"
        if allow_legacy:
            frame = parse_frame(line, allow_legacy=True)
            if frame:
                self.dispatch(frame)
                return "legacy"
        return None

    def dispatch(self, frame: dict) -> None:
        try:
            info = self.on_scan(frame)
        except Exception:
            log.exception("scan handler failed")
            return
        self._push_event(info["uid"], info["known"], info["name"])
        self.send_display(info["line1"], info["line2"])


def make_scan_handler(cfg, sync):
    """Returns the callback that stores a hardware tap and works out what the LCD should say."""
    def handle(frame: dict) -> dict:
        conn = db.connect(cfg.db_path)
        try:
            scan_id = f"{frame['dev']}:{frame['boot']}:{frame['seq']}" if frame["boot"] is not None else None
            services.record_scan(conn, frame["uid"], "live", device=frame["dev"], boot=frame["boot"],
                                 dev_seq=frame["seq"], scan_id=scan_id)
            l1, l2, known, name = services.lcd_lines(conn, frame["uid"])
        finally:
            conn.close()
        sync.trigger()
        return {"uid": frame["uid"], "known": known, "name": name, "line1": l1, "line2": l2}
    return handle
