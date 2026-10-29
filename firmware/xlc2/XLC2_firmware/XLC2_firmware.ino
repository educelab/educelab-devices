/**
 * ----------------------
 * EduceLab XLC2 Firmware
 * ----------------------
 * Available commands:
 *
 *   <?> - Report controller name and firmware version.
 *
 *   <B, #> - Set brightness for all LEDs on the pixel strip, where # is a value from 0 to 255.
 *
 *   <L, I, B> - Set brightness for individual LEDs on the pixel strip, where I is an LED index [0-221] and b is a
 *   brightness [0-255].
 *
 *   <S, #, #, #, #> - Set pixel segments brightness where # is 0-255. For convenience, the pixel strip is divided into
 *   four groups of ~55 pixels.
 *
 *   <X, #> - Set the XMount LED brightness where # is 0 to 255. This controls the brightness for all of the XMount
 *   LEDs.They can be turned off by sending a 0 brightness value.
 **/

#include <Adafruit_DotStar.h>
#include <SPI.h>

// Firmware Version
#define VERSION_MAJOR "1"
#define VERSION_MINOR "2"
#define VERSION_PATCH "0"

// The PWM pin for the XMount LED array
// Hardware SPI is a little faster, but must be wired to specific pins
// (Arduino Uno = pin 11 for data, 13 for clock, other boards are different)
int xMountLEDs = 6;

// DotStar config
#define NUMPIXELS 222
Adafruit_DotStar strip(NUMPIXELS, DOTSTAR_BRG);

// Serial IO
const byte numChars = 32;
char receivedChars[numChars];
char tempChars[numChars];

char command[numChars] = {0}; // ?,B,S,X,L
int seg1 = 0; // 0 - 255
int seg2 = 0; // 0 - 255
int seg3 = 0; // 0 - 255
int seg4 = 0; // 0 - 255
int brightVal = 0; // 0 - 255
int xmountVal = 0; // 0 - 255
int index = 0;

boolean newData = false;

void setup() {
  // Init XMount
  pinMode(xMountLEDs, OUTPUT);  // set Xmount pin as output
  analogWrite(xMountLEDs, 0);

  // Init strip
  strip.begin();
  // Setup lights
  strip.fill(strip.Color(0,0,0), 0, 240);
  strip.show();

  // Start serial
  Serial.begin(9600);
}

void loop() {
  receive();
  if(newData == true){
    strcpy(tempChars, receivedChars);
    parseData();
    if (command[0] == '?') {
      Serial.println("EduceLab XLC2 (FW "VERSION_MAJOR"."VERSION_MINOR"."VERSION_PATCH")");
    }
    else if(command[0] == 'B') {
      setBrightness(brightVal);
    }
    else if (command[0] == 'L') {
      setIndex(index, brightVal);
    }
    else if (command[0] == 'S') {
      setSegments(seg1, seg2, seg3, seg4);
    }
    else if (command[0] == 'X') {
      setXBrightness(xmountVal);
    }
    else{
      Serial.println("Unknown Command");
    }
    newData = false;
  }
}

void receive() {
  static boolean recvInProgress = false;
  static byte i = 0;
  char startMarker = '<';
  char endMarker = '>';
  char rc;

  while (Serial.available() > 0 && newData == false) {
    rc = Serial.read();
    if(recvInProgress == true) {
      if(rc != endMarker) {
        receivedChars[i] = rc;
        i++;
        if(i >= numChars) {
          i = numChars -1;
        }
      }
      else {
        receivedChars[i] = '\0';
        recvInProgress = false;
        i = 0;
        newData = true;
      }
    }
    else if(rc == startMarker) {
      recvInProgress = true;
    }
  } 
}

void parseData() {
    char * strtokIndx;

    strtokIndx = strtok(tempChars,",");      // get the first part - the string
    strcpy(command, strtokIndx); // copy it to messageFromPC
    if (command[0] == '?') {
      // nothing to parse
    }
    else if(command[0] == 'B') {
      Serial.println("Got a brightness command");
      strtokIndx = strtok(NULL, ","); 
      brightVal = atoi(strtokIndx);
    }
    else if(command[0] == 'S') {
      Serial.println("Got a segment command");
      strtokIndx = strtok(NULL, ",");
      seg1 = atoi(strtokIndx);
      strtokIndx = strtok(NULL, ",");
      seg2 = atoi(strtokIndx);
      strtokIndx = strtok(NULL, ",");
      seg3 = atoi(strtokIndx);
      strtokIndx = strtok(NULL, ",");
      seg4 = atoi(strtokIndx);
    }
    else if(command[0] == 'X') {
      Serial.println("Got a xmount command");
      strtokIndx = strtok(NULL, ",");
      xmountVal = atoi(strtokIndx);
    }
    else if(command[0] == 'L') {
      Serial.println("Got an index command");
      strtokIndx = strtok(NULL, ",");
      index = atoi(strtokIndx);
      strtokIndx = strtok(NULL, ",");
      brightVal = atoi(strtokIndx);
    }    
    else {
      Serial.println("Unknown Command");
    }
}

void setBrightness(int brightness) {
  brightness = constrain(brightness, 0, 255);
  Serial.print("Setting brightness to ");
  Serial.println(brightness);
  strip.fill(strip.Color(brightness,brightness,brightness), 0, 240);
  strip.show();
}

void setSegments(int seg1, int seg2, int seg3, int seg4) {
  seg1 = constrain(seg1, 0, 255);
  seg2 = constrain(seg2, 0, 255);
  seg3 = constrain(seg3, 0, 255);
  seg4 = constrain(seg4, 0, 255);
  strip.clear();
  strip.fill(strip.Color(seg1,seg1,seg1), 0, 55);
  strip.fill(strip.Color(seg2,seg2,seg2), 55, 56);
  strip.fill(strip.Color(seg3,seg3,seg3), 111, 56);
  strip.fill(strip.Color(seg4,seg4,seg4), 167, 55);
  strip.show();
}

void setIndex(int index, int brightness) {
  brightness = constrain(brightness, 0, 255);
  strip.setPixelColor(index, brightness, brightness, brightness);
  strip.show();
}

void setXBrightness(int brightness) {
  brightness = constrain(brightness, 0, 255);
  analogWrite(xMountLEDs, brightness);
}
