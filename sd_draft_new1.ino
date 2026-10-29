#include <Arduino.h>
#include <SPI.h>
#include <SD.h>

#define SD_CS       5
#define SD_SCK      18
#define SD_MISO     19
#define SD_MOSI     23
#define BUFFER_SIZE 512

SPIClass sdSPI(VSPI);
File currentFile;
uint8_t buffer[BUFFER_SIZE];

void ListFiles();
void OpenFile(String filename);
void SendBlock(uint32_t offset, size_t count);
void CloseFile();
uint32_t CRC32(const uint8_t *data, size_t length);

void setup()
{
    Serial.begin(115200);
    Serial.setTimeout(1000);
    delay(1500);

    sdSPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);

    if (!SD.begin(SD_CS, sdSPI, 400000))
    {
        Serial.println("ERR_SD_INIT");
        return;
    }

    Serial.println("READY");
}

void loop()
{
    if (!Serial.available())
        return;

    String cmd = Serial.readStringUntil('\n');
    cmd.trim();

    if (cmd == "PING")
    {
        Serial.println("PONG");
    }
    else if (cmd == "LIST")
    {
        ListFiles();
    }
    else if (cmd.startsWith("GET "))
    {
        OpenFile(cmd.substring(4));
    }
    else if (cmd.startsWith("READ "))
    {
        unsigned long offset = 0;
        unsigned int count = 0;
        char extra;

        int fields = sscanf(
            cmd.c_str(),
            "READ %lu %u %c",
            &offset, &count, &extra
        );

        if (fields != 2)
        {
            Serial.println("ERR_COMMAND");
            return;
        }

        SendBlock((uint32_t)offset, count);
    }
    else if (cmd == "CLOSE")
    {
        CloseFile();
    }
    else
    {
        Serial.println("ERR_COMMAND");
    }
}

void ListFiles()
{
    File root = SD.open("/");

    if (!root || !root.isDirectory())
    {
        Serial.println("ERR_DIRECTORY");
        return;
    }

    File entry;

    while ((entry = root.openNextFile()))
    {
        if (!entry.isDirectory())
        {
            Serial.print(entry.name());
            Serial.print(" | ");
            Serial.print((unsigned long)entry.size());
            Serial.println(" bytes");
        }

        entry.close();
    }

    root.close();
    Serial.println("ENDLIST");
}

void OpenFile(String filename)
{
    if (currentFile)
        currentFile.close();

    filename.trim();

    if (!filename.startsWith("/"))
        filename = "/" + filename;

    currentFile = SD.open(filename.c_str(), FILE_READ);

    if (!currentFile)
    {
        Serial.println("ERR_OPEN");
        return;
    }

    if (currentFile.isDirectory())
    {
        currentFile.close();
        Serial.println("ERR_DIRECTORY");
        return;
    }

    uint32_t size = currentFile.size();

    if (size == 0)
    {
        currentFile.close();
        Serial.println("ERR_EMPTY");
        return;
    }

    Serial.printf("FILE %lu\n", (unsigned long)size);
}

void SendBlock(uint32_t offset, size_t count)
{
    if (!currentFile)
    {
        Serial.println("ERR_NO_FILE");
        return;
    }

    uint32_t size = currentFile.size();

    if (count == 0 || count > BUFFER_SIZE ||
        offset >= size || count > size - offset)
    {
        Serial.println("ERR_RANGE");
        return;
    }

    if (!currentFile.seek(offset))
    {
        Serial.printf(
            "ERR_SD_SEEK_AT %lu\n",
            (unsigned long)offset
        );
        return;
    }

    size_t total = 0;

    while (total < count)
    {
        size_t n = currentFile.read(
            buffer + total,
            count - total
        );

        if (n == 0)
        {
            Serial.printf(
                "ERR_SD_READ_AT %lu\n",
                (unsigned long)(offset + total)
            );
            return;
        }

        total += n;
    }

    uint32_t crc = CRC32(buffer, count);

    Serial.printf(
        "DATA %lu %u %08lX\n",
        (unsigned long)offset,
        (unsigned int)count,
        (unsigned long)crc
    );

    size_t sent = 0;

    while (sent < count)
    {
        size_t written = Serial.write(
            buffer + sent,
            count - sent
        );

        if (written > 0)
            sent += written;
        else
            delay(1);
    }

    Serial.flush();
}

void CloseFile()
{
    if (currentFile)
        currentFile.close();

    Serial.println("OK");
}

uint32_t CRC32(const uint8_t *data, size_t length)
{
    uint32_t crc = 0xFFFFFFFFUL;

    for (size_t i = 0; i < length; i++)
    {
        crc ^= data[i];

        for (uint8_t bit = 0; bit < 8; bit++)
        {
            if (crc & 1)
                crc = (crc >> 1) ^ 0xEDB88320UL;
            else
                crc >>= 1;
        }
    }

    return crc ^ 0xFFFFFFFFUL;
}
