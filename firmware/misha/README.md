# Setting up the MISHA LED panels

## Flashing the controller code
### Download Arduino software 
1. Download and install the Arduino IDE software for your windows computer: https://www.arduino.cc/en/software

###  Setup to program the Arduino Boards
1. Open Arduino IDE
2. Go to Tools → Manage Libraries...
  - Search for `Adafruit MCP23017` and install version 2.3.2+
  - Search for `SoftPWM` and install version 1.0.1+
3. Close the Library Manager
4. Go to Tools → Board → Arduino AVR Boards and select `Arduino Nano`
5. Go to Tools → Processor and select `ATmega328P`
6. Go to Tools → Port and select the device you want to program
7. Can now use the regular `Upload` button

### How to Control the Arduino Boards
The board will accept a command through the virtual COM port (through USB), in the form `wavelength,intensity\n`.
This will turn off all other LEDs and turn on the requested wavelength at the given intensity.

> [!NOTE]
> The command must have a comma separating the wavelength and intensity, and it must end with a newline character.


Examples:
```python
conn.write('420,100\n'.encode())  # set 420um LED to 100% intensity
conn.write('590,50\n'.encode())   # set 590um LED to 50% intensity
conn.write('590,\n'.encode())     # Device will report invalid command received
conn.write('0,0\n'.encode())      # Turns off all LEDs
```

### Identifying the device
Sending `ID\n` (or `?\n`) asks the board to identify itself. It replies with a single
comma-separated line, `name,hardware revision,software version`:

```python
conn.write('ID\n'.encode())
conn.readline()  # b'MISHA LED Panels,1.1,1.0.0\r\n'
```

Use this to confirm a serial port belongs to a MISHA board before sending LED commands.
The hardware revision tracks the PCB (currently 1.1); the software version tracks
`MISHA.ino` and is set by the `SW_VERSION` define at the top of that file.
