/*
    FILE: AD7193_2026_WIFI renamed from  AD7193_MOM_v01 on June 5, 2024
    AUTHOR: RAM
    DATE: 02/18/2026
    PURPOSE: Implement ability to 1) save only during daylight hours and 2) comm with WIFI to send prev day's data
    STATUS: In progress

    Based on AD7193_MOM_v01 which was operationals from June 2024 thru Dec 2025

    June 5, 2024 - when went operational
    --- renamed AAD7193_MOM_v01 because no longer a beta version. Installed on all R4 arduinos for Kent Island

    Feb 18, 2026
    --- Time the speed HZ before any testing - 51.59 hz (summer were 56.9hz - card difference)
    --- Added time check in loop so it only saves data in day time (default 7AM, 8PM)
    --- Time the speed HZ with time check added - 51.56 - NO TIME ADDED for the check
*/

// libraries needed

#include <PRDC_AD7193.h>
#include <LiquidCrystal.h>
#include <SD.h>
#include <SPI.h>
#include <Wire.h>
#include <WiFiS3.h>
#include <WiFiUdp.h>
#include <limits.h>
#include "Time.h"
#include "RTClib.h"
#include "secrets.h"

// Define variables
// --------------------
File myFile;
String myFilename;

// PCB variable defined by Tacuna code
#define SRAM_CS 1 //Use A0 for Uno R3.  Use 1 for Uno R4
#define SD_CS 10
#define AD7193_CS 0 //Use A1 for Uno R3. Use 0 for Uno R4

// Handle the ADC PCB unit
// PRDC_AD7193 AD7193;

// RTC
RTC_DS1307 RTC;

// Setup time variables that will be used in multiple places
DateTime currenttime;
long int myUnixTime;

// LCD instantiation
const int rs = 9, en = 8, d4 = 5, d5 = 4, d6 = 3, d7 = 2;
LiquidCrystal lcd(rs, en, d4, d5, d6, d7);

// AD7193 ADC scale
PRDC_AD7193 scale;
long int strain;  // value of the scale at any point in time

int tCounter = 0;  // Count # loops we to thru - must be global because don't want to initialize each time
String deviceId;
String deviceID_6;
bool wifiModeActive = false;
bool wifiInitialized = false;
bool sdReady = false;
IPAddress targetIp;
WiFiUDP udp;

const char POLL_MESSAGE[] = "POLL_UID";
const char LIST_FILES_MESSAGE[] = "LIST_FILES";
const char START_FILE_MESSAGE[] = "START_FILE";
const char RESUME_MESSAGE[] = "RESUME";
const char SET_TIME_MESSAGE[] = "SET_TIME";

const uint8_t START_HOUR = 7;
const uint8_t END_HOUR = 19;
const size_t FILE_CHUNK_SIZE = 4096;
const uint32_t TRIM_CALIBRATION_SECONDS = 600UL;
const uint32_t TRIM_START_GUARD_SECONDS = 3600UL;
const uint32_t TRIM_PRE_EVENT_SECONDS = 180UL;
const uint32_t TRIM_POST_EVENT_SECONDS = 600UL;
const long TRIM_TRIGGER_MIN_DELTA = 2000L;
const float TRIM_CAL_SPLIT_FRACTION = 0.35f;
const float TRIM_DEBOUNCE_SECONDS = 0.5f;
const uint32_t TRIM_PROGRESS_ROWS = 50000UL;
const int MAX_TRIM_INTERVALS = 128;
const bool TRIM_USE_TODAY_FILENAME = false;  // false=default to yesterday's DL file, true=use today's DL file for testing

/////////////////////
//  set constants
/////////////////////
// set the samples to average when getting data - thru 2025 was 80 which gave 56.9hz - use 70 -> 59 at home
const int myAVG = 70;

// initialize variables for SD -- use chipSelect = 4 without RTC board
const int chipSelect = 10;  // for the Wigoneer board and Adafruit board

// set the communications speed
const int comLevel = 115200;

// set flag for amount of feedback - false means to give us too much info
const bool verbose = true;

// set flag for printing to LCD
const bool printLCD = true;

// set flag to print somethnig only if debugging
const bool debug = false;

// flag for countdown
const bool countdown = true;

struct TrimInterval {
  uint32_t start_ts;
  uint32_t end_ts;
};

struct CalibrationThresholds {
  bool ok;
  uint32_t firstTs;
  long baselineMean;
  long lowCalibrationMean;
  long enterThreshold;
  long exitThreshold;
};

TrimInterval trimIntervals[MAX_TRIM_INTERVALS];
int trimIntervalCount = 0;

String getChipIdHex() {
  const bsp_unique_id_t *uid = R_BSP_UniqueIdGet();
  char id[33];
  snprintf(
    id, sizeof(id),
    "%08lX%08lX%08lX%08lX",
    (unsigned long) uid->unique_id_words[0],
    (unsigned long) uid->unique_id_words[1],
    (unsigned long) uid->unique_id_words[2],
    (unsigned long) uid->unique_id_words[3]
  );
  return String(id);
}

bool isRtcDateSane(const DateTime &dt) {
  int y = dt.year();
  int m = dt.month();
  int d = dt.day();
  return (y >= 2024 && y <= 2099 && m >= 1 && m <= 12 && d >= 1 && d <= 31);
}

bool readRtcStable(DateTime &out) {
  DateTime prev((uint32_t)0);
  bool hasPrev = false;
  const uint8_t maxReads = 6;
  for (uint8_t i = 0; i < maxReads; ++i) {
    DateTime cur = RTC.now();
    if (!isRtcDateSane(cur)) {
      delay(30);
      continue;
    }
    if (hasPrev) {
      uint32_t tPrev = prev.unixtime();
      uint32_t tCur = cur.unixtime();
      uint32_t dt = (tCur >= tPrev) ? (tCur - tPrev) : (tPrev - tCur);
      if (dt <= 2) {
        out = cur;
        return true;
      }
    }
    prev = cur;
    hasPrev = true;
    delay(30);
  }
  return false;
}

bool beginRtcWithRetry(uint8_t attempts = 3) {
  for (uint8_t i = 0; i < attempts; ++i) {
    if (RTC.begin()) return true;
    delay(100);
  }
  return false;
}

void recoverI2CBus() {
  // Attempt to recover a stuck I2C bus after MCU reset while peripherals remain powered.
  pinMode(SDA, INPUT_PULLUP);
  pinMode(SCL, INPUT_PULLUP);
  delay(2);

  // If SDA is low, pulse SCL to release a stuck slave state machine.
  if (digitalRead(SDA) == LOW) {
    pinMode(SCL, OUTPUT);
    for (uint8_t i = 0; i < 9; ++i) {
      digitalWrite(SCL, HIGH);
      delayMicroseconds(10);
      digitalWrite(SCL, LOW);
      delayMicroseconds(10);
    }
    pinMode(SCL, INPUT_PULLUP);
    delay(2);
  }
}

uint32_t crc32Update(uint32_t crc, const uint8_t *data, size_t len) {
  crc = ~crc;
  for (size_t i = 0; i < len; ++i) {
    crc ^= data[i];
    for (int j = 0; j < 8; ++j) {
      crc = (crc & 1) ? (crc >> 1) ^ 0xEDB88320UL : (crc >> 1);
    }
  }
  return ~crc;
}

String crc32Hex(uint32_t value) {
  char out[9];
  snprintf(out, sizeof(out), "%08lX", (unsigned long) value);
  return String(out);
}

void setLcdStatusLine1(const String &status) {
  if (!printLCD) return;
  lcd.setCursor(0, 0);
  String text = status;
  while (text.length() < 16) text += " ";
  lcd.print(text.substring(0, 16));
}

void sendUdpMessage(const String &msg, const IPAddress &ip, uint16_t port) {
  udp.beginPacket(ip, port);
  udp.print(msg);
  udp.endPacket();
}

int splitCsv(char *input, char *fields[], int maxFields) {
  int count = 0;
  char *savePtr = nullptr;
  char *token = strtok_r(input, ",", &savePtr);
  while (token != nullptr && count < maxFields) {
    fields[count++] = token;
    token = strtok_r(nullptr, ",", &savePtr);
  }
  return count;
}

bool readDataLine(File &f, char *buf, size_t n) {
  if (!f.available()) return false;
  size_t len = f.readBytesUntil('\n', buf, n - 1);
  buf[len] = '\0';
  while (len > 0 && (buf[len - 1] == '\r' || buf[len - 1] == '\n' || buf[len - 1] == ' ' || buf[len - 1] == '\t')) {
    buf[len - 1] = '\0';
    len--;
  }
  return len > 0;
}

bool parseDataCsvLine(char *line, long &valueOut, uint32_t &tsOut) {
  char *comma = strchr(line, ',');
  if (!comma) return false;
  *comma = '\0';
  char *left = line;
  char *right = comma + 1;
  while (*left == ' ' || *left == '\t') left++;
  while (*right == ' ' || *right == '\t') right++;

  char *end1 = nullptr;
  char *end2 = nullptr;
  long value = strtol(left, &end1, 10);
  unsigned long ts = strtoul(right, &end2, 10);
  if (end1 == left || end2 == right || ts == 0UL) return false;
  valueOut = value;
  tsOut = (uint32_t) ts;
  return true;
}

String trimFilenameFromRaw(const String &rawName) {
  if (rawName.length() >= 2 && rawName[0] == 'D' && rawName[1] == 'L') {
    String out = rawName;
    out.setCharAt(0, 'T');
    out.setCharAt(1, 'R');
    return out;
  }
  return "TR_" + rawName;
}

String dlFilenameFromEpoch(uint32_t epoch) {
  DateTime dt(epoch);
  int yy = dt.year() % 100;
  int mm = dt.month();
  int dd = dt.day();
  char name[13];
  snprintf(name, sizeof(name), "DL%02d%02d%02d.TXT", yy, mm, dd);
  return String(name);
}

String trimRawFilenameByDatePolicy() {
  DateTime nowRtc = RTC.now();
  if (!isRtcDateSane(nowRtc)) return "";

  uint32_t nowEpoch = nowRtc.unixtime();
  uint32_t dayOffset = TRIM_USE_TODAY_FILENAME ? 0UL : 86400UL;
  if (nowEpoch <= dayOffset) return "";

  return dlFilenameFromEpoch(nowEpoch - dayOffset);
}

String findLatestRawDlFilename() {
  File root = SD.open("/");
  if (!root || !root.isDirectory()) return "";

  String best = "";
  while (true) {
    File entry = root.openNextFile();
    if (!entry) break;
    if (!entry.isDirectory()) {
      String n = String(entry.name());
      if (n.length() == 11 && n.startsWith("DL") && n.endsWith(".TXT")) {
        if (best.length() == 0 || n > best) best = n;
      }
    }
    entry.close();
  }
  root.close();
  return best;
}

void addTrimInterval(uint32_t startTs, uint32_t endTs) {
  if (endTs <= startTs) return;
  if (trimIntervalCount == 0) {
    trimIntervals[0].start_ts = startTs;
    trimIntervals[0].end_ts = endTs;
    trimIntervalCount = 1;
    return;
  }
  TrimInterval &last = trimIntervals[trimIntervalCount - 1];
  if (startTs <= last.end_ts) {
    if (endTs > last.end_ts) last.end_ts = endTs;
    return;
  }
  if (trimIntervalCount >= MAX_TRIM_INTERVALS) {
    if (endTs > last.end_ts) last.end_ts = endTs;
    return;
  }
  trimIntervals[trimIntervalCount].start_ts = startTs;
  trimIntervals[trimIntervalCount].end_ts = endTs;
  trimIntervalCount++;
}

CalibrationThresholds deriveCalibrationThresholdsFromFile(const String &inputName) {
  CalibrationThresholds r = {false, 0UL, 0L, 0L, 0L, 0L};
  File in = SD.open(inputName.c_str(), FILE_READ);
  if (!in) return r;

  char line[96];
  long value = 0;
  uint32_t ts = 0;
  bool haveFirst = false;
  long calMin = 0;
  long calMax = 0;
  uint32_t firstTs = 0;

  while (readDataLine(in, line, sizeof(line))) {
    if (!parseDataCsvLine(line, value, ts)) continue;
    if (!haveFirst) {
      haveFirst = true;
      firstTs = ts;
      calMin = value;
      calMax = value;
    }
    if (ts > firstTs + TRIM_CALIBRATION_SECONDS) break;
    if (value < calMin) calMin = value;
    if (value > calMax) calMax = value;
  }
  in.close();
  if (!haveFirst) return r;

  long split = calMin + (long) ((float) (calMax - calMin) * TRIM_CAL_SPLIT_FRACTION);
  if (split < calMin + TRIM_TRIGGER_MIN_DELTA) split = calMin + TRIM_TRIGGER_MIN_DELTA;

  in = SD.open(inputName.c_str(), FILE_READ);
  if (!in) return r;

  double baselineMean = 0.0;
  uint32_t baselineN = 0;
  bool inSeg = false;
  long segVals[1200];
  int segValsN = 0;
  long lowMean = LONG_MAX;

  while (readDataLine(in, line, sizeof(line))) {
    if (!parseDataCsvLine(line, value, ts)) continue;
    if (ts > firstTs + TRIM_CALIBRATION_SECONDS) break;

    if (value <= split) {
      baselineN++;
      double delta = (double) value - baselineMean;
      baselineMean += delta / (double) baselineN;
    }

    bool elevated = (value > split);
    if (elevated) {
      if (!inSeg) {
        inSeg = true;
        segValsN = 0;
      }
      if (segValsN < (int) (sizeof(segVals) / sizeof(segVals[0]))) {
        segVals[segValsN++] = value;
      }
    } else if (inSeg) {
      if (segValsN >= 20) {
        int s = (int) (0.30f * (float) segValsN);
        int e = (int) (0.70f * (float) segValsN);
        if (e <= s) e = s + 1;
        long long sum = 0;
        int n = 0;
        for (int i = s; i < e && i < segValsN; i++) {
          sum += segVals[i];
          n++;
        }
        if (n > 0) {
          long m = (long) (sum / n);
          if (m < lowMean) lowMean = m;
        }
      }
      inSeg = false;
      segValsN = 0;
    }
  }
  if (inSeg && segValsN >= 20) {
    int s = (int) (0.30f * (float) segValsN);
    int e = (int) (0.70f * (float) segValsN);
    if (e <= s) e = s + 1;
    long long sum = 0;
    int n = 0;
    for (int i = s; i < e && i < segValsN; i++) {
      sum += segVals[i];
      n++;
    }
    if (n > 0) {
      long m = (long) (sum / n);
      if (m < lowMean) lowMean = m;
    }
  }
  in.close();

  if (baselineN < 10) return r;
  long baseline = (long) baselineMean;
  if (lowMean == LONG_MAX || lowMean <= baseline + TRIM_TRIGGER_MIN_DELTA) {
    lowMean = baseline + TRIM_TRIGGER_MIN_DELTA;
  }

  r.ok = true;
  r.firstTs = firstTs;
  r.baselineMean = baseline;
  r.lowCalibrationMean = lowMean;
  r.enterThreshold = lowMean;
  r.exitThreshold = lowMean;
  return r;
}

bool buildTrimIntervalsForFile(const String &inputName, const CalibrationThresholds &cal) {
  trimIntervalCount = 0;
  File in = SD.open(inputName.c_str(), FILE_READ);
  if (!in) return false;

  char line[96];
  long value = 0;
  uint32_t ts = 0;
  uint32_t firstTs = 0;
  uint32_t lastTs = 0;
  bool haveFirst = false;
  bool eventLatched = false;
  bool captureActive = false;
  uint32_t captureUntil = 0;
  uint32_t currentCaptureStart = 0;
  uint32_t aboveSince = 0;
  uint32_t belowSince = 0;
  uint32_t parsedRows = 0;
  uint32_t nextProgress = TRIM_PROGRESS_ROWS;

  while (readDataLine(in, line, sizeof(line))) {
    if (!parseDataCsvLine(line, value, ts)) continue;
    parsedRows++;
    if (!haveFirst) {
      haveFirst = true;
      firstTs = ts;
      addTrimInterval(firstTs, firstTs + TRIM_CALIBRATION_SECONDS);
    }
    lastTs = ts;

    if (ts <= firstTs + TRIM_CALIBRATION_SECONDS) continue;
    if (ts < firstTs + TRIM_START_GUARD_SECONDS) continue;

    if (value >= cal.enterThreshold) {
      if (aboveSince == 0) aboveSince = ts;
      belowSince = 0;
    } else if (value <= cal.exitThreshold) {
      if (belowSince == 0) belowSince = ts;
      aboveSince = 0;
    } else {
      aboveSince = 0;
      belowSince = 0;
    }

    if (!eventLatched && aboveSince != 0 && (ts - aboveSince) >= (uint32_t) TRIM_DEBOUNCE_SECONDS) {
      eventLatched = true;
      uint32_t preStart = (ts > TRIM_PRE_EVENT_SECONDS) ? (ts - TRIM_PRE_EVENT_SECONDS) : firstTs;
      if (preStart < firstTs) preStart = firstTs;
      if (!captureActive) {
        captureActive = true;
        currentCaptureStart = preStart;
      } else if (preStart < currentCaptureStart) {
        currentCaptureStart = preStart;
      }
      captureUntil = ts + TRIM_POST_EVENT_SECONDS;
      aboveSince = 0;
    }

    if (eventLatched && belowSince != 0 && (ts - belowSince) >= (uint32_t) TRIM_DEBOUNCE_SECONDS) {
      eventLatched = false;
      belowSince = 0;
    }

    if (captureActive && !eventLatched && ts >= captureUntil) {
      addTrimInterval(currentCaptureStart, captureUntil);
      captureActive = false;
    }

    if (parsedRows >= nextProgress) {
      Serial.print(F("TRIM analyze rows="));
      Serial.print(parsedRows);
      Serial.print(F(" intervals="));
      Serial.println(trimIntervalCount);
      nextProgress += TRIM_PROGRESS_ROWS;
    }
  }
  in.close();
  if (!haveFirst) return false;
  if (captureActive) {
    uint32_t endTs = (lastTs > captureUntil) ? captureUntil : lastTs;
    addTrimInterval(currentCaptureStart, endTs);
  }
  return true;
}

bool writeTrimmedFileFromIntervals(const String &inputName, const String &outputName) {
  File in = SD.open(inputName.c_str(), FILE_READ);
  if (!in) return false;
  SD.remove(outputName.c_str());
  File out = SD.open(outputName.c_str(), FILE_WRITE);
  if (!out) {
    in.close();
    return false;
  }

  char line[96];
  long value = 0;
  uint32_t ts = 0;
  int idx = 0;
  uint32_t total = 0;
  uint32_t kept = 0;
  uint32_t nextProgress = TRIM_PROGRESS_ROWS;

  while (readDataLine(in, line, sizeof(line))) {
    char parse[96];
    strncpy(parse, line, sizeof(parse) - 1);
    parse[sizeof(parse) - 1] = '\0';
    if (!parseDataCsvLine(parse, value, ts)) continue;

    total++;
    while (idx < trimIntervalCount && ts > trimIntervals[idx].end_ts) idx++;
    if (idx < trimIntervalCount && ts >= trimIntervals[idx].start_ts && ts <= trimIntervals[idx].end_ts) {
      out.println(line);
      kept++;
    }
    if (total >= nextProgress) {
      Serial.print(F("TRIM write rows="));
      Serial.print(total);
      Serial.print(F(" kept="));
      Serial.println(kept);
      nextProgress += TRIM_PROGRESS_ROWS;
    }
  }
  out.close();
  in.close();
  Serial.print(F("TRIM done rows="));
  Serial.print(total);
  Serial.print(F(" kept="));
  Serial.println(kept);
  return true;
}

bool ensureTrimmedFileReadyForWifi() {
  if (!sdReady) return false;

  String rawName = trimRawFilenameByDatePolicy();
  if (rawName.length() == 0 || !SD.exists(rawName.c_str())) {
    rawName = myFilename;
  }
  if (rawName.length() == 0 || !SD.exists(rawName.c_str())) {
    rawName = findLatestRawDlFilename();
  }
  if (rawName.length() == 0 || !SD.exists(rawName.c_str())) {
    Serial.println(F("TRIM skip: no raw DL file found."));
    return false;
  }

  Serial.print(F("TRIM raw target: "));
  Serial.println(rawName);

  String trimName = trimFilenameFromRaw(rawName);
  if (SD.exists(trimName.c_str())) {
    File f = SD.open(trimName.c_str(), FILE_READ);
    unsigned long sz = f ? (unsigned long) f.size() : 0UL;
    if (f) f.close();
    if (sz > 0) {
      Serial.print(F("TRIM ready: "));
      Serial.println(trimName);
      return true;
    }
  }

  Serial.print(F("TRIM start raw="));
  Serial.print(rawName);
  Serial.print(F(" out="));
  Serial.println(trimName);
  setLcdStatusLine1("Trim: analyze");

  CalibrationThresholds cal = deriveCalibrationThresholdsFromFile(rawName);
  if (!cal.ok) {
    Serial.println(F("TRIM fail: calibration thresholds"));
    setLcdStatusLine1("Trim: fail cal");
    return false;
  }
  Serial.print(F("TRIM thresholds baseline="));
  Serial.print(cal.baselineMean);
  Serial.print(F(" low="));
  Serial.println(cal.lowCalibrationMean);

  if (!buildTrimIntervalsForFile(rawName, cal)) {
    Serial.println(F("TRIM fail: interval build"));
    setLcdStatusLine1("Trim: fail int");
    return false;
  }
  Serial.print(F("TRIM intervals="));
  Serial.println(trimIntervalCount);

  setLcdStatusLine1("Trim: writing");
  if (!writeTrimmedFileFromIntervals(rawName, trimName)) {
    Serial.println(F("TRIM fail: write"));
    setLcdStatusLine1("Trim: fail wr");
    return false;
  }

  setLcdStatusLine1("Trim: complete");
  return true;
}

bool connectWiFi() {
  int status = WiFi.status();
  if (status == WL_NO_MODULE) {
    Serial.println(F("WiFi module not detected."));
    return false;
  }

  while (status != WL_CONNECTED) {
    if (strlen(SECRET_PASS) == 0) {
      status = WiFi.begin(SECRET_SSID);
    } else {
      status = WiFi.begin(SECRET_SSID, SECRET_PASS);
    }
    unsigned long start = millis();
    while ((millis() - start) < 8000UL && WiFi.status() != WL_CONNECTED) {
      delay(200);
    }
    status = WiFi.status();
    if (status != WL_CONNECTED) {
      Serial.println(F("WiFi connect retry..."));
      delay(1000);
    }
  }

  unsigned long ipWaitStart = millis();
  while (WiFi.localIP() == IPAddress(0, 0, 0, 0) && (millis() - ipWaitStart) < 10000UL) {
    delay(100);
  }

  Serial.print(F("WiFi connected. Local IP: "));
  Serial.println(WiFi.localIP());
  return true;
}

bool resolveTargetIp() {
#ifdef UDP_TARGET_HOST
  if (WiFi.hostByName(UDP_TARGET_HOST, targetIp) == 1) {
    Serial.print(F("Resolved host to: "));
    Serial.println(targetIp);
    return true;
  }
#endif
  if (targetIp.fromString(UDP_TARGET_IP)) {
    return true;
  }
  return false;
}

void showWiFiInfo() {
  Serial.print(F("SSID: "));
  Serial.println(WiFi.SSID());
  Serial.print(F("IP: "));
  Serial.println(WiFi.localIP());
  Serial.print(F("RSSI: "));
  Serial.print(WiFi.RSSI());
  Serial.println(F(" dBm"));
  Serial.print(F("UDP local port: "));
  Serial.println(UDP_LOCAL_PORT);
  Serial.print(F("UDP target: "));
  Serial.print(targetIp);
  Serial.print(F(":"));
  Serial.println(UDP_TARGET_PORT);

  if (!printLCD) return;

  lcd.setCursor(0, 0);
  lcd.print("WiFi OK        ");
  lcd.setCursor(0, 1);
  lcd.print(WiFi.localIP().toString().substring(0, 16));
  delay(1200);

  lcd.setCursor(0, 0);
  lcd.print("UDP ");
  lcd.print(UDP_LOCAL_PORT);
  lcd.print("->");
  lcd.print(UDP_TARGET_PORT);
  lcd.print("   ");
  lcd.setCursor(0, 1);
  lcd.print("UID: " + deviceID_6 + "      ");
  delay(1200);
}

void runStartupWiFiCheck() {
  Serial.println(F("Startup WiFi check..."));
  if (!connectWiFi()) {
    Serial.println(F("Startup WiFi check failed: no connection."));
    if (printLCD) {
      lcd.setCursor(0, 0);
      lcd.print("WiFi check fail ");
      lcd.setCursor(0, 1);
      lcd.print("No connection   ");
      delay(3000);
    }
    return;
  }

  bool targetOk = resolveTargetIp();
  Serial.print(F("SSID: "));
  Serial.println(WiFi.SSID());
  Serial.print(F("IP: "));
  Serial.println(WiFi.localIP());
  Serial.print(F("RSSI: "));
  Serial.print(WiFi.RSSI());
  Serial.println(F(" dBm"));
  Serial.print(F("UDP local port: "));
  Serial.println(UDP_LOCAL_PORT);
  Serial.print(F("UDP target: "));
  if (targetOk) {
    Serial.print(targetIp);
  } else {
    Serial.print(F("UNRESOLVED("));
    Serial.print(UDP_TARGET_IP);
    Serial.print(F(")"));
  }
  Serial.print(F(":"));
  Serial.println(UDP_TARGET_PORT);

  if (printLCD) {
    lcd.setCursor(0, 0);
    String line1 = "WiFi " + WiFi.localIP().toString();
    while (line1.length() < 16) line1 += " ";
    lcd.print(line1.substring(0, 16));

    lcd.setCursor(0, 1);
    String line2 = "R" + String(WiFi.RSSI()) + " U:" + deviceID_6;
    while (line2.length() < 16) line2 += " ";
    lcd.print(line2.substring(0, 16));
    delay(3000);
  }

  WiFi.disconnect();
  udp.stop();
  wifiInitialized = false;
  wifiModeActive = false;
  Serial.println(F("Startup WiFi check done."));
}

void enterWifiMode() {
  Serial.println(F("Entering WiFi mode"));
  setLcdStatusLine1("WiFi: connect");
  if (!connectWiFi()) {
    return;
  }
  if (!resolveTargetIp()) {
    Serial.println(F("Invalid UDP target config."));
    return;
  }
  udp.begin(UDP_LOCAL_PORT);
  showWiFiInfo();
  wifiInitialized = true;
  setLcdStatusLine1("WiFi: waiting");
}

void exitWifiMode() {
  Serial.println(F("Exiting WiFi mode"));
  udp.stop();
  WiFi.disconnect();
  wifiInitialized = false;
  setLcdStatusLine1("Data:");
}

void sendFileList(const String &transferId, const IPAddress &replyIp, uint16_t replyPort) {
  if (!sdReady) {
    sendUdpMessage("ERROR," + transferId + ",SD_NOT_READY,SD init failed", replyIp, replyPort);
    return;
  }

  int fileCount = 0;
  File root = SD.open("/");
  if (!root || !root.isDirectory()) {
    sendUdpMessage("ERROR," + transferId + ",SD_OPEN_FAILED,Cannot open root", replyIp, replyPort);
    return;
  }
  while (true) {
    File entry = root.openNextFile();
    if (!entry) break;
    if (!entry.isDirectory()) fileCount++;
    entry.close();
  }
  root.close();

  sendUdpMessage("FILE_LIST_BEGIN," + transferId + "," + String(fileCount), replyIp, replyPort);
  root = SD.open("/");
  while (true) {
    File entry = root.openNextFile();
    if (!entry) break;
    if (!entry.isDirectory()) {
      String msg = "FILE_ITEM,";
      msg += transferId;
      msg += ",";
      msg += entry.name();
      msg += ",";
      msg += String((unsigned long) entry.size());
      msg += ",0";
      sendUdpMessage(msg, replyIp, replyPort);
    }
    entry.close();
  }
  root.close();
  sendUdpMessage("FILE_LIST_END," + transferId, replyIp, replyPort);
}

void sendFileOverTcp(
  const String &transferId,
  const String &filename,
  const IPAddress &controllerIp,
  uint16_t controllerTcpPort,
  unsigned long startOffset,
  const IPAddress &replyIp,
  uint16_t replyPort
) {
  setLcdStatusLine1("Xfer: start");
  if (!sdReady) {
    sendUdpMessage("ERROR," + transferId + ",SD_NOT_READY,SD init failed", replyIp, replyPort);
    return;
  }

  String path = filename;
  if (!path.startsWith("/")) path = "/" + path;
  File file = SD.open(path.c_str(), FILE_READ);
  if (!file) {
    sendUdpMessage("ERROR," + transferId + ",FILE_NOT_FOUND," + filename, replyIp, replyPort);
    return;
  }

  unsigned long fileSize = (unsigned long) file.size();
  if (startOffset > fileSize || !file.seek(startOffset)) {
    file.close();
    sendUdpMessage("ERROR," + transferId + ",BAD_OFFSET," + String(startOffset), replyIp, replyPort);
    return;
  }

  String info = "FILE_INFO,";
  info += transferId;
  info += ",";
  info += filename;
  info += ",";
  info += String(fileSize);
  info += ",";
  info += String((unsigned long) FILE_CHUNK_SIZE);
  sendUdpMessage(info, replyIp, replyPort);

  WiFiClient client;
  if (!client.connect(controllerIp, controllerTcpPort)) {
    file.close();
    sendUdpMessage("ERROR," + transferId + ",TCP_CONNECT_FAILED," + String(controllerTcpPort), replyIp, replyPort);
    return;
  }
  setLcdStatusLine1("Xfer: sending");

  uint8_t buffer[FILE_CHUNK_SIZE];
  char header[128];
  unsigned long offset = startOffset;
  unsigned long chunkIndex = 0;
  uint32_t fullCrc = 0;
  unsigned long nextProgressOffset = startOffset + 262144UL;  // 256 KB

  while (offset < fileSize) {
    if (!client.connected()) {
      client.stop();
      file.close();
      sendUdpMessage("ERROR," + transferId + ",TCP_DISCONNECTED," + String(offset), replyIp, replyPort);
      return;
    }

    size_t toRead = FILE_CHUNK_SIZE;
    unsigned long remaining = fileSize - offset;
    if (remaining < toRead) toRead = remaining;

    int bytesRead = file.read(buffer, (int) toRead);
    if (bytesRead <= 0) {
      client.stop();
      file.close();
      sendUdpMessage("ERROR," + transferId + ",FILE_READ_FAILED," + String(offset), replyIp, replyPort);
      return;
    }

    uint32_t chunkCrc = crc32Update(0, buffer, (size_t) bytesRead);
    fullCrc = crc32Update(fullCrc, buffer, (size_t) bytesRead);

    snprintf(
      header, sizeof(header),
      "CHUNK,%s,%lu,%lu,%d,%08lX\n",
      transferId.c_str(),
      chunkIndex,
      offset,
      bytesRead,
      (unsigned long) chunkCrc
    );
    if (client.print(header) <= 0) {
      client.stop();
      file.close();
      sendUdpMessage("ERROR," + transferId + ",TCP_HEADER_WRITE_FAILED," + String(offset), replyIp, replyPort);
      return;
    }

    size_t written = 0;
    unsigned long writeStart = millis();
    while (written < (size_t) bytesRead) {
      if (!client.connected()) break;
      int n = client.write(buffer + written, (size_t) bytesRead - written);
      if (n > 0) {
        written += (size_t) n;
        writeStart = millis();
      } else {
        if ((millis() - writeStart) > 5000UL) break;
        delay(1);
      }
    }
    if (written != (size_t) bytesRead) {
      client.stop();
      file.close();
      sendUdpMessage("ERROR," + transferId + ",TCP_WRITE_TIMEOUT," + String(offset), replyIp, replyPort);
      return;
    }

    offset += (unsigned long) bytesRead;
    chunkIndex++;

    if (offset >= nextProgressOffset || offset >= fileSize) {
      Serial.print(F("XFER "));
      Serial.print(offset);
      Serial.print(F("/"));
      Serial.println(fileSize);
      nextProgressOffset += 262144UL;
    }
  }

  String eof = "EOF,";
  eof += transferId;
  eof += ",";
  eof += String(fileSize);
  eof += ",";
  eof += crc32Hex(fullCrc);
  eof += "\n";
  client.print(eof);
  client.stop();
  file.close();
  sendUdpMessage("FILE_SENT," + transferId + "," + String(fileSize) + "," + crc32Hex(fullCrc), replyIp, replyPort);
  setLcdStatusLine1("Xfer: done");
}

void serviceWifiCommands() {
  if (!wifiInitialized) return;
  int packetSize = udp.parsePacket();
  if (packetSize <= 0) return;

  char incoming[192];
  int n = udp.read(incoming, sizeof(incoming) - 1);
  if (n < 0) return;
  incoming[n] = '\0';
  while (n > 0 && (incoming[n - 1] == '\n' || incoming[n - 1] == '\r' || incoming[n - 1] == ' ')) {
    incoming[n - 1] = '\0';
    n--;
  }

  IPAddress remoteIp = udp.remoteIP();
  uint16_t remotePort = udp.remotePort();

  if (strcmp(incoming, POLL_MESSAGE) == 0) {
    String response = "ID,";
    response += deviceId;
    response += ",";
    response += WiFi.localIP().toString();
    response += ",";
    response += UDP_TARGET_IP;
    response += ",";
    response += String(UDP_TARGET_PORT);
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  char parseBuf[192];
  strncpy(parseBuf, incoming, sizeof(parseBuf) - 1);
  parseBuf[sizeof(parseBuf) - 1] = '\0';
  char *fields[6] = {nullptr};
  int fieldCount = splitCsv(parseBuf, fields, 6);

  if (fieldCount >= 1 && strcmp(fields[0], LIST_FILES_MESSAGE) == 0) {
    String transferId = (fieldCount >= 2) ? String(fields[1]) : String("T0");
    sendFileList(transferId, remoteIp, remotePort);
    return;
  }

  if (fieldCount >= 4 && strcmp(fields[0], START_FILE_MESSAGE) == 0) {
    String transferId = String(fields[1]);
    String filename = String(fields[2]);
    uint16_t tcpPort = (uint16_t) atoi(fields[3]);
    unsigned long offset = 0;
    if (fieldCount >= 5) {
      offset = strtoul(fields[4], nullptr, 10);
    }
    sendFileOverTcp(transferId, filename, remoteIp, tcpPort, offset, remoteIp, remotePort);
    return;
  }

  if (fieldCount >= 3 && strcmp(fields[0], RESUME_MESSAGE) == 0) {
    String transferId = String(fields[1]);
    sendUdpMessage("ACK_RESUME_HINT," + transferId + ",USE_START_FILE_WITH_OFFSET", remoteIp, remotePort);
    return;
  }

  if (fieldCount >= 2 && strcmp(fields[0], SET_TIME_MESSAGE) == 0) {
    unsigned long epoch = strtoul(fields[1], nullptr, 10);
    if (epoch > 0) {
      RTC.adjust(DateTime((uint32_t) epoch));
      delay(50);
      DateTime verified((uint32_t)0);
      bool ok = readRtcStable(verified);
      if (ok) {
        uint32_t r = verified.unixtime();
        uint32_t diff = (r >= epoch) ? (r - epoch) : (epoch - r);
        if (diff <= 5UL) {
          sendUdpMessage("ACK_TIME," + String(epoch), remoteIp, remotePort);
          Serial.print(F("RTC set from controller epoch: "));
          Serial.println(epoch);
          Serial.print(F("RTC now: "));
          Serial.println(verified.timestamp(DateTime::TIMESTAMP_FULL));
        } else {
          sendUdpMessage("ERR_TIME,VERIFY_MISMATCH", remoteIp, remotePort);
          Serial.print(F("RTC verify mismatch. epoch="));
          Serial.print(epoch);
          Serial.print(F(" rtc="));
          Serial.println((unsigned long) r);
        }
      } else {
        sendUdpMessage("ERR_TIME,VERIFY_FAILED", remoteIp, remotePort);
        Serial.println(F("RTC verify failed after SET_TIME."));
      }
    } else {
      sendUdpMessage("ERR_TIME,BAD_EPOCH", remoteIp, remotePort);
    }
    return;
  }
}

void runAcquisitionCycle(uint32_t unixTs) {
  unsigned long sampleValue;
  File dataFile = SD.open(myFilename, FILE_WRITE);

  if (dataFile) {
    for (int i = 0; i < 300; i++) {
      sampleValue = Get_Data();
      dataFile.print(sampleValue);
      dataFile.print(", ");
      dataFile.println(unixTs);

      if (debug) {
        Serial.print(sampleValue);
        Serial.print(", ");
        Serial.println(unixTs);
      }
    }

    lcd.setCursor(6, 0);
    lcd.print(sampleValue);
    dataFile.close();
  } else {
    Serial.println("Error opening file ");
    lcd.setCursor(0, 0);
    lcd.print("Can't open file!!");
    delay(2000);
    lcd.setCursor(0, 1);
    lcd.print("Figure it out!");
    delay(10000);
  }
}



////////////////////
//  Get_Data - encapsulate data access to test different ideas - for now, very simple - would it be faster if we didn't use it at all?
////
long int Get_Data() {
  // return (scale.read()); // different mode of the amplifier? From chris Lange
  return scale.continuousReadAverage(myAVG);
}

////////////////////
//  Get_TimeStamp - encapsulate data in case we change libraries
////
long int Get_TimeStamp() {
  /* GET CURRENT TIME FROM RTC */
  currenttime = RTC.now();
  // convert to raw unix value for return - could give options
  return (currenttime.unixtime());
}

////////////////////
//  Get_TimeStampString - encapsulate data in case we change the way we record time
////
String Get_TimeStampString() {
  /* GET CURRENT TIME FROM RTC */
  currenttime = RTC.now();
  return(currenttime.timestamp(DateTime::TIMESTAMP_TIME));
}

////////////////////
//  Get_TimeStampString - encapsulate data in case we change the way we record time
////
bool IsBetweenHours(uint32_t unixTs, uint8_t startHour = START_HOUR, uint8_t endHour = END_HOUR) {
  uint32_t secOfDay = unixTs % 86400UL;
  uint32_t start = (uint32_t)startHour * 3600UL;
  uint32_t end   = (uint32_t)endHour   * 3600UL;
  return (secOfDay >= start && secOfDay < end);
}


///////////////////
// File name function - based on RTC time - so that we have a new filename for every day "DL_MM_DD.txt"
/////////
String rtnFilename() {
  DateTime nowRtc = RTC.now();
  if (!isRtcDateSane(nowRtc)) {
    // Fallback so we never emit malformed filenames from a bad RTC read.
    nowRtc = DateTime(F(__DATE__), F(__TIME__));
  }
  int yy = nowRtc.year() % 100;
  int mm = nowRtc.month();
  int dd = nowRtc.day();

  char name[13]; // "DLYYMMDD.TXT" + null
  snprintf(name, sizeof(name), "DL%02d%02d%02d.TXT", yy, mm, dd);
  return String(name);
}


void setup() {

  ////////// 
  // Set CS pins high as soon as we can - from TACUNA
  ////////
  pinMode(SRAM_CS, OUTPUT); 
  digitalWrite(SRAM_CS, HIGH);

  pinMode(SD_CS, OUTPUT); 
  digitalWrite(SD_CS, HIGH);

  pinMode(AD7193_CS, OUTPUT); 
  digitalWrite(AD7193_CS, HIGH);

  // Communication settings
  Serial.begin(115200);
  delay(500); // give time for serial to start up
  Serial.println("setup lcd");
  deviceId = getChipIdHex();
  deviceID_6 = (deviceId.length() >= 6) ? deviceId.substring(deviceId.length() - 6) : deviceId;
  Serial.print("Device ID: ");
  Serial.println(deviceId);

  ///////////////////////////
  // setup LCD
  ///////////////////////////
    // set up the LCD's number of columns and rows:
  lcd.begin(16, 2);

  lcd.setCursor(0,0);     // user feedback in the field
  lcd.print("Checking...");  
  delay(1000);
  lcd.setCursor(0,0);
  lcd.print("                "); 

  ///////////////////////////
  // setup ADC AD7193 on PCB from Tacuna code
  ///////////////////////////
  scale.setSPI(SPI);
    if(!scale.begin(AD7193_CS, PIN_SPI_MISO)) {
      Serial.println(F("AD7193 initialization failed!"));

    } else {
      scale.printAllRegisters();
      scale.setClockMode(AD7193_CLK_INT);
      scale.setRate(0x001);
      scale.setFilter(AD7193_MODE_SINC4);
      scale.enableNotchFilter(false);     // learn what this will do
      scale.enableChop(false);
      scale.enableBuffer(true);
      scale.rangeSetup(0, AD7193_CONF_GAIN_128);
      scale.channelSelect(AD7193_CH_0);
      Serial.println(F("AD7193 Initialized!"));
    }


  ///////////////////////////
  // RTC - Setup - turn on and off with flag
  //.    May 7, 2024 - disable 
  ///////////////////////////

  Serial.println("RTC setup");
  recoverI2CBus();
  Wire.end();
  delay(10);
  Wire.begin();
  delay(200);
  if (!beginRtcWithRetry(3)) {
    Serial.println("RTC failed after retries.");
    lcd.print("                ");
    lcd.setCursor(0, 0);
    lcd.print("RTC begin fail! ");
    lcd.setCursor(0, 1);
    lcd.print("Check wiring    ");
    while (1) { delay(1000); }
  }

  DateTime rtcNow((uint32_t)0);
  bool rtcStable = readRtcStable(rtcNow);
  if (!RTC.isrunning() || !rtcStable) {
    Serial.println("RTC invalid/unset. Applying compile time.");
    RTC.adjust(DateTime(F(__DATE__), F(__TIME__)));
    delay(50);

    DateTime verify((uint32_t)0);
    if (!readRtcStable(verify)) {
      Serial.println("RTC verify failed after adjust.");
      lcd.print("                ");
      lcd.setCursor(0, 0);
      lcd.print("RTC verify fail ");
      lcd.setCursor(0, 1);
      lcd.print("Check module    ");
      while (1) { delay(1000); }
    }
    rtcNow = verify;
    Serial.print("RTC set. TIMESTAMP:\t");
    Serial.println(rtcNow.timestamp(DateTime::TIMESTAMP_FULL));
  } else {
    Serial.print("RTC running. TIMESTAMP:\t");
    Serial.println(rtcNow.timestamp(DateTime::TIMESTAMP_FULL));
  }

  lcd.print("                ");
  lcd.setCursor(0, 0);
  lcd.print("RTC Running!    ");
  lcd.setCursor(0, 1);
  lcd.print(String(rtcNow.timestamp(DateTime::TIMESTAMP_FULL)).substring(0, 16));
  delay(3000);


  ///////////////////////////
  // setup SD Card - turn on and off with flag
  ///////////////////////////


  if (debug) {
    // myFilename = "Test_tm.txt";  // use this if you want a custom name
    myFilename = rtnFilename();  // use this if debugging and want to have the autonamed file - NEED RTC running to make it work
  } else {
    myFilename = rtnFilename();
  }

  Serial.print("Saving to: ");
  Serial.println(myFilename);

  // setup lcd for user feedback
  lcd.setCursor(0, 0);

  // initialize the SD card process with user feedback
  Serial.print("Initializing SD card...");

  delay(1000);

  // see if the card is present and can be initialized:
  sdReady = SD.begin(chipSelect);
  if (!sdReady) {
    Serial.println("Card failed, or not present");  // don't do anything more:
    lcd.print("                ");
    lcd.setCursor(0, 0);
    lcd.print("Card failed!");
    lcd.setCursor(1, 1);
    lcd.print("Disconnect!     ");
    delay(10000);

    while (1)
      ;
    Serial.println("card initialized.");  // confirm that it is good to go
  }                                       // end of checking for card and initializing



  if (verbose) {  // give full feedback on status to the user

    float wt_Check01;  // track current weight
    float wt_Check02;  // track the raw values from load cell
    float wt_Check03;  // track the raw values from load cell

    Serial.println("Before setting up the scale:");
    Serial.print("read: \t\t\t");
    Serial.println(scale.singleConversion());  // print a raw reading from the ADC

    Serial.print("read average: \t\t");
    Serial.println(Get_Data());  // print the average of normal sample of readings from the ADC

    wt_Check01 = scale.singleConversion();
    Serial.print("Check 01: \t\t");
    Serial.println(wt_Check01);
    delay(200);
    wt_Check02 = scale.singleConversion();
    Serial.print("Check 02: \t\t");
    Serial.println(wt_Check02);
    delay(200);
    wt_Check03 = scale.singleConversion();
    Serial.print("Check 03: \t\t");
    Serial.println(wt_Check03);
    delay(200);

    if ((wt_Check01 == wt_Check02) & (wt_Check02 == wt_Check03)) {
      Serial.print("We have a problem; load cell always reads: ");
      Serial.println(wt_Check01);
      lcd.setCursor(1, 0);
      lcd.print("Load cell Problem!");
      lcd.setCursor(1, 1);
      lcd.print("Disconnnect!    ");
      delay(10000);
    } else {
      lcd.setCursor(0, 0);
      lcd.print("Load cell works");
      Serial.println("Load cell working properly.");
      delay(1000);
    }

  if(countdown){
    int N = 10;
    for (int i = 1; i < N; i++) {
      lcd.setCursor(0, 1);
      lcd.print("Start in: ");
      lcd.print(N - i);
      lcd.print(" secs");
      delay(800);
    }
  }
    
  }

  // turn off the lcd?
  if (!printLCD) {
    // lcd.noBacklight();
    // lcd.noDisplay();
  } else {
    lcd.setCursor(0, 0);
    lcd.print("Data:           ");
    lcd.setCursor(0, 1);
    lcd.print("UID: " + deviceID_6 + "      ");
    lcd.setCursor(6, 0);
    lcd.print(Get_Data());  // do this while we are messing with closing the datafile
  }

  runStartupWiFiCheck();

  // Re-initialize SD after startup WiFi check to avoid SPI/driver state issues
  // before entering acquisition mode.
  sdReady = SD.begin(chipSelect);
  if (!sdReady) {
    Serial.println(F("SD re-init failed after WiFi check."));
    if (printLCD) {
      lcd.setCursor(0, 0);
      lcd.print("SD re-init fail ");
      lcd.setCursor(0, 1);
      lcd.print("Disconnect!     ");
    }
    while (1) { delay(1000); }
  }
  myFilename = rtnFilename();
  Serial.print(F("Saving to: "));
  Serial.println(myFilename);

  if (printLCD) {
    lcd.setCursor(0, 0);
    lcd.print("Data:           ");
    lcd.setCursor(0, 1);
    lcd.print("UID: " + deviceID_6 + "      ");
  }
}


void loop() {
  tCounter = tCounter + 1;
  uint32_t unixTs = Get_TimeStamp();
  bool inWifiWindow = IsBetweenHours(unixTs);

  if (inWifiWindow && !wifiModeActive) {
    if (!ensureTrimmedFileReadyForWifi()) {
      delay(1000);
      return;
    }
    enterWifiMode();
    wifiModeActive = wifiInitialized;
  } else if (!inWifiWindow && wifiModeActive) {
    exitWifiMode();
    wifiModeActive = false;
  }

  if (wifiModeActive) {
    serviceWifiCommands();
    return;
  }

  runAcquisitionCycle(unixTs);
}


// -- END OF FILE --
