/**
 * ----------------------
 * EduceLab XLC1 Firmware
 * ----------------------
 * Commands are framed by < and >. Every command is answered with exactly one
 * line: OK if it was carried out, or ERR <reason> if it wasn't. A rejected
 * command changes nothing.
 *
 *   <?> or <ID> - Report "name,hardware version,firmware version".
 *
 *   <C,R,G,B> - Set color for all LEDs on the strip, where R,G,B are each a value from 0 to 255.
 **/

#define RPIN 3
#define GPIN 5
#define BPIN 6

// Device identification, reported in response to the <?> handshake command
#define DEVICE_NAME "EduceLab XLC1"
#define HW_VERSION  "1.0"
#define SW_VERSION  "2.0.0"

const byte numChars = 32;
char receivedChars[numChars];
boolean overflow = false;
boolean newData = false;

void setup() {
  pinMode(RPIN, OUTPUT);
  pinMode(GPIN, OUTPUT);
  pinMode(BPIN, OUTPUT);

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
    Serial.println(DEVICE_NAME "," HW_VERSION "," SW_VERSION);
    return;
  }

  char * fields[5];
  int count = splitFields(receivedChars, fields, 5);
  if(strcmp(fields[0], "C") == 0) {
    int values[3];
    if(count != 4 || !parseLevels(fields + 1, values, 3)) {
      Serial.println("ERR invalid command");
      return;
    }
    setColor(values[0], values[1], values[2]);
    Serial.println("OK");
  }
  else {
    Serial.println("ERR unknown command");
  }
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

void setColor(int R, int G, int B) {
  analogWrite(RPIN, R);
  analogWrite(GPIN, G);
  analogWrite(BPIN, B);
}
