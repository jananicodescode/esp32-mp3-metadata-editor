# ESP32 MP3 Metadata Editor

An embedded file-transfer and metadata-editing application built using an ESP32, a microSD card module, and Python.

## Overview

This project transfers an MP3 file stored on a microSD card connected to an ESP32 to a Windows PC over serial communication. The received file is verified using CRC32 checksums and can then be edited using a Python-based MP3 metadata editor with a graphical user interface.

The project combines embedded C/C++ firmware with Python application development.

## Features

- **MicroSD access:** Reads an MP3 file from a microSD card using SPI.
- **UART file transfer:** Transfers file data from the ESP32 to a PC through a serial connection.
- **Data integrity verification:** Uses CRC32 checksums to verify received data blocks.
- **MP3 metadata editing:** Reads and updates supported ID3 tags.
- **Graphical user interface:** Provides controls for file transfer and metadata editing.
- **Playback and file properties:** Provides options to play the received MP3 and open its file properties.

## System Architecture

```text
      microSD Card
           |
           | SPI
           v
       ESP32 DevKit
           |
           | UART
           | USB-to-serial
           v
       Windows PC
           |
           v
    Python Application
           |
           +-- File Reception
           +-- CRC32 Verification
           +-- MP3 Metadata Editing
           +-- Graphical User Interface
```

## Hardware Requirements

- ESP32 DevKit V1
- HW-125 microSD card module
- microSD card containing an MP3 file
- USB cable for ESP32 programming and serial communication
- Jumper wires
- Windows PC

## Software Requirements

- ESP32 firmware development environment
- Python 3
- PySerial
- Mutagen
- Tkinter for the graphical interface

PyInstaller can optionally be used to package the Python GUI as a standalone Windows executable.

## Project Structure

```text
esp32-mp3-metadata-editor/
├── receive_mp3.py
├── edit_metadata.py
├── mp3_gui.py
├── MP3 Metadata Editor.spec
├── README.md
└── .gitignore
```

- `receive_mp3.py` — receives the MP3 file from the ESP32 and verifies the transferred data.
- `edit_metadata.py` — handles MP3 metadata operations.
- `mp3_gui.py` — provides the graphical application.
- `MP3 Metadata Editor.spec` — PyInstaller build configuration.
- `.gitignore` — excludes generated files and local development artifacts.

The ESP32 firmware source should also be included in the repository if maintained separately from these Python files.

## Python Dependencies

Install the required packages in your Python environment:

```bash
pip install pyserial mutagen
```

Tkinter is generally included with standard Windows Python installations.

## Hardware Configuration

The project uses the ESP32's VSPI interface to communicate with the microSD module.

| Signal | ESP32 Pin |
|---|---|
| CS | GPIO 5 |
| SCK | GPIO 18 |
| MISO | GPIO 19 |
| MOSI | GPIO 23 |

The serial connection between the ESP32 and PC uses a baud rate of 115200.

## Execution

1. Connect the microSD module to the ESP32 using the specified SPI pins.
2. Insert a microSD card containing the MP3 file.
3. Upload the corresponding firmware to the ESP32.
4. Connect the ESP32 to the PC.
5. Configure the Python application to use the correct serial port.
6. Run the receiver or launch the GUI.
7. Transfer the MP3 file and verify its integrity.
8. Edit the supported metadata fields and save the changes.

## Technologies Used

- Embedded C/C++
- ESP32
- SPI
- UART
- Python
- PySerial
- Mutagen
- Tkinter
- Git and GitHub

## Version Control

The project is maintained using Git. Commits record changes to the source code and documentation, while version tags can identify stable releases.

## Future Improvements

- Support transferring multiple files in one session.
- Add transfer recovery and retry mechanisms.
- Improve error reporting and serial-port configuration.
- Expand metadata editing and validation.
- Add automated testing for file-transfer integrity.

## Author

Devanand Eranchery

## License

A license has not yet been selected. Until one is added, do not assume that others have permission to redistribute or modify this project.
