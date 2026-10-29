
from pathlib import Path
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON
from mutagen.id3 import ID3NoHeaderError

# MP3 file received from the ESP32
MP3_PATH = Path(__file__).resolve().parent / "Nokia_received.mp3"


def display_metadata(tags):
    print("\nCurrent MP3 metadata:")

    fields = {
        "Title": "TIT2",
        "Artist": "TPE1",
        "Album": "TALB",
        "Year": "TDRC",
        "Genre": "TCON",
    }

    for label, tag_id in fields.items():
        tag = tags.get(tag_id)
        value = str(tag.text[0]) if tag and tag.text else "Not set"
        print(f"{label}: {value}")


def edit_metadata():
    if not MP3_PATH.is_file():
        print(f"ERROR: MP3 file not found:\n{MP3_PATH}")
        return

    try:
        tags = ID3(MP3_PATH)
    except ID3NoHeaderError:
        print("This MP3 has no existing ID3 tags. Creating new tags.")
        tags = ID3()

    print(f"File: {MP3_PATH.name}")
    display_metadata(tags)

    print("\nEnter the new metadata.")
    print("Press Enter to keep the current value.")

    fields = [
        ("Title", "TIT2", TIT2),
        ("Artist", "TPE1", TPE1),
        ("Album", "TALB", TALB),
        ("Year", "TDRC", TDRC),
        ("Genre", "TCON", TCON),
    ]

    for label, tag_id, tag_class in fields:
        current_tag = tags.get(tag_id)
        current_value = (
            str(current_tag.text[0])
            if current_tag and current_tag.text
            else ""
        )

        new_value = input(
            f"{label} [{current_value or 'Not set'}]: "
        ).strip()

        if new_value:
            tags.setall(tag_id, [tag_class(encoding=3, text=new_value)])

    tags.save(MP3_PATH, v2_version=3)

    # Read the saved tags again to verify the changes
    verified_tags = ID3(MP3_PATH)

    print("\nMetadata saved and verified successfully!")
    display_metadata(verified_tags)


if __name__ == "__main__":
    edit_metadata()
