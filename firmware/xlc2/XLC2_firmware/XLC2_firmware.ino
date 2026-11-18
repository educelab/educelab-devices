/**
 * ----------------------
 * EduceLab XLC2 Firmware
 * ----------------------
 * Commands are framed by < and >. Every command is answered with exactly one
 * line: OK if it was carried out, or ERR <reason> if it wasn't. A rejected
 * command changes nothing.
 *
 *   <?> or <ID> - Report "name,hardware version,firmware version,unit ID". The
 *   unit ID is empty until one is set.
 *
 *   <U,id> - Store a unit ID in EEPROM: 1-16 characters from A-Z, a-z, 0-9,
 *   '_' and '-', with no spaces. It survives power cycles and reflashing through the
 *   bootloader.
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
#include <EEPROM.h>

// Device identification, reported in response to the <?> handshake command
#define DEVICE_NAME "EduceLab XLC2"
#define HW_VERSION  "1.0"
#define SW_VERSION  "2.1.0"

// Unit ID in EEPROM: a magic byte, a length, then the characters. Blank EEPROM
// (0xFF) has no magic byte, so it reads as unset.
#define ID_MAGIC      0xEC
#define ID_MAGIC_ADDR 0
#define ID_LEN_ADDR   1
#define ID_TEXT_ADDR  2
#define ID_MAX_LEN    16
char unitId[ID_MAX_LEN + 1] = "";

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
boolean overflow = false;
boolean newData = false;

void setup() {
  // Init XMount
  pinMode(xMountLEDs, OUTPUT);  // set Xmount pin as output
  analogWrite(xMountLEDs, 0);

  // Init strip
  strip.begin();
  // Setup lights
  strip.fill(strip.Color(0,0,0), 0, NUMPIXELS);
  strip.show();

  loadUnitId();

  // Start serial
  Serial.begin(9600);
}

void loop() {
  receive();
  if(newData == true){
    handleCommand();
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
    if(rc == startMarker) {
      // A new frame replaces one left unfinished
      recvInProgress = true;
      overflow = false;
      i = 0;
    }
    else if(recvInProgress == true) {
      if(rc != endMarker) {
        if(i < numChars - 1) {
          receivedChars[i] = rc;
          i++;
        }
        else {
          overflow = true;
        }
      }
      else {
        receivedChars[i] = '\0';
        recvInProgress = false;
        i = 0;
        newData = true;
      }
    }
  }
}

void handleCommand() {
  if(overflow) {
    Serial.println("ERR invalid command");
    return;
  }

  // Handshake: the one command answered with something other than OK/ERR
  if(strcmp(receivedChars, "?") == 0 || strcasecmp(receivedChars, "ID") == 0) {
    Serial.print(DEVICE_NAME "," HW_VERSION "," SW_VERSION ",");
    Serial.println(unitId);
    return;
  }

  // The unit ID isn't a level, so it's checked on its own
  if(receivedChars[0] == 'U' && (receivedChars[1] == ',' || receivedChars[1] == '\0')) {
    if(receivedChars[1] == '\0' || !storeUnitId(receivedChars + 2)) {
      Serial.println("ERR invalid command");
      return;
    }
    Serial.println("OK");
    return;
  }

  char * fields[6];
  int count = splitFields(receivedChars, fields, 6);
  int values[4];
  if(strcmp(fields[0], "B") == 0) {
    if(count != 2 || !parseLevels(fields + 1, values, 1)) {
      Serial.println("ERR invalid command");
      return;
    }
    setBrightness(values[0]);
  }
  else if(strcmp(fields[0], "X") == 0) {
    if(count != 2 || !parseLevels(fields + 1, values, 1)) {
      Serial.println("ERR invalid command");
      return;
    }
    setXBrightness(values[0]);
  }
  else if(strcmp(fields[0], "S") == 0) {
    if(count != 5 || !parseLevels(fields + 1, values, 4)) {
      Serial.println("ERR invalid command");
      return;
    }
    setSegments(values[0], values[1], values[2], values[3]);
  }
  else if(strcmp(fields[0], "L") == 0) {
    if(count != 3 || !parseLevels(fields + 1, values, 2)) {
      Serial.println("ERR invalid command");
      return;
    }
    if(values[0] >= NUMPIXELS) {
      Serial.println("ERR index out of range");
      return;
    }
    setIndex(values[0], values[1]);
  }
  else {
    Serial.println("ERR unknown command");
    return;
  }
  Serial.println("OK");
}

// Splits text on commas in place. Returns the number of fields, or max + 1 if
// there are more than max.
int splitFields(char * text, char ** fields, int max) {
  int count = 0;
  char * start = text;
  while(true) {
    if(count == max) {
      return max + 1;
    }
    fields[count++] = start;
    char * comma = strchr(start, ',');
    if(comma == NULL) {
      return count;
    }
    *comma = '\0';
    start = comma + 1;
  }
}

// Parses each field as a whole number from 0 to 255, allowing spaces around it.
boolean parseLevels(char ** fields, int * values, int count) {
  for(int f = 0; f < count; f++) {
    char * text = fields[f];
    while(*text == ' ') {
      text++;
    }
    int length = strlen(text);
    while(length > 0 && text[length - 1] == ' ') {
      length--;
    }
    text[length] = '\0';
    if(length == 0 || length > 3) {
      return false;
    }
    for(int c = 0; c < length; c++) {
      if(!isDigit(text[c])) {
        return false;
      }
    }
    values[f] = atoi(text);
    if(values[f] > 255) {
      return false;
    }
  }
  return true;
}

// Reads the unit ID from EEPROM, leaving it empty if none was stored.
void loadUnitId() {
  unitId[0] = '\0';
  if(EEPROM.read(ID_MAGIC_ADDR) != ID_MAGIC) {
    return;
  }
  byte length = EEPROM.read(ID_LEN_ADDR);
  if(length == 0 || length > ID_MAX_LEN) {
    return;
  }
  for(byte c = 0; c < length; c++) {
    unitId[c] = EEPROM.read(ID_TEXT_ADDR + c);
  }
  unitId[length] = '\0';
}

// Validates and stores a unit ID. Returns false, changing nothing, if it's invalid.
boolean storeUnitId(const char * text) {
  int length = strlen(text);
  if(length == 0 || length > ID_MAX_LEN) {
    return false;
  }
  for(int c = 0; c < length; c++) {
    char ch = text[c];
    boolean allowed = (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') ||
                      (ch >= '0' && ch <= '9') || ch == '_' || ch == '-';
    if(!allowed) {
      return false;
    }
  }
  // Cleared first, so a write cut short reads as unset rather than garbled
  EEPROM.update(ID_MAGIC_ADDR, 0xFF);
  for(int c = 0; c < length; c++) {
    EEPROM.update(ID_TEXT_ADDR + c, text[c]);
  }
  EEPROM.update(ID_LEN_ADDR, length);
  EEPROM.update(ID_MAGIC_ADDR, ID_MAGIC);
  strcpy(unitId, text);
  return true;
}

void setBrightness(int brightness) {
  strip.fill(strip.Color(brightness,brightness,brightness), 0, NUMPIXELS);
  strip.show();
}

void setSegments(int seg1, int seg2, int seg3, int seg4) {
  strip.clear();
  strip.fill(strip.Color(seg1,seg1,seg1), 0, 55);
  strip.fill(strip.Color(seg2,seg2,seg2), 55, 56);
  strip.fill(strip.Color(seg3,seg3,seg3), 111, 56);
  strip.fill(strip.Color(seg4,seg4,seg4), 167, 55);
  strip.show();
}

void setIndex(int index, int brightness) {
  strip.setPixelColor(index, brightness, brightness, brightness);
  strip.show();
}

void setXBrightness(int brightness) {
  analogWrite(xMountLEDs, brightness);
}
