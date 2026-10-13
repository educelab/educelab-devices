/*
MISHA Arduino program
Author: Reed Terdal
Date: 08/01/2021
*/

// Libraries required for operation
#include <SoftPWM.h>
#include <SoftPWM_timer.h>
#include <Adafruit_MCP23X17.h>
#include <Wire.h>
#include "HashMap.h"
#include <Arduino.h>

// Device identification, reported in response to the "ID" handshake command
#define DEVICE_NAME "MISHA LED Panels"
#define HW_VERSION  "1.1"
#define SW_VERSION  "1.0.0"

// Variables used for wavelength look-up
const byte NUM_LEDS = 16; 
HashType<int,int> hashRawArray[NUM_LEDS + 1]; 
HashMap<int,int> pinMapping = HashMap<int,int>( hashRawArray , NUM_LEDS + 1 ); 

// Variables for driving the LEDs
Adafruit_MCP23X17 mcp1;
Adafruit_MCP23X17 mcp2;

void setup() {
  // Configure the mapping of wavelength to 1-indexed pin number.
  // Using 1-based values so that 0 unambiguously means "not found".
  pinMapping[0](365,1);
  pinMapping[1](385,2);
  pinMapping[2](395,3);
  pinMapping[3](420,4);
  pinMapping[4](450,5);
  pinMapping[5](470,6);
  pinMapping[6](500,7);
  pinMapping[7](530,8);
  pinMapping[8](560,9);
  pinMapping[9](590,10);
  pinMapping[10](615,11);
  pinMapping[11](630,12);
  pinMapping[12](660,13);
  pinMapping[13](730,14);
  pinMapping[14](850,15);
  pinMapping[15](940,16);

  // Start the PWM
  SoftPWMBegin();
  SoftPWMSet(3, 0);

  // Set up the Arduinos pins 
  pinMode(4, OUTPUT); // ADDR Pin
  pinMode(7, OUTPUT); // Reset Pin
  
  // Start the Serial interface at 9600 Baud
  Serial.begin(9600);

  // Setup the LED driver
  digitalWrite(4, HIGH); // Set the address high on the board with the Arduino, other is low
  digitalWrite(7, LOW); 
  delay(10);            // Reset the LED driver
  digitalWrite(7, HIGH);
  delay(10);
  
  // Start the LED driver and initalize everything to output 0
  // New Adafruit_MCP23X17 library takes the full 7-bit I2C address.
  // The MCP23017 base address is 0x20; A0/A1/A2 pins add to that.
  // mcp1 has ADDR pin HIGH (A0=1) → 0x21; mcp2 is the slave with A0=0 → 0x20.
  mcp1.begin_I2C(0x21, &Wire);
  mcp2.begin_I2C(0x20, &Wire);
  for(int idx = 0; idx < 16; idx++)
  {
    mcp1.pinMode(idx, OUTPUT);
    mcp2.pinMode(idx, OUTPUT);
    mcp1.digitalWrite(idx, LOW);
    mcp2.digitalWrite(idx, LOW);
  }
  
}

void loop() {
  // Check if a new command is available
  if(Serial.available() > 0)
  {
    // Read the input and convert from a string to a char buffer.
    // Buffer is 32 bytes — enough for the longest command ("940,100\n")
    // with room to spare; the old 16-byte buffer could silently truncate.
    char buffer[32];
    String input = Serial.readStringUntil('\n');
    input.trim();

    // Handshake: let the host confirm which device and versions it's talking to
    if(input.equalsIgnoreCase("ID") || input == "?")
    {
      Serial.println(DEVICE_NAME "," HW_VERSION "," SW_VERSION);
      return;
    }

    input.toCharArray(buffer, 32);
    
    // Parse the input to get the wavelength and intensity
    int wavelength;
    int intensity;
    int result = sscanf(buffer, "%d,%d", &wavelength, &intensity);
    if(result != 2)
    {
      Serial.println("Invalid command");
      return;
    }
    
    // Turn off all LEDs first (handles the wavelength==0 "turn off" command
    // and clears state before lighting a new channel).
    turnOffAll();

    // Look up the 1-indexed pin number for this wavelength.
    // getValueOf() returns 0 when the key is not found — which is now
    // unambiguously "not found" because all valid entries are >= 1.
    int pinNumber = pinMapping.getValueOf(wavelength);
    if(pinNumber >= 1 && pinNumber <= NUM_LEDS)
    {
      int pin = pinNumber - 1;  // convert to 0-indexed MCP pin
      SoftPWMSetPercent(3, intensity);
      mcp1.digitalWrite(pin, HIGH);
      mcp2.digitalWrite(pin, HIGH);
    }
  }
}

void turnOffAll()
{
  // Turns off all LEDs
  SoftPWMSetPercent(3, 0);
  for(int idx = 0; idx < 16; idx++)
  {
    mcp1.digitalWrite(idx, LOW);
    mcp2.digitalWrite(idx, LOW);
  }
}
