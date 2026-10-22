
#include <LiquidCrystal.h>
#include <SoftwareSerial.h>

// LCD: RS, EN, D4, D5, D6, D7
LiquidCrystal lcd(8, 9, 10, 11, 12, 13);

// Bluetooth: Arduino RX, TX
// HC-05 TX -> Arduino D3
// HC-05 RX remains disconnected
SoftwareSerial BT(3, 4);

// Gas sensors
#define GAS_INLET A0
#define GAS_OUTLET A1

// Fans
#define FAN_INLET 6
#define FAN_OUTLET 7

// PIR motion sensor
#define PIR_SENSOR 2

// Change if your buzzer uses another pin
#define BUZZER 5

#define GAS_THRESHOLD 400

// 10 minutes in milliseconds
const unsigned long INACTIVITY_TIMEOUT = 600000UL;

int inletValue = 0;
int outletValue = 0;

unsigned long lastMotionTime = 0;
unsigned long lastDisplayTime = 0;

bool inactivityShutdown = false;

// -1 = automatic, 0 = manual OFF, 1 = manual ON
int manualMode = -1;

String command = "";

void setFans(bool state) {
  digitalWrite(FAN_INLET, state ? HIGH : LOW);
  digitalWrite(FAN_OUTLET, state ? HIGH : LOW);
}

void setup() {
  pinMode(GAS_INLET, INPUT);
  pinMode(GAS_OUTLET, INPUT);
  pinMode(PIR_SENSOR, INPUT);

  pinMode(FAN_INLET, OUTPUT);
  pinMode(FAN_OUTLET, OUTPUT);
  pinMode(BUZZER, OUTPUT);

  setFans(false);
  digitalWrite(BUZZER, LOW);

  Serial.begin(9600);
  BT.begin(9600);

  lcd.begin(16, 2);
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Smart Air");
  lcd.setCursor(0, 1);
  lcd.print("Purifier");
  delay(2000);
  lcd.clear();

  lastMotionTime = millis();

  Serial.println("Smart Air Purifier Ready");
  Serial.println("Bluetooth commands: ON, OFF, AUTO");
}

void loop() {
  unsigned long now = millis();

  // Read Bluetooth commands
  while (BT.available()) {
    char c = BT.read();

    if (c == '\n' || c == '\r') {
      command.trim();
      command.toUpperCase();

      if (command == "ON") {
        manualMode = 1;
        Serial.println("Manual mode: ON");
      }
      else if (command == "OFF") {
        manualMode = 0;
        Serial.println("Manual mode: OFF");
      }
      else if (command == "AUTO") {
        manualMode = -1;
        lastMotionTime = now;
        inactivityShutdown = false;
        Serial.println("Automatic mode enabled");
      }

      command = "";
    }
    else if (command.length() < 20) {
      command += c;
    }
  }

  // Read motion sensor
  bool motion = digitalRead(PIR_SENSOR) == HIGH;

  if (motion) {
    lastMotionTime = now;
    inactivityShutdown = false;
  }

  // Check inactivity timeout
  if (now - lastMotionTime >= INACTIVITY_TIMEOUT) {
    inactivityShutdown = true;
  }

  // Read air-quality sensors
  inletValue = analogRead(GAS_INLET) / 1.2;
  outletValue = analogRead(GAS_OUTLET);

  // Fan-control priority
  if (manualMode == 1) {
    setFans(true);
  }
  else if (manualMode == 0) {
    setFans(false);
  }
  else if (inactivityShutdown) {
    setFans(false);
  }
  else if (inletValue >= GAS_THRESHOLD) {
    setFans(true);
  }
  else {
    digitalWrite(FAN_INLET, LOW);
    digitalWrite(
      FAN_OUTLET,
      outletValue >= GAS_THRESHOLD ? HIGH : LOW
    );
  }

  // Update LCD every 500 milliseconds
  if (now - lastDisplayTime >= 500) {
    lastDisplayTime = now;

    lcd.setCursor(0, 0);
    lcd.print("IN:");
    lcd.print(inletValue);
    lcd.print("    ");

    lcd.setCursor(9, 0);
    lcd.print("OUT:");
    lcd.print(outletValue);
    lcd.print("  ");

    lcd.setCursor(0, 1);

    if (manualMode == 1) {
      lcd.print("Manual: ON     ");
    }
    else if (manualMode == 0) {
      lcd.print("Manual: OFF    ");
    }
    else if (inactivityShutdown) {
      lcd.print("No Motion: OFF ");
    }
    else if (inletValue >= GAS_THRESHOLD ||
             outletValue >= GAS_THRESHOLD) {
      lcd.print("Purifying Air  ");
    }
    else {
      lcd.print("Air Quality OK ");
    }
  }
}
