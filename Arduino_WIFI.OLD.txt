#include <WiFiS3.h>
#include <WiFiUdp.h>
#include <SD.h>
#include "secrets.h"

#include <Arduino.h>

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
  return String(id);  // 32 hex chars
}

// NEED TO DO - MANAGE DAYLIGHT ACTIVIITES - TURN ON AND OFF SO NIGHTTIME IS NOT AFFECTED
//            - TURN ON WIFI CONNECTION AT 10AM AND OFF AT 7PM
//            - DON'T WANT TO BE LISTENING DURING OTHER TIMES
//            - MANAGE SD CARD FILES - IF FILES EXIST, WAIT FOR DOWNLOAD BEFORE SENDING NEW DATA
//            - IF FILES EXIST AND NO DOWNLOAD AFTER A LONG TIME, DELETE FILES TO FREE UP SPACE
//            - WATCH RAM USAGE WITH LOAD CELL DATA COLLECTION


// UNO R4 WiFi base sketch: connect to WiFi and send a simple UDP payload.
// Replace `buildPayload()` contents with your sensor + timestamp fields.

WiFiUDP udp;
IPAddress targetIp;

String deviceId;
const char POLL_MESSAGE[] = "POLL_UID";
const char DOWNLOAD_MESSAGE[] = "DOWNLOAD_DATA";
const char LIST_FILES_MESSAGE[] = "LIST_FILES";
const char START_FILE_MESSAGE[] = "START_FILE";
const char RESUME_MESSAGE[] = "RESUME";

#ifndef SD_CS_PIN
#define SD_CS_PIN 10
#endif

const size_t FILE_CHUNK_SIZE = 1024;

const unsigned long SEND_INTERVAL_MS = 1000UL;

unsigned long lastSendMs = 0;
unsigned long packetCount = 0;
int burstRemaining = 0;
bool sdReady = false;

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
    if (!entry) {
      break;
    }
    if (!entry.isDirectory()) {
      fileCount++;
    }
    entry.close();
  }
  root.close();

  sendUdpMessage("FILE_LIST_BEGIN," + transferId + "," + String(fileCount), replyIp, replyPort);

  root = SD.open("/");
  while (true) {
    File entry = root.openNextFile();
    if (!entry) {
      break;
    }
    if (!entry.isDirectory()) {
      String msg = "FILE_ITEM,";
      msg += transferId;
      msg += ",";
      msg += entry.name();
      msg += ",";
      msg += String((unsigned long) entry.size());
      msg += ",0";
      sendUdpMessage(msg, replyIp, replyPort);
      delay(2);
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
  if (!sdReady) {
    sendUdpMessage("ERROR," + transferId + ",SD_NOT_READY,SD init failed", replyIp, replyPort);
    return;
  }

  String path = filename;
  if (!path.startsWith("/")) {
    path = "/" + path;
  }
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

  uint8_t buffer[FILE_CHUNK_SIZE];
  unsigned long offset = startOffset;
  unsigned long chunkIndex = 0;
  uint32_t fullCrc = 0;

  while (offset < fileSize) {
    size_t toRead = FILE_CHUNK_SIZE;
    unsigned long remaining = fileSize - offset;
    if (remaining < toRead) {
      toRead = remaining;
    }

    int bytesRead = file.read(buffer, (int) toRead);
    if (bytesRead <= 0) {
      client.stop();
      file.close();
      sendUdpMessage("ERROR," + transferId + ",FILE_READ_FAILED," + String(offset), replyIp, replyPort);
      return;
    }

    uint32_t chunkCrc = crc32Update(0, buffer, (size_t) bytesRead);
    fullCrc = crc32Update(fullCrc, buffer, (size_t) bytesRead);

    String header = "CHUNK,";
    header += transferId;
    header += ",";
    header += String(chunkIndex);
    header += ",";
    header += String(offset);
    header += ",";
    header += String(bytesRead);
    header += ",";
    header += crc32Hex(chunkCrc);
    header += "\n";

    client.print(header);
    size_t written = client.write(buffer, (size_t) bytesRead);
    if (written != (size_t) bytesRead) {
      client.stop();
      file.close();
      sendUdpMessage("ERROR," + transferId + ",TCP_WRITE_FAILED," + String(offset), replyIp, replyPort);
      return;
    }

    offset += (unsigned long) bytesRead;
    chunkIndex++;
    delay(1);
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
}

void printWifiStatus() {
  Serial.print(F("SSID: "));
  Serial.println(WiFi.SSID());

  IPAddress ip = WiFi.localIP();
  Serial.print(F("IP: "));
  Serial.println(ip);

  long rssi = WiFi.RSSI();
  Serial.print(F("RSSI: "));
  Serial.print(rssi);
  Serial.println(F(" dBm"));
}

void connectWiFi() {
  int status = WL_IDLE_STATUS;

  while (status != WL_CONNECTED) {
    Serial.print(F("Connecting to SSID: "));
    Serial.println(SECRET_SSID);
    status = WiFi.begin(SECRET_SSID, SECRET_PASS);

    unsigned long start = millis();
    while ((millis() - start) < 10000UL && WiFi.status() != WL_CONNECTED) {
      delay(200);
    }

    if (WiFi.status() != WL_CONNECTED) {
      Serial.println(F("WiFi connect failed, retrying..."));
      delay(1500);
    }
  }

  // Wait for DHCP to provide a usable local IP.
  unsigned long ipWaitStart = millis();
  while (WiFi.localIP() == IPAddress(0, 0, 0, 0) && (millis() - ipWaitStart) < 10000UL) {
    delay(100);
  }

  Serial.println(F("WiFi connected."));
  printWifiStatus();
}

bool resolveTargetIp() {
#ifdef UDP_TARGET_HOST
  Serial.print(F("Resolving UDP target host: "));
  Serial.println(UDP_TARGET_HOST);
  if (WiFi.hostByName(UDP_TARGET_HOST, targetIp) == 1) {
    Serial.print(F("Resolved host to: "));
    Serial.println(targetIp);
    return true;
  }
  Serial.println(F("Host resolve failed; falling back to UDP_TARGET_IP."));
#endif

  if (targetIp.fromString(UDP_TARGET_IP)) {
    Serial.print(F("Using UDP target IP: "));
    Serial.println(targetIp);
    return true;
  }

  return false;
}

String buildPayload() {
  // Placeholder values; swap in your real values later.
  unsigned long unixTime = millis() / 1000UL;
  unsigned long sample = analogRead(A0);

  String payload;
  payload.reserve(48);
  payload += deviceId; // DEVICE_ID;
  payload += ',';
  payload += String(unixTime);
  payload += ',';
  payload += String(sample);
  return payload;
}

void sendDataPacket() {
  String payload = buildPayload();
  udp.beginPacket(targetIp, UDP_TARGET_PORT);
  udp.print(payload);
  udp.endPacket();

  packetCount++;
  Serial.print(F("Sent #"));
  Serial.print(packetCount);
  Serial.print(F(": "));
  Serial.println(payload);
}

void handleCommands() {
  int packetSize = udp.parsePacket();
  if (packetSize <= 0) {
    return;
  }

  char incoming[192];
  int n = udp.read(incoming, sizeof(incoming) - 1);
  if (n < 0) {
    return;
  }
  incoming[n] = '\0';
  while (n > 0 && (incoming[n - 1] == '\n' || incoming[n - 1] == '\r' || incoming[n - 1] == ' ')) {
    incoming[n - 1] = '\0';
    n--;
  }

  IPAddress remoteIp = udp.remoteIP();
  uint16_t remotePort = udp.remotePort();

  if (strcmp(incoming, POLL_MESSAGE) == 0) {
    // Always unicast poll replies to the configured Mac listener target.
    IPAddress replyIp = targetIp;
    unsigned int replyPort = UDP_TARGET_PORT;

    String response = "ID,";
    response += deviceId;
    response += ",";
    response += WiFi.localIP().toString();
    response += ",";
    response += UDP_TARGET_IP;
    response += ",";
    response += String(UDP_TARGET_PORT);

    udp.beginPacket(replyIp, replyPort);
    udp.print(response);
    udp.endPacket();

    Serial.print(F("Poll reply -> "));
    Serial.print(replyIp);
    Serial.print(':');
    Serial.print(replyPort);
    Serial.print(F(" "));
    Serial.println(response);
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

    Serial.print(F("START_FILE "));
    Serial.print(filename);
    Serial.print(F(" -> "));
    Serial.print(remoteIp);
    Serial.print(':');
    Serial.println(tcpPort);

    sendFileOverTcp(transferId, filename, remoteIp, tcpPort, offset, remoteIp, remotePort);
    return;
  }

  if (fieldCount >= 3 && strcmp(fields[0], RESUME_MESSAGE) == 0) {
    // Resume is controller-driven; controller should send START_FILE with requested offset.
    String transferId = String(fields[1]);
    sendUdpMessage("ACK_RESUME_HINT," + transferId + ",USE_START_FILE_WITH_OFFSET", remoteIp, remotePort);
    return;
  }

  if (strcmp(incoming, DOWNLOAD_MESSAGE) == 0) {
    burstRemaining = 10;
    lastSendMs = 0;

    Serial.print(F("Download command from "));
    Serial.print(udp.remoteIP());
    Serial.print(':');
    Serial.print(udp.remotePort());
    Serial.println(F(" -> sending 10 CSV packets"));

    udp.beginPacket(udp.remoteIP(), udp.remotePort());
    udp.print("ACK,DOWNLOAD_DATA,10");
    udp.endPacket();
    return;
  }

  Serial.print(F("Unknown command from "));
  Serial.print(udp.remoteIP());
  Serial.print(':');
  Serial.print(udp.remotePort());
  Serial.print(F(" -> "));
  Serial.println(incoming);
}

void setup() {
  Serial.begin(115200);

  // Cache once; this is the MCU factory unique ID.
  deviceId = getChipIdHex();
  Serial.print("Device ID: ");
  Serial.println(deviceId);


  while (!Serial && millis() < 5000UL) {
    ;
  }

  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println(F("WiFi module not detected."));
    while (true) {
      delay(1000);
    }
  }

  connectWiFi();

  if (!resolveTargetIp()) {
    Serial.println(F("Invalid UDP target in secrets.h"));
    while (true) {
      delay(1000);
    }
  }

  udp.begin(UDP_LOCAL_PORT);

  sdReady = SD.begin(SD_CS_PIN);
  Serial.print(F("SD card: "));
  Serial.println(sdReady ? F("ready") : F("init failed"));

  Serial.print(F("UDP local port: "));
  Serial.println(UDP_LOCAL_PORT);
  Serial.print(F("UDP target: "));
  Serial.print(targetIp);
  Serial.print(':');
  Serial.println(UDP_TARGET_PORT);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(F("WiFi dropped; reconnecting..."));
    connectWiFi();
    resolveTargetIp();
  }

  handleCommands();

  if (burstRemaining > 0) {
    unsigned long now = millis();
    if ((now - lastSendMs) >= SEND_INTERVAL_MS) {
      lastSendMs = now;
      sendDataPacket();
      burstRemaining--;
      if (burstRemaining == 0) {
        Serial.println(F("Burst complete; returning to listening mode."));
      }
    }
  }
}
