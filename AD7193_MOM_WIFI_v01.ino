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
#include "Time.h"
#include "RTClib.h"

// Define variables
// --------------------
File myFile;
String myFilename;

// PCB variable defined by Tacuna code
#define SRAM_CS 1 //Use A0 for Uno R3.  Use 1 for Uno R4
#define SD_CS 10. // 
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
bool IsBetweenHours(uint32_t unixTs, uint8_t startHour = 7, uint8_t endHour = 20) {
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
  if (!SD.begin(chipSelect)) {
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
    lcd.print("                ");
    lcd.setCursor(6, 0);
    lcd.print(Get_Data());  // do this while we are messing with closing the datafile
  }
}


void loop() {
  tCounter = tCounter + 1;
  unsigned long strain;

    // get a single time stamp to put on all 300 data points - not sure worth the speed to lose the resolution
  uint32_t  myUnixTime = Get_TimeStamp(); // NOT SURE WHY THIS ISN'T UPDATING SECONDS EACH TIME
    // myUnixTime = tCounter;
    // open the file to write 300 data points

    if (IsBetweenHours(myUnixTime, 10, 12)) {
      // Serial.print("Send data to the WIFI network during the day...")
      return;
    }

  File dataFile = SD.open(myFilename, FILE_WRITE);

  if (dataFile) {

    for (int i = 0; i < 300; i++) {

      strain = Get_Data(); // CHECK TO SEE IF THIS CAN BE FASTER BY DIRECT CALL TO THE AD7193 INSTEAD OF GET_DATA FUNCTION

      dataFile.print(strain);
      dataFile.print(", ");  
      dataFile.println(myUnixTime); // add the time stamp on every loop

      if (debug) {

        Serial.print(strain);
        Serial.print(", ");  
        // Serial.println(Get_TimeStampString()); // add the time stamp each time which will slow it down
        Serial.println(myUnixTime); // add the time stamp on every loop with the UNIX TIME

      }  // end of if debug



    }  // end of for loop; data file being open, now do time lag items

    // show user we are collecting data BY SHOWING IT ON THE LCD
    // Write to LCD
    lcd.setCursor(6, 0);
    lcd.print(strain);  // do this while we are messing with closing the datafile

    // close file, saving all in the loop
    dataFile.close();  // at the end, close before we start another loop


  } else {  // if you can't open a file, we can't go forward. Show user that it didn't work

    Serial.println("Error opening file ");  //  + myFile);  // if the file isn't open, pop up an error:
    lcd.setCursor(0, 0);
    lcd.print("Can't open file!!");  // do this while we are messing with closing the datafile
    delay(2000);
    lcd.setCursor(0, 1);
    lcd.print("Figure it out!");  // do this while we are messing with closing the datafile
    delay(10000);

  }
}


// -- END OF FILE --
