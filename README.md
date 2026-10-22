# PetTrack

RFID collar tag → Arduino scanner → laptop web app → optional cloud copy. Works fully offline; syncs when online.

```
 tag ─► Arduino 1 (RC522 + LCD) ──USB──► laptop (Flask + SQLite) ◄──sync──► cloud (same app, mode=cloud)
              │ D1/TX
              └──────────────► Arduino 2 (SD logger) ─► PETLOG.TXT ──(pull card, upload on /import)──► laptop
```

## 1. Hardware

**Arduino 1 (scanner)**: RC522 SDA D10, SCK D13, MOSI D11, MISO D12, RST D9, VCC **3.3 V**, GND. LCD RS D2, E D3, D4–D7 → D4–D7. USB to laptop.
**Arduino 2 (logger)**: SD module CS D8, SCK D13, MOSI D11, MISO D12, VCC 5 V, GND. Own USB power.
**Link**: A1 D1/TX → A2 D0/RX, GND ↔ GND. Never join the 5 V rails.

Do not put RC522 and SD on one board (shared MISO broke RFID reads in your tests).

Upload order: unplug the TX→RX wire → upload `arduino/arduino1_scanner` to board 1 (library: *MFRC522*), `arduino/arduino2_logger` to board 2 → reconnect the wire.
Format the SD card FAT16/FAT32.

Expected on power-up: LCD "PetTrack v1.0 / Starting…" then "Tap Collar Tag..". Board 2's LED (optional, A0→220 Ω→GND) blinks once per logged tap, fast blink = SD problem.

## 2. Laptop

```
pip install -r requirements.txt
python run.py            # or start-local.bat / start-local.sh
```
Open http://localhost:5000. Serial port is auto-detected; force one with `PETTRACK_PORT=COM8`.
Phone on the same wifi: `http://<laptop-ip>:5000`.

Tap a tag: browser jumps to the pet (or the registration form), the LCD shows the pet name, "LOST PET" or vaccine warnings.
No scanner? Type the UID in the box on Home.

> The laptop site has no login by default (anyone on your wifi can edit). Set `PETTRACK_HOST=127.0.0.1` or `PETTRACK_REQUIRE_LOGIN=1` + `PETTRACK_ADMIN_PASSWORD` if that matters.

## 3. Cloud (optional)

Deploy this folder (Docker, or `render.yaml`) with:
`PETTRACK_MODE=cloud`, `PETTRACK_ADMIN_PASSWORD`, `PETTRACK_SYNC_KEY`, `PETTRACK_SECRET_KEY`, data dir on a **persistent disk**.
On the laptop set `PETTRACK_CLOUD_URL` + the same `PETTRACK_SYNC_KEY` (+ `PETTRACK_PUBLIC_URL` = cloud URL so QR codes work for strangers).

- Writes always go to local SQLite first, then upload within seconds (retry every 30 s while offline).
- Scan lookups try the cloud first (3 s timeout), cache the result, fall back to local data.
- Conflicts: newest edit wins. Deletes sync. Photos sync.
- Cloud database wiped (free-tier redeploy)? The laptop notices and re-uploads everything.
- Phone: open the cloud URL, log in, use "Add to Home Screen" (installable app; needs HTTPS).
- Public finder page `/p/<UID>` needs no login; this is what the QR on the tag/collar card opens.

## 4. SD log import (laptop was off)

1. Pull the SD card, open `PETLOG.TXT`.
2. Laptop → **SD import** → choose the file → set "when was the card pulled" (default: now) → Import.
3. Taps already seen live are skipped (same `A1:boot:seq` id). Times are **estimated** from the board's uptime; taps from older power-ons have unknown time and are marked so.
4. Unknown tags from the log show up on Home → "Unregistered tags".

Log line: `dev,boot,seq,uid,ms,sess`.

## 5. Serial protocol

`#A1,<boot>,<seq>,<UID>*<XOR>` (tap, XOR of the text between `#` and `*`), `!HELLO`, `!HB` (every 5 s), `!ERR,RFID`/`!OK,RFID`; laptop → board 1: `@line1|line2` for the LCD. Corrupt frames are dropped on both sides.

## 6. Tests

`pip install -r requirements-dev.txt && pytest` (34 tests: protocol, CRUD, security, SD import, two-server sync, offline fallback, cloud wipe).
Firmware was syntax-checked with stubs only; compile in the Arduino IDE, then run the checklist below.

**Bench checklist**: (1) tap tag → LCD name + page opens; (2) stop `run.py`, tap → board 2 LED blinks, line appears in PETLOG.TXT; (3) import it, no duplicate of step 1's tap; (4) pull SD card while tapping → LED fast blink, replug, taps still logged; (5) unplug laptop wifi, edit a pet, reconnect → "Synced".
