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

    May 20, 2026
    --- Added check for empty DL file to prevent endless failed loop in trim process when no raw files are present. If no raw files, will skip trim and proceed to WiFi mode with empty file, which controller can handle as a no-data day.
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

// Runtime state and hardware handles.
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

// Main loop cycle counter (debug/visibility only).
int tCounter = 0;
String deviceId;
String deviceID_6;
// Startup calibration-window state machine flags.
bool startupCalWindowInitialized = false;
bool startupCalWindowComplete = false;
uint32_t startupCalWindowEndTs = 0;
bool bootedInWifiWindow = false;
bool wifiSessionArmed = false;
bool wifiIdleAnnounced = false;
uint32_t wifiOutWindowSinceTs = 0;
// Error tracking for diagnostics
uint32_t i2cErrorCount = 0;
uint32_t rtcErrorCount = 0;
uint32_t sdErrorCount = 0;

const char POLL_MESSAGE[] = "POLL_UID";
const char LIST_FILES_MESSAGE[] = "LIST_FILES";
const char DELETE_FILE_MESSAGE[] = "DELETE_FILE";
const char START_FILE_MESSAGE[] = "START_FILE";
const char RESUME_MESSAGE[] = "RESUME";
const char SET_TIME_MESSAGE[] = "SET_TIME";
const char GET_TIME_MESSAGE[] = "GET_TIME";
const char PING_MESSAGE[] = "PING";
const char GET_STATUS_MESSAGE[] = "GET_STATUS";
const char GET_CONFIG_MESSAGE[] = "GET_CONFIG";
const char GET_DIAGNOSTICS_MESSAGE[] = "GET_DIAGNOSTICS";
const char GET_LAST_DATA_MESSAGE[] = "GET_LAST_DATA";
const char GET_NET_UID_MESSAGE[] = "GET_NET_UID";
const char GET_VERSION_MESSAGE[] = "GET_VERSION";
const char SET_CONFIG_MESSAGE[] = "SET_CONFIG";
const char REBOOT_MESSAGE[] = "REBOOT";
const char ENTER_DATA_MODE_MESSAGE[] = "ENTER_DATA_MODE";
const char GET_LOGS_MESSAGE[] = "GET_LOGS";
const char CLEAR_ERRORS_MESSAGE[] = "CLEAR_ERRORS";

// Cached RTC time support
uint32_t rtcBaseUnixTs = 0;          // Unix time from last successful RTC read
uint32_t rtcBaseMs = 0;              // millis() at last successful RTC read
const uint32_t RTC_REFRESH_INTERVAL_MS = 6000;   // refresh RTC every ~6 seconds
uint32_t rtcFallbackUnixTs = 0;      // startup-derived fallback epoch if RTC read glitches

// Time window for WiFi phase (hours in local controller time).
uint8_t START_HOUR = 7;
uint8_t END_HOUR = 19;
// TCP chunk size used for file transfer to controller.
const size_t FILE_CHUNK_SIZE = 4096;
// Mandatory raw-capture period immediately after reboot.
// Set to 300s for normal time to reach WiFi mode quickly after reboot. 10 for testing
const uint32_t STARTUP_CAL_CAPTURE_SECONDS = 10UL; // 300UL;
// Duration from file start treated as calibration section for trim logic.
const uint32_t TRIM_CALIBRATION_SECONDS = 300UL;
// Optional guard from file start before event detection can begin.
const uint32_t TRIM_START_GUARD_SECONDS = TRIM_CALIBRATION_SECONDS;
// Seconds of context retained before event trigger time.
const uint32_t TRIM_PRE_EVENT_SECONDS = 180UL;
// Seconds retained after event trigger time.
const uint32_t TRIM_POST_EVENT_SECONDS = 600UL;
// Minimum threshold distance above baseline used in trim detection.
const long TRIM_TRIGGER_MIN_DELTA = 2000L;
// Fraction used to split calibration values into baseline vs elevated segments.
const float TRIM_CAL_SPLIT_FRACTION = 0.35f;
// Debounce hold time in seconds for event enter/exit state.
const float TRIM_DEBOUNCE_SECONDS = 0.5f;
// Progress logging frequency while analyzing/writing trim files.
const uint32_t TRIM_PROGRESS_ROWS = 50000UL;
// Maximum merged keep-intervals stored in RAM for trim pass.
const int MAX_TRIM_INTERVALS = 128;
// Require this many seconds continuously outside the WiFi window before leaving WiFi mode.
const uint32_t WIFI_EXIT_DEBOUNCE_SECONDS = 120UL;
// Raw file selection policy for trim phase.
// false: default to yesterday's DL file
// true : use today's DL file (test mode)
const bool TRIM_USE_TODAY_FILENAME = false;

/////////////////////
//  set constants
/////////////////////
// set the samples to average when getting data - thru 2025 was 80 which gave 56.9hz - use 70 -> 59 at home
const int myAVG = 80;

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
const char VERSION[] = "1.0";

// Interval of file timestamps to retain in trimmed output.
struct TrimInterval {
  uint32_t start_ts;
  uint32_t end_ts;
};

// Calibration-derived thresholds for trim event detection.
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

// WiFi and UDP globals
WiFiUDP udp;
bool sdReady = false;
IPAddress targetIp;
bool wifiInitialized = false;
bool wifiModeActive = false;
String networkHostname;

/***********************
 * Returns the MCU unique ID as a 32-hex-character string.
 * @return Device unique identifier (uppercase hex).
 ***********************/
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

String getFirmwareVersion() {
  return String(VERSION);
}

/***********************
 * Builds a stable 6-char short UID from full chip ID.
 * Uses FNV-1a hash + extra mixing + Base32 alphabet.
 * @param fullId Full chip unique ID (hex string).
 * @param outLen Number of chars to emit (default 6).
 * @return Deterministic short UID.
 ***********************/
String shortUidFromHash(const String &fullId, size_t outLen = 6) {
  uint32_t h = 2166136261UL;  // FNV offset basis
  for (size_t i = 0; i < fullId.length(); ++i) {
    h ^= (uint8_t)fullId[i];
    h *= 16777619UL;  // FNV prime
  }

  // Extra avalanche for better bit diffusion.
  h ^= (h >> 16);
  h *= 0x7feb352dUL;
  h ^= (h >> 15);
  h *= 0x846ca68bUL;
  h ^= (h >> 16);

  const char ALPHABET[] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  String out = "";
  out.reserve(outLen);
  uint32_t x = h;
  for (size_t i = 0; i < outLen; ++i) {
    uint8_t idx = x & 31U;
    out += ALPHABET[idx];
    x = (x >> 5) ^ (x << 27);
  }
  return out;
}

/***********************
 * Returns the ESP32-S3 station MAC address as uppercase hex.
 * @return MAC string without separators, e.g. AABBCCDDEEFF.
 ***********************/
String getWifiMacHex() {
  uint8_t mac[6] = {0};
  WiFi.macAddress(mac);
  char buf[13];
  snprintf(
    buf, sizeof(buf),
    "%02X%02X%02X%02X%02X%02X",
    mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]
  );
  return String(buf);
}

/***********************
 * Builds a stable network UID for external controller use.
 * Format mirrors common controller naming style.
 * @return UID like ESP32S3-ABCDE (fallback: ESP32S3-<deviceID_6>).
 ***********************/
String getNetworkUid() {
  String macHex = getWifiMacHex();
  if (macHex.length() >= 5) {
    return "ESP32S3-" + macHex.substring(macHex.length() - 5);
  }
  return "ESP32S3-" + deviceID_6;
}

/***********************
 * Builds hostname advertised to WiFi/DHCP controllers (e.g., UniFi).
 * Includes both network UID and MCU short ID for easier field matching.
 ***********************/
String buildNetworkHostname() {
  String host = getNetworkUid() + "-" + deviceID_6;
  if (host.length() > 31) {
    host = host.substring(0, 31);
  }
  return host;
}

/***********************
 * Returns a 4-hex suffix derived from ESP32 network UID.
 * @return Last 4 chars of network UID token.
 ***********************/
String getNetworkUidSuffix4() {
  String netUid = getNetworkUid();
  int dash = netUid.lastIndexOf('-');
  String token = (dash >= 0) ? netUid.substring(dash + 1) : netUid;
  if (token.length() >= 4) {
    return token.substring(token.length() - 4);
  }
  return token;
}

/***********************
 * Builds LCD UID line content.
 * Mode:
 * - R4   => UID: <deviceID_6>
 * - ESP  => UID: <esp4>
 * - BOTH => UID: <deviceID_6>-<esp4>
 * Default (unknown/empty): R4
 ***********************/
String getLcdUidLineBase(const String &modeIn) {
  String mode = modeIn;
  mode.toUpperCase();
  if (mode == "ESP") {
    return "UID: " + getNetworkUidSuffix4();
  }
  if (mode == "BOTH") {
    return "UID: " + deviceID_6 + "-" + getNetworkUidSuffix4();
  }
  return "UID: " + deviceID_6;
}

/***********************
 * Validates RTC date range to reject invalid/uninitialized reads.
 * @param dt RTC date-time to validate.
 * @return True when date components are in expected ranges.
 ***********************/
bool isRtcDateSane(const DateTime &dt) {
  int y = dt.year();
  int m = dt.month();
  int d = dt.day();
  return (y >= 2024 && y <= 2099 && m >= 1 && m <= 12 && d >= 1 && d <= 31);
}

/***********************
 * Reads RTC repeatedly until values are stable/sane.
 * @param out Populated with a stable DateTime on success.
 * @return True if a stable/sane time was obtained; false otherwise.
 ***********************/
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

/***********************
 * Attempts RTC.begin() with retries.
 * @param attempts Number of begin attempts.
 * @return True if RTC initialized; false if all attempts fail.
 ***********************/
bool beginRtcWithRetry(uint8_t attempts = 3) {
  for (uint8_t i = 0; i < attempts; ++i) {
    if (RTC.begin()) return true;
    delay(100);
  }
  return false;
}

/***********************
 * Refreshes cached timestamp base from RTC.
 * Call after successful RTC set operations so Get_TimeStamp() uses fresh time.
 * @return True when cache updated from a stable RTC read.
 ***********************/
bool Update_TimeStamp_Cache_From_RTC() {
  DateTime nowRtc((uint32_t)0);
  if (!readRtcStable(nowRtc)) {
    return false;
  }
  rtcBaseUnixTs = nowRtc.unixtime();
  rtcBaseMs = millis();
  // Keep fallback close to a recently-verified RTC value.
  rtcFallbackUnixTs = rtcBaseUnixTs;
  return true;
}

/***********************
 * Attempts to recover a stuck I2C bus by pulsing SCL.
 * Used after reset when peripherals may still be powered.
 ***********************/
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

/***********************
 * Incremental CRC32 update helper.
 * @param crc Current CRC state.
 * @param data Byte buffer.
 * @param len Number of bytes.
 * @return Updated CRC32 value.
 ***********************/
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

/***********************
 * Formats CRC32 value as 8-char uppercase hex.
 * @param value CRC32 value.
 * @return Hex string.
 ***********************/
String crc32Hex(uint32_t value) {
  char out[9];
  snprintf(out, sizeof(out), "%08lX", (unsigned long) value);
  return String(out);
}

/***********************
 * Writes status text to LCD line 1 (padded/truncated to 16 chars).
 * @param status Text to show.
 ***********************/
void setLcdStatusLine1(const String &status) {
  if (!printLCD) return;
  lcd.setCursor(0, 0);
  String text = status;
  while (text.length() < 16) text += " ";
  lcd.print(text.substring(0, 16));
}

/***********************
 * Writes UID line to LCD line 2, optionally appending CAL tag.
 * @param showCalTag True to show trailing "CAL", false to clear it.
 ***********************/
void setLcdUidLine(bool showCalTag) {
  if (!printLCD) return;
  lcd.setCursor(0, 1);
  String line2 = getLcdUidLineBase("BOTH");
  if (showCalTag) {
    if (line2.length() <= 13) {
      while (line2.length() < 13) line2 += " ";
      line2 += "CAL";
    }
  } else {
    while (line2.length() < 16) line2 += " ";
  }
  lcd.print(line2.substring(0, 16));
}

/***********************
 * Sends one UDP text message.
 * @param msg Message payload.
 * @param ip Destination IP.
 * @param port Destination UDP port.
 ***********************/
void sendUdpMessage(const String &msg, const IPAddress &ip, uint16_t port) {
  udp.beginPacket(ip, port);
  udp.print(msg);
  udp.endPacket();
}

/***********************
 * CSV splitter in-place using strtok_r.
 * @param input Mutable C-string to split.
 * @param fields Output pointers to tokens.
 * @param maxFields Capacity of fields[].
 * @return Number of parsed fields.
 ***********************/
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

/***********************
 * Reads one newline-terminated data line from SD file.
 * @param f Open SD File.
 * @param buf Output buffer.
 * @param n Buffer size.
 * @return True when a non-empty line was read.
 ***********************/
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

/***********************
 * Parses "value, unix_time" CSV row.
 * @param line Mutable CSV line.
 * @param valueOut Parsed sample value.
 * @param tsOut Parsed unix timestamp.
 * @return True when parsing succeeds.
 ***********************/
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

/***********************
 * Converts raw filename prefix DL->TR.
 * @param rawName Raw filename (typically DL*.TXT).
 * @return Trim filename.
 ***********************/
String trimFilenameFromRaw(const String &rawName) {
  if (rawName.length() >= 2 && rawName[0] == 'D' && rawName[1] == 'L') {
    String out = rawName;
    out.setCharAt(0, 'T');
    out.setCharAt(1, 'R');
    return out;
  }
  return "TR_" + rawName;
}

/***********************
 * Builds DL filename from epoch date.
 * @param epoch Unix epoch seconds.
 * @return Filename in DLYYMMDD.TXT format.
 ***********************/
String dlFilenameFromEpoch(uint32_t epoch) {
  DateTime dt(epoch);
  int yy = dt.year() % 100;
  int mm = dt.month();
  int dd = dt.day();
  char name[13];
  snprintf(name, sizeof(name), "DL%02d%02d%02d.TXT", yy, mm, dd);
  return String(name);
}

/***********************
 * Chooses trim target raw filename by date policy.
 * @return Selected DL filename or empty string if unavailable.
 ***********************/
String trimRawFilenameByDatePolicy() {
  DateTime nowRtc = RTC.now();
  if (!isRtcDateSane(nowRtc)) return "";

  uint32_t nowEpoch = nowRtc.unixtime();
  uint32_t dayOffset = TRIM_USE_TODAY_FILENAME ? 0UL : 86400UL;
  if (nowEpoch <= dayOffset) return "";

  return dlFilenameFromEpoch(nowEpoch - dayOffset);
}

/***********************
 * Scans SD root and returns latest DL*.TXT by lexical date token.
 * @return Latest matching filename or empty string.
 ***********************/
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

/***********************
 * Adds/merges a keep-interval for trim output.
 * @param startTs Interval start timestamp.
 * @param endTs Interval end timestamp.
 ***********************/
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

/***********************
 * Derives baseline/low thresholds from file calibration section.
 * @param inputName Raw SD filename to analyze.
 * @return CalibrationThresholds with ok=false when derivation fails.
 ***********************/
CalibrationThresholds deriveCalibrationThresholdsFromFile(const String &inputName) {
  CalibrationThresholds r = {false, 0UL, 0L, 0L, 0L, 0L};
  File in = SD.open(inputName.c_str(), FILE_READ);
  if (!in) return r;

  if (in.size() == 0) {
    in.close();
    Serial.println(F("TRIM fail: empty file"));
    return r;
  }

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

/***********************
 * Builds merged trim keep-intervals using event detection rules.
 * @param inputName Raw SD filename.
 * @param cal Thresholds derived from calibration section.
 * @return True on success.
 ***********************/
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

/***********************
 * Writes trimmed file by copying rows that fall inside keep-intervals.
 * @param inputName Source raw filename.
 * @param outputName Destination trim filename.
 * @return True on successful write.
 ***********************/
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

/***********************
 * Ensures a TR file exists and is ready before WiFi upload session.
 * If TR exists and non-empty, skip re-trim; otherwise build it.
 * @return True when TR file is ready for transfer.
 ***********************/
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

  // If TRIM_USE_TODAY_FILENAME is false and we're using today's file, skip trimming
  if (!TRIM_USE_TODAY_FILENAME && rawName == myFilename) {
    Serial.println(F("TRIM skip: TRIM_USE_TODAY_FILENAME is false, not trimming today's file."));
    return true;  // Proceed without trimming
  }

  Serial.print(F("TRIM raw target: "));
  Serial.println(rawName);

  // Check if raw file is empty; if so, skip trimming and proceed
  File rawFile = SD.open(rawName.c_str(), FILE_READ);
  if (rawFile) {
    if (rawFile.size() == 0) {
      rawFile.close();
      Serial.println(F("TRIM skip: empty raw file, proceeding without trim"));
      return true;
    }
    rawFile.close();
  }

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

/***********************
 * Connects STA WiFi with retries.
 * @return True when connected and local IP assigned.
 ***********************/
bool connectWiFi() {
  int status = WiFi.status();
  if (status == WL_NO_MODULE) {
    Serial.println(F("WiFi module not detected."));
    return false;
  }

  // Use for naming, the 6-character UID string.
  if (networkHostname.length() == 0) {
    networkHostname = buildNetworkHostname();
  }
  Serial.print("Hostname: ");
  Serial.println(networkHostname);
  // Set hostname before WiFi.begin() so controller lists device consistently.
  WiFi.setHostname(networkHostname.c_str());
  delay(1000); // Short delay to ensure hostname is set before connection attempts
  
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

/***********************
 * Resolves UDP target host/IP from secrets configuration.
 * @return True when targetIp is valid.
 ***********************/
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

/***********************
 * Prints WiFi diagnostics to Serial/LCD.
 ***********************/
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
  String uidLine = getLcdUidLineBase("BOTH");
  while (uidLine.length() < 16) uidLine += " ";
  lcd.print(uidLine.substring(0, 16));
  delay(1200);
}

/***********************
 * One-time startup WiFi self-check (connect, print info, disconnect).
 ***********************/
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

/***********************
 * Enters active WiFi command mode (connect + UDP bind).
 ***********************/
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

/***********************
 * Leaves WiFi command mode and returns to local data state.
 ***********************/
void exitWifiMode() {
  Serial.println(F("Exiting WiFi mode"));
  udp.stop();
  WiFi.disconnect();
  wifiInitialized = false;
  setLcdStatusLine1("Data:");
}

/***********************
 * Sends remote file list over UDP control channel.
 * @param transferId Transfer correlation ID.
 * @param replyIp Destination IP for replies.
 * @param replyPort Destination UDP port for replies.
 ***********************/
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

/***********************
 * Returns true when filename is safe to use on SD root path.
 ***********************/
bool isSafeSdFilename(const String &filename) {
  if (filename.length() == 0 || filename.length() > 40) return false;
  if (filename.indexOf('/') >= 0 || filename.indexOf('\\') >= 0) return false;
  if (filename.indexOf("..") >= 0) return false;
  return true;
}

/***********************
 * Deletes a specific file from SD card on controller request.
 * Command format: DELETE_FILE,<transferId>,<filename>
 ***********************/
void deleteFileFromSd(const String &transferId, const String &filenameIn, const IPAddress &replyIp, uint16_t replyPort) {
  if (!sdReady) {
    sendUdpMessage("ERROR," + transferId + ",SD_NOT_READY,SD init failed", replyIp, replyPort);
    return;
  }

  String filename = filenameIn;
  filename.trim();
  if (filename.startsWith("/")) filename = filename.substring(1);

  if (!isSafeSdFilename(filename)) {
    sendUdpMessage("ERROR," + transferId + ",BAD_FILENAME," + filenameIn, replyIp, replyPort);
    return;
  }

  // Safety guard: do not delete the active acquisition file.
  if (filename.equalsIgnoreCase(myFilename)) {
    sendUdpMessage("ERROR," + transferId + ",ACTIVE_FILE," + filename, replyIp, replyPort);
    return;
  }

  String path = "/" + filename;
  bool exists = SD.exists(path.c_str()) || SD.exists(filename.c_str());
  if (!exists) {
    sendUdpMessage("ERROR," + transferId + ",FILE_NOT_FOUND," + filename, replyIp, replyPort);
    return;
  }

  bool removed = SD.remove(path.c_str());
  if (!removed) removed = SD.remove(filename.c_str());
  if (!removed) {
    sendUdpMessage("ERROR," + transferId + ",DELETE_FAILED," + filename, replyIp, replyPort);
    return;
  }

  Serial.print(F("SD delete OK: "));
  Serial.println(filename);
  sendUdpMessage("ACK_DELETE," + transferId + "," + filename, replyIp, replyPort);
}

/***********************
 * Streams one remote file over TCP using CHUNK headers.
 * @param transferId Transfer correlation ID.
 * @param filename Remote filename requested by controller.
 * @param controllerIp Controller IP for TCP connect.
 * @param controllerTcpPort Controller TCP listening port.
 * @param startOffset Byte offset for resume/start.
 * @param replyIp UDP reply IP.
 * @param replyPort UDP reply port.
 ***********************/
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

/***********************
 * Handles inbound UDP control commands while in WiFi mode.
 * Commands include polling, list files, start file transfer, and RTC sync.
 ***********************/
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
    response += ",";
    response += getNetworkUid();
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  if (strcmp(incoming, GET_NET_UID_MESSAGE) == 0) {
    String netUid = getNetworkUid();
    String response = "NET_UID,";
    response += netUid;
    response += ",HOST=";
    response += networkHostname;
    response += ",MAC=";
    response += getWifiMacHex();
    Serial.print(F("GET_NET_UID -> "));
    Serial.print(netUid);
    Serial.print(F(" (HOST="));
    Serial.print(networkHostname);
    Serial.println(F(")"));
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  if (strcmp(incoming, GET_VERSION_MESSAGE) == 0) {
    String response = "VERSION,";
    response += getFirmwareVersion();
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  if (strcmp(incoming, GET_TIME_MESSAGE) == 0) {
    DateTime nowRtc((uint32_t)0);
    bool ok = false;
    // Retry stable RTC reads first.
    for (uint8_t i = 0; i < 3 && !ok; ++i) {
      ok = readRtcStable(nowRtc);
      if (!ok) delay(40);
    }
    // If still failing, do a lightweight I2C/RTC recovery and try once more.
    if (!ok) {
      recoverI2CBus();
      Wire.end();
      delay(10);
      Wire.begin();
      Wire.setClock(50000);
      delay(50);
      if (beginRtcWithRetry(2)) {
        ok = readRtcStable(nowRtc);
      }
    }
    if (ok) {
      String response = "TIME,";
      response += String((unsigned long) nowRtc.unixtime());
      response += ",";
      response += nowRtc.timestamp(DateTime::TIMESTAMP_FULL);
      sendUdpMessage(response, remoteIp, remotePort);
    } else {
      Serial.println(F("GET_TIME failed: RTC read/recovery unsuccessful."));
      sendUdpMessage("ERR_TIME,READ_FAILED", remoteIp, remotePort);
    }
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

  if (fieldCount >= 3 && strcmp(fields[0], DELETE_FILE_MESSAGE) == 0) {
    String transferId = String(fields[1]);
    String filename = String(fields[2]);
    deleteFileFromSd(transferId, filename, remoteIp, remotePort);
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
          Update_TimeStamp_Cache_From_RTC();
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

  // PING: Simple connectivity check
  if (strcmp(incoming, PING_MESSAGE) == 0) {
    sendUdpMessage("PONG," + deviceId, remoteIp, remotePort);
    return;
  }

  // GET_STATUS: Report device status (uptime, mode, SD space, last data timestamp)
  if (strcmp(incoming, GET_STATUS_MESSAGE) == 0) {
    uint32_t uptime = millis() / 1000;
    String mode = wifiModeActive ? "WIFI" : "DATA";
    uint32_t sdFree = 0; // TODO: SD.totalBytes() may not be available in all libraries
    uint32_t lastTs = 0; // TODO: Get_TimeStamp() may hang if RTC bad
    String response = "STATUS,UPTIME=" + String(uptime) + ",MODE=" + mode + ",SD_FREE_KB=" + String(sdFree) + ",LAST_DATA_TS=" + String(lastTs) + ",BATTERY=N/A";
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  // GET_CONFIG: Report current configuration (hours, device ID)
  if (strcmp(incoming, GET_CONFIG_MESSAGE) == 0) {
    String response = "CONFIG,START_HOUR=" + String(START_HOUR) + ",END_HOUR=" + String(END_HOUR) + ",DEVICE_ID=" + deviceId;
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  // GET_DIAGNOSTICS: Report error counts and health status
  if (strcmp(incoming, GET_DIAGNOSTICS_MESSAGE) == 0) {
    DateTime now((uint32_t)0);
    bool rtcOk = readRtcStable(now);
    String response = "DIAG,RTC_OK=" + String(rtcOk ? 1 : 0) + ",RTC_ERRORS=" + String(rtcErrorCount) + ",I2C_ERRORS=" + String(i2cErrorCount) + ",SD_ERRORS=" + String(sdErrorCount);
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  // GET_LAST_DATA: Report current sensor reading and timestamp
  if (strcmp(incoming, GET_LAST_DATA_MESSAGE) == 0) {
    long data = 0; // TODO: Get_Data() may hang if ADC not ready
    uint32_t ts = 0; // TODO: Get_TimeStamp() may hang if RTC bad
    String response = "LAST_DATA,VALUE=" + String(data) + ",TS=" + String(ts);
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  // SET_CONFIG: Set configuration parameters (e.g., SET_CONFIG,START_HOUR=8,END_HOUR=16)
  if (fieldCount >= 2 && strcmp(fields[0], SET_CONFIG_MESSAGE) == 0) {
    bool updated = false;
    for (int i = 1; i < fieldCount; ++i) {
      char *param = fields[i];
      char *eq = strchr(param, '=');
      if (eq) {
        *eq = '\0';
        char *key = param;
        char *value = eq + 1;
        if (strcmp(key, "START_HOUR") == 0) {
          uint8_t val = (uint8_t)atoi(value);
          if (val < 24) {
            START_HOUR = val;
            updated = true;
          }
        } else if (strcmp(key, "END_HOUR") == 0) {
          uint8_t val = (uint8_t)atoi(value);
          if (val < 24) {
            END_HOUR = val;
            updated = true;
          }
        }
      }
    }
    if (updated) {
      sendUdpMessage("ACK_CONFIG", remoteIp, remotePort);
      Serial.println(F("Config updated via SET_CONFIG"));
    } else {
      sendUdpMessage("ERR_CONFIG,INVALID_PARAMS", remoteIp, remotePort);
    }
    return;
  }

  // REBOOT: Reboot the device
  if (strcmp(incoming, REBOOT_MESSAGE) == 0) {
    sendUdpMessage("ACK_REBOOT", remoteIp, remotePort);
    Serial.println(F("Rebooting via REBOOT command"));
    delay(1000);
    NVIC_SystemReset();
    return;
  }

  // ENTER_DATA_MODE: Exit WiFi mode and return to data collection
  if (strcmp(incoming, ENTER_DATA_MODE_MESSAGE) == 0) {
    exitWifiMode();
    sendUdpMessage("ACK_ENTER_DATA_MODE", remoteIp, remotePort);
    return;
  }

  // GET_LOGS: Retrieve recent logs (not implemented, respond with not supported)
  if (strcmp(incoming, GET_LOGS_MESSAGE) == 0) {
    sendUdpMessage("LOGS,NOT_SUPPORTED", remoteIp, remotePort);
    return;
  }

  // CLEAR_ERRORS: Reset error counters
  if (strcmp(incoming, CLEAR_ERRORS_MESSAGE) == 0) {
    i2cErrorCount = 0;
    rtcErrorCount = 0;
    sdErrorCount = 0;
    sendUdpMessage("ACK_CLEAR_ERRORS", remoteIp, remotePort);
    Serial.println(F("Error counters cleared"));
    return;
  }
}

/***********************
 * Performs one acquisition batch and appends it to current data file.
 * @param unixTs Timestamp value recorded for this acquisition batch.
 ***********************/
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
/***********************
 * Reads averaged ADC sample from load cell front-end.
 * @return Averaged ADC reading.
 ***********************/
long int Get_Data() {
  // return (scale.read()); // different mode of the amplifier? From chris Lange
  return scale.continuousReadAverage(myAVG);
}

////////////////////
//  Get_TimeStamp - encapsulate data in case we change libraries
////
/***********************
 * Reads current RTC unix timestamp.
 * @return Unix time in seconds.
 ***********************/
long int OLD_Get_TimeStamp() {
  /* GET CURRENT TIME FROM RTC */
  currenttime = RTC.now();
  // convert to raw unix value for return - could give options
  return (currenttime.unixtime());
}

/***********************
 * Get_TimeStamp - cached RTC read
 * Reads RTC only every ~6 seconds, then uses millis() to advance time
 * between reads. Returns Unix time in seconds.
 * should prevent I2C problems that cause RTC failure
 ***********************/
uint32_t Get_TimeStamp() {
  uint32_t nowMs = millis();

  // Refresh from RTC on first call, or every RTC_REFRESH_INTERVAL_MS
  if (rtcBaseUnixTs == 0 || (nowMs - rtcBaseMs >= RTC_REFRESH_INTERVAL_MS)) {

    // Retry a few times in case I2C is temporarily flaky
    bool gotRtc = false;
    for (int i = 0; i < 3; i++) {
      currenttime = RTC.now();

      // Basic sanity check: reject obviously bad reads
      if (currenttime.year() >= 2024) {
        rtcBaseUnixTs = currenttime.unixtime();
        rtcBaseMs = nowMs;
        gotRtc = true;
        break;
      }

      delay(5);
    }

    // If first read attempts fail and cache is still empty, return a safe
    // startup-derived fallback instead of near-1970 time.
    if (!gotRtc && rtcBaseUnixTs == 0) {
      if (rtcFallbackUnixTs == 0) {
        // Last-resort fallback: compile-time epoch.
        rtcFallbackUnixTs = DateTime(F(__DATE__), F(__TIME__)).unixtime();
      }
      return rtcFallbackUnixTs + (nowMs / 1000UL);
    }
  }

  // Advance cached Unix time using elapsed millis since last RTC read
  return rtcBaseUnixTs + ((nowMs - rtcBaseMs) / 1000);
}

////////////////////
//  Get_TimeStampString - encapsulate data in case we change the way we record time
////
/***********************
 * Reads current RTC time string.
 * @return RTC time string in HH:MM:SS style.
 ***********************/
String Get_TimeStampString() {
  /* GET CURRENT TIME FROM RTC */
  currenttime = RTC.now();
  return(currenttime.timestamp(DateTime::TIMESTAMP_TIME));
}

////////////////////
//  Get_TimeStampString - encapsulate data in case we change the way we record time
////
/***********************
 * Checks whether current unix timestamp is inside configured WiFi window.
 * @param unixTs Unix timestamp to test.
 * @param startHour Window start hour [0..23].
 * @param endHour Window end hour [0..23], exclusive when start<end.
 * @return True when timestamp falls within window semantics.
 ***********************/
bool IsBetweenHours(uint32_t unixTs, uint8_t startHour = START_HOUR, uint8_t endHour = END_HOUR) {
  uint32_t secOfDay = unixTs % 86400UL;
  uint32_t start = (uint32_t)startHour * 3600UL;
  uint32_t end   = (uint32_t)endHour   * 3600UL;
  return (secOfDay >= start && secOfDay < end);
}


///////////////////
// File name function - based on RTC time - so that we have a new filename for every day "DL_MM_DD.txt"
/////////
/***********************
 * Builds daily data filename from RTC date.
 * @return Filename in DLYYMMDD.TXT format.
 ***********************/
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

/***********************
 * Arduino initialization entrypoint.
 * Sets up peripherals, RTC, SD, diagnostics, and initial display state.
 ***********************/
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
  deviceID_6 = shortUidFromHash(deviceId, 6);
  networkHostname = buildNetworkHostname();
 

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
  Wire.setClock(50000);
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

  rtcFallbackUnixTs = rtcNow.unixtime();
  // Prime cached timestamp base after startup RTC validation.
  Update_TimeStamp_Cache_From_RTC();

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
  } else {
    Serial.println("card initialized.");  // confirm that it is good to go
  }



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
    setLcdUidLine(false);
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
    setLcdUidLine(false);
  }
}


/***********************
 * Main runtime state machine.
 * - startup calibration capture window
 * - no-acquisition WiFi window behavior
 * - optional trim + WiFi command mode
 * - normal acquisition outside WiFi window
 ***********************/
void loop() {
  tCounter = tCounter + 1;
  uint32_t unixTs = Get_TimeStamp();
  bool inWifiWindow = IsBetweenHours(unixTs);

  // Always capture 10 minutes of raw data after each reboot before any WiFi workflow.
  if (!startupCalWindowInitialized) {
    startupCalWindowInitialized = true;
    startupCalWindowComplete = false;
    startupCalWindowEndTs = unixTs + STARTUP_CAL_CAPTURE_SECONDS;
    bootedInWifiWindow = inWifiWindow;
    wifiSessionArmed = bootedInWifiWindow;  // WiFi session only allowed after reboot that occurred in WiFi window.
    Serial.print(F("Startup capture begin. bootedInWifiWindow="));
    Serial.println(bootedInWifiWindow ? F("YES") : F("NO"));
    if (printLCD) {
      setLcdStatusLine1("Data:");
      setLcdUidLine(true);
    }
  }

  if (!startupCalWindowComplete) {
    runAcquisitionCycle(unixTs);
    if (unixTs >= startupCalWindowEndTs) {
      startupCalWindowComplete = true;
      Serial.println(F("Startup capture complete."));
      if (printLCD) {
        setLcdStatusLine1("Data:");
        setLcdUidLine(false);
      }
    }
    return;
  }

  // If already in live WiFi mode, keep servicing commands.
  if (wifiModeActive) {
    if (!inWifiWindow) {
      if (wifiOutWindowSinceTs == 0) {
        wifiOutWindowSinceTs = unixTs;
        Serial.println(F("WiFi out-of-window detected; debounce started."));
      }
      bool exitNow = (unixTs >= wifiOutWindowSinceTs + WIFI_EXIT_DEBOUNCE_SECONDS);
      if (exitNow) {
        exitWifiMode();
        wifiModeActive = false;
        wifiIdleAnnounced = false;
        wifiOutWindowSinceTs = 0;
      } else {
        // Keep processing commands during debounce window to tolerate transient RTC/window glitches.
        serviceWifiCommands();
      }
    } else {
      wifiOutWindowSinceTs = 0;
      serviceWifiCommands();
    }
    return;
  }

  // In WiFi window: acquisition must remain stopped.
  if (inWifiWindow) {
    if (!wifiSessionArmed) {
      if (!wifiIdleAnnounced) {
        Serial.println(F("WiFi window active. Waiting for reboot-armed session."));
        wifiIdleAnnounced = true;
      }
      if (printLCD) setLcdStatusLine1("WiFi: reboot req");
      delay(250);
      return;
    }

    // Reboot-armed path: trim first, then open WiFi listener.
    if (!ensureTrimmedFileReadyForWifi()) {
      wifiSessionArmed = false;  // consume even on failure to prevent endless retry
      delay(1000);
      return;
    }
    enterWifiMode();
    wifiModeActive = wifiInitialized;
    if (wifiModeActive) {
      wifiSessionArmed = false;  // consume one armed session per reboot
      wifiIdleAnnounced = false;
      wifiOutWindowSinceTs = 0;
    }
    return;
  }

  // Outside WiFi window: normal acquisition.
  wifiIdleAnnounced = false;
  runAcquisitionCycle(unixTs);
}


// -- END OF FILE --
