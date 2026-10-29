/**
 * ----------------------
 * EduceLab XLC1 Firmware
 * ----------------------
 * Available commands:
 *
 *   <?> - Report controller name and firmware version.
 *
 *   <B, #> - Set brightness for all LEDs on the strip, where # is a value from 0 to 255.
 *
 *   <C, R, G, B> - Set color for all LEDs on the strip, where R,G,B are each a value from 0 to 255.
 **/

#define RPIN 3
#define GPIN 5
#define BPIN 6

const byte numChars = 32;
char receivedChars[numChars];
char tempChars[numChars];

char command[numChars] = {0}; // I,C,B
int Rval = 0; // 0 - 255
int Gval = 0; // 0 - 255
int Bval = 0; // 0 - 255
int brightVal = 0; // 0 - 100


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
    strcpy(tempChars, receivedChars);
    parseData();
    if (command[0] == '?') {
      Serial.println("EduceLab XLC1 (FW 1.1)");
    }
    else if(command[0] == 'B') {
      setBrightness(brightVal);
    }
    else if (command[0] == 'C') {
      setColor(Rval, Gval, Bval);
    }
    else {
      Serial.println("Unknown command");
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
    if(command[0] == '?') {
      // nothing to parse
    }
    else if(command[0] == 'B') {
      Serial.println("Got a brightness command");
      strtokIndx = strtok(NULL, ","); 
      brightVal = atoi(strtokIndx);
    }
    else if(command[0] == 'C') {
      Serial.println("Got a color command");
      strtokIndx = strtok(NULL, ",");
      Rval = atoi(strtokIndx);
      strtokIndx = strtok(NULL, ",");
      Gval = atoi(strtokIndx);
      strtokIndx = strtok(NULL, ",");
      Bval = atoi(strtokIndx);
    }
    else {
      Serial.println("Unknown Command");
    }
}

void setBrightness(int brightness) {
  Serial.print("Setting brightness to ");
  Serial.println(brightness);
}

void setColor(int R, int G, int B) {
  Serial.print("Setting color to ");
  Serial.print(R);
  Serial.print(", ");
  Serial.print(G);
  Serial.print(", ");
  Serial.println(B);
  analogWrite(RPIN, R);
  analogWrite(GPIN, G);
  analogWrite(BPIN, B);
}
