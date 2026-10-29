
import time
import zlib
from pathlib import Path

import serial

PORT = "COM16"
BAUD = 115200

SD_FILENAME = "nokia.mp3"
OUTPUT_PATH = Path(__file__).resolve().parent / "Nokia_received.mp3"

BLOCK_SIZE = 512
RESPONSE_TIMEOUT = 15.0
STALL_TIMEOUT = 15.0


def send_command(ser, command):
    payload = (command + "\n").encode("ascii")
    written = ser.write(payload)

    if written != len(payload):
        raise IOError("Could not send the complete command.")

    ser.flush()


def read_line(ser, timeout=RESPONSE_TIMEOUT):
    deadline = time.monotonic() + timeout
    data = bytearray()

    while time.monotonic() < deadline:
        char = ser.read(1)

        if not char:
            continue

        if char == b"\n":
            return data.decode("ascii", errors="replace").strip()

        data.extend(char)

        if len(data) > 256:
            raise RuntimeError("Invalid or oversized response header.")

    raise TimeoutError(
        f"Timed out waiting for response: {bytes(data)!r}"
    )


def read_exactly(ser, size):
    data = bytearray()
    last_progress = time.monotonic()

    while len(data) < size:
        chunk = ser.read(size - len(data))

        if chunk:
            data.extend(chunk)
            last_progress = time.monotonic()
        elif time.monotonic() - last_progress >= STALL_TIMEOUT:
            raise TimeoutError(
                f"Received {len(data)} of {size} bytes before timeout."
            )

    return bytes(data)


def check_error(line):
    if line.startswith("ERR_"):
        raise RuntimeError(f"ESP32 reported: {line}")


def synchronize(ser):
    time.sleep(3)
    ser.reset_input_buffer()

    send_command(ser, "PING")

    deadline = time.monotonic() + RESPONSE_TIMEOUT

    while time.monotonic() < deadline:
        line = read_line(ser, deadline - time.monotonic())

        if line == "PONG":
            return

        if line:
            print("ESP32:", line)

    raise TimeoutError("ESP32 did not respond to PING.")


def receive_file():
    temp_path = OUTPUT_PATH.with_suffix(".mp3.part")

    try:
        with serial.Serial(
            PORT,
            BAUD,
            timeout=0.25,
            write_timeout=5,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        ) as ser:

            synchronize(ser)

            print(f"Requesting {SD_FILENAME}...")
            send_command(ser, f"GET {SD_FILENAME}")

            header = read_line(ser)
            check_error(header)

            fields = header.split()

            if len(fields) != 2 or fields[0] != "FILE":
                raise RuntimeError(f"Unexpected file header: {header!r}")

            file_size = int(fields[1])

            if file_size <= 0:
                raise RuntimeError("Invalid file size.")

            print(f"File size: {file_size:,} bytes")

            received = 0
            started = time.monotonic()

            with temp_path.open("wb") as output:
                while received < file_size:
                    count = min(BLOCK_SIZE, file_size - received)

                    send_command(ser, f"READ {received} {count}")

                    header = read_line(ser)
                    check_error(header)

                    fields = header.split()

                    if len(fields) != 4 or fields[0] != "DATA":
                        raise RuntimeError(
                            f"Unexpected block header: {header!r}"
                        )

                    offset = int(fields[1])
                    block_size = int(fields[2])
                    expected_crc = int(fields[3], 16)

                    if offset != received or block_size != count:
                        raise RuntimeError("Unexpected block offset or size.")

                    block = read_exactly(ser, block_size)
                    actual_crc = zlib.crc32(block) & 0xFFFFFFFF

                    if actual_crc != expected_crc:
                        raise RuntimeError(
                            f"CRC mismatch at byte {received}."
                        )

                    output.write(block)
                    received += block_size

                    percentage = received * 100 / file_size

                    print(
                        f"\rReceived {received:,}/{file_size:,} bytes "
                        f"({percentage:.1f}%)",
                        end="",
                        flush=True,
                    )

            print()

            send_command(ser, "CLOSE")
            response = read_line(ser)
            check_error(response)

            if response != "OK":
                raise RuntimeError(f"Unexpected CLOSE response: {response!r}")

            if temp_path.stat().st_size != file_size:
                raise RuntimeError("Output file size verification failed.")

            temp_path.replace(OUTPUT_PATH)

            print("Transfer completed successfully.")
            print(f"Saved to: {OUTPUT_PATH}")
            print(f"Elapsed time: {time.monotonic() - started:.1f} seconds")

    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    try:
        receive_file()
    except KeyboardInterrupt:
        print("\nTransfer cancelled.")
    except (
        serial.SerialException,
        OSError,
        ValueError,
        TimeoutError,
        RuntimeError,
    ) as error:
        print(f"\nTRANSFER FAILED: {error}")
