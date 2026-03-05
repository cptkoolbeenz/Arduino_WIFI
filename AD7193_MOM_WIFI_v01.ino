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
#include <WiFiS3.h>
#include <WiFiUdp.h>
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

const uint8_t START_HOUR = 10;
const uint8_t END_HOUR = 12;
const size_t FILE_CHUNK_SIZE = 1024;

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
  unsigned long offset = startOffset;
  unsigned long chunkIndex = 0;
  uint32_t fullCrc = 0;

  while (offset < fileSize) {
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
    if (client.write(buffer, (size_t) bytesRead) != (size_t) bytesRead) {
      client.stop();
      file.close();
      sendUdpMessage("ERROR," + transferId + ",TCP_WRITE_FAILED," + String(offset), replyIp, replyPort);
      return;
    }

    offset += (unsigned long) bytesRead;
    chunkIndex++;
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

  DateTime currenttime;

  currenttime = RTC.now();

  int MM = currenttime.month();
  int DD = currenttime.day();

  String formattedDateTime = "DL_";
  formattedDateTime += (MM < 10 ? "0" : "") + String(MM) + "_";
  formattedDateTime += (DD < 10 ? "0" : "") + String(DD);
  formattedDateTime += ".txt";

  return formattedDateTime;
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
  if (!RTC.begin()) {  // initialize RTC
    Serial.println("RTC failed");
  } else {
    if (!RTC.isrunning()) {
      Serial.println("RTC is NOT running!");
      // following line sets the RTC to the date & time this sketch was compiled
      // RTC.adjust(DateTime(F(__DATE__), F(__TIME__)));
      Serial.print("NOT RUNNING AND NO Time set ");
      // Serial.print("new date: ");
      // Serial.println(F(__DATE__));
      lcd.print("                ");
      lcd.setCursor(0, 0);
      lcd.print("RTC failed!");
      lcd.setCursor(1, 1);
      lcd.print("Disconnect!     ");
      delay(10000);
    } else {
      // RTC.adjust(DateTime(F(__DATE__), F(__TIME__)));
            lcd.print("                ");
      lcd.setCursor(0, 0);
      lcd.print("RTC Running!");
      lcd.setCursor(1, 1);
      lcd.print(String(RTC.now().timestamp(DateTime::TIMESTAMP_FULL)));
      Serial.println("RTC is already initialized. Time: ");
      Serial.println(String("TIMESTAMP:\t")+RTC.now().timestamp(DateTime::TIMESTAMP_FULL));
      delay(3000);
    }
  }


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
}


void loop() {
  tCounter = tCounter + 1;
  uint32_t unixTs = Get_TimeStamp();
  bool inWifiWindow = IsBetweenHours(unixTs);

  if (inWifiWindow && !wifiModeActive) {
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
