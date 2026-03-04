#include <WiFiS3.h>
#include <WiFiUdp.h>
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


// UNO R4 WiFi base sketch: connect to WiFi and send a simple UDP payload.
// Replace `buildPayload()` contents with your sensor + timestamp fields.

WiFiUDP udp;
IPAddress targetIp;

String deviceId;
const char POLL_MESSAGE[] = "POLL_UID";
const char DOWNLOAD_MESSAGE[] = "DOWNLOAD_DATA";

const unsigned long SEND_INTERVAL_MS = 1000UL;

unsigned long lastSendMs = 0;
unsigned long packetCount = 0;
int burstRemaining = 0;

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

  char incoming[64];
  int n = udp.read(incoming, sizeof(incoming) - 1);
  if (n < 0) {
    return;
  }
  incoming[n] = '\0';

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
