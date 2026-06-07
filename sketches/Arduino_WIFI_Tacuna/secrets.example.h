#ifndef SECRETS_H
#define SECRETS_H

// Copy this file to `secrets.h` and edit values for your local deployment.
// Keep `secrets.h` device-specific when needed.

// WiFi credentials for the local AP used by the Arduino.
#define SECRET_SSID "HappyHappy"
#define SECRET_PASS "enjoytoday"

// Controller endpoint settings.
// Use local/private network values only (not public internet IPs).
#define UDP_TARGET_IP "192.168.10.1"
#define UDP_TARGET_PORT 5005
#define UDP_LOCAL_PORT 8888

// Optional hostname-based target (used first if defined/resolvable).
// Uncomment only if you want DNS/hostname routing instead of fixed IP.
// #define UDP_TARGET_HOST "bsm-controller.local"

#endif
