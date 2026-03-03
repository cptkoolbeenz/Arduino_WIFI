#include <WiFiS3.h>
#include <WiFiUdp.h>
#include "secrets.h"

// UNO R4 WiFi base sketch: connect to WiFi and send a simple UDP payload.
// Replace `buildPayload()` contents with your sensor + timestamp fields.

WiFiUDP udp;
IPAddress targetIp;

const char DEVICE_ID[] = "MOM01";
const unsigned long SEND_INTERVAL_MS = 1000UL;

unsigned long lastSendMs = 0;
unsigned long packetCount = 0;

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

  Serial.println(F("WiFi connected."));
  printWifiStatus();
}

String buildPayload() {
  // Placeholder values; swap in your real values later.
  unsigned long unixTime = millis() / 1000UL;
  unsigned long sample = analogRead(A0);

  String payload;
  payload.reserve(48);
  payload += DEVICE_ID;
  payload += ',';
  payload += String(unixTime);
  payload += ',';
  payload += String(sample);
  return payload;
}

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 5000UL) {
    ;
  }

  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println(F("WiFi module not detected."));
    while (true) {
      delay(1000);
    }
  }

  if (!targetIp.fromString(UDP_TARGET_IP)) {
    Serial.println(F("Invalid UDP_TARGET_IP in secrets.h"));
    while (true) {
      delay(1000);
    }
  }

  connectWiFi();
  udp.begin(UDP_LOCAL_PORT);

  Serial.print(F("UDP local port: "));
  Serial.println(UDP_LOCAL_PORT);
  Serial.print(F("UDP target: "));
  Serial.print(UDP_TARGET_IP);
  Serial.print(':');
  Serial.println(UDP_TARGET_PORT);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(F("WiFi dropped; reconnecting..."));
    connectWiFi();
  }

  unsigned long now = millis();
  if ((now - lastSendMs) >= SEND_INTERVAL_MS) {
    lastSendMs = now;

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
}
