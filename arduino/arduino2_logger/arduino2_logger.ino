/*
 * PetTrack - Arduino 2: SD card logger                firmware 1.0
 *
 * Listens on the hardware UART (D0/RX) for the frames Arduino 1 sends,
 *     #<dev>,<boot>,<seq>,<UID>*<XOR>\n
 * checks the checksum, and appends one CSV line per tap to PETLOG.TXT:
 *     dev,boot,seq,uid,ms,sess
 *   ms   = this board's uptime when the frame arrived (the SD card has no real clock)
 *   sess = this board's power-on counter (EEPROM), so the laptop can tell power-ons apart
 * Anything that is not a '#' frame (heartbeats '!HB...', boot text, noise) is ignored, which is
 * what caused the old INVALID_UID messages. The parser resynchronises on every '#'.
 *
 * Reliability
 *   - the file is opened, written and closed per tap, so pulling power loses nothing already logged
 *   - taps are queued (6 deep) while the card is missing or failing; the card is re-initialised
 *     every 2 s and the queue is flushed when it comes back
 *
 * Wiring
 *   SD module : CS D8, SCK D13, MOSI D11, MISO D12, VCC 5V (HW-125 has a regulator), GND
 *   Link      : Arduino 1 D1/TX -> this board's D0/RX, GND <-> GND (unplug D0 while uploading)
 *   Optional  : status LED + 220R from A0 to GND (blinks per logged tap, fast blink = SD problem)
 * Power both boards separately over USB; never join the two 5V rails.
 * Debug text goes out of this board's TX over USB only (Arduino 1 never listens to it).
 * Library: SD (built in).
 */
#include <SPI.h>
#include <SD.h>
#include <EEPROM.h>

#define FW_VERSION "1.0"
#define LOG_FILE   "PETLOG.TXT"

const uint8_t SD_CS = 8;
const uint8_t STATUS_LED = A0;                    // optional, harmless if nothing is attached
const unsigned long BAUD_RATE = 9600UL;
const unsigned long SD_RETRY_MS = 2000UL;
const unsigned long LED_PULSE_MS = 80UL;
const uint8_t QUEUE_SIZE = 6;
const uint8_t RX_MAX = 63;

struct Entry {
  char dev[9];
  uint32_t boot;
  uint32_t seq;
  char uid[21];
  uint32_t ms;
};

struct SessionRecord {
  uint16_t magic;
  uint32_t count;
};
const uint16_t SESSION_MAGIC = 0x5053;  // "PS"

Entry queue[QUEUE_SIZE];
uint8_t qHead = 0, qCount = 0;
uint32_t sessionId = 0;
uint16_t droppedCount = 0;

bool sdReady = false;
unsigned long nextSdAttempt = 0;
unsigned long ledOffAt = 0;
unsigned long lastBlink = 0;

char rx[RX_MAX + 1];
uint8_t rxLen = 0;
bool inFrame = false;

// ---- helpers ------------------------------------------------------------------------------
void loadSession() {
  SessionRecord rec;
  EEPROM.get(0, rec);
  if (rec.magic != SESSION_MAGIC) {
    rec.magic = SESSION_MAGIC;
    rec.count = 0;
  }
  rec.count++;
  EEPROM.put(0, rec);
  sessionId = rec.count;
}

void ledPulse() {
  digitalWrite(STATUS_LED, HIGH);
  ledOffAt = millis() + LED_PULSE_MS;
}

bool allDigits(const char *s) {
  if (!*s || strlen(s) > 10) return false;
  for (; *s; s++) if (*s < '0' || *s > '9') return false;
  return true;
}

bool validDev(const char *s) {
  size_t n = strlen(s);
  if (n < 1 || n > 8) return false;
  for (; *s; s++) {
    char c = *s;
    if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '_' || c == '-')) return false;
  }
  return true;
}

bool validUid(const char *s) {
  size_t n = strlen(s);
  if (n < 8 || n > 20 || (n % 2)) return false;  // 4, 7 or 10 byte UIDs
  for (; *s; s++) {
    char c = *s;
    if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'F'))) return false;
  }
  return true;
}

// Parses "dev,boot,seq,uid*XX" (without the leading '#'). Modifies s.
bool parseFrame(char *s, Entry &e) {
  char *star = strrchr(s, '*');
  if (!star) return false;
  *star = '\0';
  const char *csText = star + 1;
  if (strlen(csText) != 2) return false;

  uint8_t cs = 0;
  for (const char *p = s; *p; p++) cs ^= (uint8_t)*p;
  char *endp;
  long given = strtol(csText, &endp, 16);
  if (*endp != '\0' || given != (long)cs) return false;

  char *dev = strtok(s, ",");
  char *boot = strtok(NULL, ",");
  char *seq = strtok(NULL, ",");
  char *uid = strtok(NULL, ",");
  if (!dev || !boot || !seq || !uid || strtok(NULL, ",")) return false;
  if (!validDev(dev) || !allDigits(boot) || !allDigits(seq) || !validUid(uid)) return false;

  strncpy(e.dev, dev, sizeof(e.dev) - 1);
  e.dev[sizeof(e.dev) - 1] = '\0';
  e.boot = strtoul(boot, NULL, 10);
  e.seq = strtoul(seq, NULL, 10);
  strncpy(e.uid, uid, sizeof(e.uid) - 1);
  e.uid[sizeof(e.uid) - 1] = '\0';
  e.ms = millis();
  return true;
}

void enqueue(const Entry &e) {
  if (qCount == QUEUE_SIZE) {      // full: drop the oldest, count it
    qHead = (qHead + 1) % QUEUE_SIZE;
    qCount--;
    droppedCount++;
    Serial.println(F("[SD] queue full, dropped oldest tap"));
  }
  queue[(qHead + qCount) % QUEUE_SIZE] = e;
  qCount++;
}

void popQueue() {
  qHead = (qHead + 1) % QUEUE_SIZE;
  qCount--;
}

// ---- serial receive: state machine, resyncs on '#', ignores everything else ----------------
void readSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '#') {                // start (or restart) of a frame
      inFrame = true;
      rxLen = 0;
    } else if (!inFrame) {
      continue;                    // heartbeat text, boot messages, noise
    } else if (c == '\n' || c == '\r') {
      if (rxLen > 0) {
        rx[rxLen] = '\0';
        Entry e;
        if (parseFrame(rx, e)) {
          enqueue(e);
        } else {
          Serial.println(F("[SD] bad frame ignored"));
        }
      }
      inFrame = false;
      rxLen = 0;
    } else if (rxLen < RX_MAX) {
      rx[rxLen++] = c;
    } else {                       // too long to be ours
      inFrame = false;
      rxLen = 0;
    }
  }
}

// ---- SD ------------------------------------------------------------------------------------
bool openCard() {
  if (!SD.begin(SD_CS)) return false;
  if (!SD.exists(LOG_FILE)) {
    File f = SD.open(LOG_FILE, FILE_WRITE);
    if (!f) return false;
    f.println(F("dev,boot,seq,uid,ms,sess"));
    f.close();
  }
  return true;
}

bool writeEntry(const Entry &e) {
  File f = SD.open(LOG_FILE, FILE_WRITE);
  if (!f) return false;
  char line[72];
  int n = snprintf(line, sizeof(line), "%s,%lu,%lu,%s,%lu,%lu", e.dev, (unsigned long)e.boot, (unsigned long)e.seq,
                   e.uid, (unsigned long)e.ms, (unsigned long)sessionId);
  size_t written = f.println(line);
  f.close();
  return n > 0 && written >= (size_t)n;
}

// ---- Arduino entry points ------------------------------------------------------------------
void setup() {
  Serial.begin(BAUD_RATE);
  pinMode(STATUS_LED, OUTPUT);
  digitalWrite(STATUS_LED, LOW);

  // keep the RC522's chip select (if wired to this board by accident) quiet
  pinMode(10, OUTPUT);
  digitalWrite(10, HIGH);
  pinMode(SD_CS, OUTPUT);
  digitalWrite(SD_CS, HIGH);

  loadSession();
  SPI.begin();

  Serial.print(F("[SD] PetTrack logger "));
  Serial.print(F(FW_VERSION));
  Serial.print(F(", session "));
  Serial.println((unsigned long)sessionId);

  sdReady = openCard();
  nextSdAttempt = millis() + SD_RETRY_MS;
  Serial.println(sdReady ? F("[SD] card ready") : F("[SD] card NOT ready, will keep retrying"));
}

void loop() {
  unsigned long now = millis();

  readSerial();

  if (!sdReady && (long)(now - nextSdAttempt) >= 0) {
    sdReady = openCard();
    nextSdAttempt = now + SD_RETRY_MS;
    if (sdReady) Serial.println(F("[SD] card ready"));
  }

  if (sdReady && qCount > 0) {
    if (writeEntry(queue[qHead])) {
      Serial.print(F("[SD] logged "));
      Serial.println(queue[qHead].uid);
      popQueue();
      ledPulse();
    } else {
      sdReady = false;             // card pulled or failing: retry init later, keep the queue
      nextSdAttempt = now + SD_RETRY_MS;
      Serial.println(F("[SD] write failed, re-initialising"));
    }
  }

  if (!sdReady && now - lastBlink >= 250) {   // fast blink = problem
    lastBlink = now;
    digitalWrite(STATUS_LED, !digitalRead(STATUS_LED));
  } else if (sdReady && ledOffAt && (long)(now - ledOffAt) >= 0) {
    digitalWrite(STATUS_LED, LOW);
    ledOffAt = 0;
  }
}
