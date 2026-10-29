import os
import time
import zlib
import threading
import queue
import ctypes
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

import serial
from mutagen.id3 import (
    ID3, TIT2, TPE1, TALB, TDRC, TCON, ID3NoHeaderError
)


# -------------------- CONFIGURATION --------------------

PORT = "COM16"
BAUD = 115200
BLOCK_SIZE = 512

WORK_DIR = Path(__file__).resolve().parent

TAG_FIELDS = [
    ("Title", "TIT2", TIT2),
    ("Artist", "TPE1", TPE1),
    ("Album", "TALB", TALB),
    ("Year", "TDRC", TDRC),
    ("Genre", "TCON", TCON),
]

# -------------------- THEME (dark / mint, matches XEV 9S lab dashboard) --------------------

C = {
    "bg": "#0C1015",
    "rail": "#11171D",
    "panel": "#13191F",
    "panel2": "#182129",
    "border": "#28313A",
    "border2": "#33414C",
    "field": "#172028",
    "muted": "#8996A3",
    "muted2": "#6B7B89",
    "text": "#E4EBEF",
    "text2": "#C1CDD7",
    "green": "#66D5B3",
    "green_bg": "#1E3832",
    "green_fg": "#74D3B4",
    "mint": "#A8E7CF",
    "mint_hover": "#BFF0DD",
    "mint_text": "#122B24",
    "orange": "#F19D64",
    "red": "#EF727C",
    "btn": "#1C252D",
    "btn_hover": "#26323C",
    "sel": "#233B38",
    "log_bg": "#10171D",
}

_FALLBACK_SANS = "Segoe UI"
_FALLBACK_MONO = "Consolas"
_FALLBACK_NUM = "Segoe UI Semibold"


def pick_font(candidates, fallback):
    import tkinter.font as tkfont
    available = set(tkfont.families())
    for name in candidates:
        if name in available:
            return name
    return fallback


# -------------------- ESP32 COMMUNICATION --------------------

def send_command(ser, command):
    payload = (command + "\n").encode("ascii")

    if ser.write(payload) != len(payload):
        raise IOError("Could not send the complete command.")

    ser.flush()


def read_line(ser, timeout=15):
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
            raise RuntimeError("Invalid ESP32 response header.")

    raise TimeoutError(
        f"Timed out waiting for ESP32. Received: {bytes(data)!r}"
    )


def check_error(line):
    if line.startswith("ERR_"):
        raise RuntimeError(f"ESP32 error: {line}")


def connect_esp32():
    ser = serial.Serial(
        PORT,
        BAUD,
        timeout=0.25,
        write_timeout=5,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )

    try:
        time.sleep(3)
        ser.reset_input_buffer()
        send_command(ser, "PING")

        deadline = time.monotonic() + 15

        while time.monotonic() < deadline:
            line = read_line(ser, deadline - time.monotonic())

            if line == "PONG":
                return ser

        raise TimeoutError("ESP32 did not respond to PING.")

    except Exception:
        ser.close()
        raise


def list_sd_files(ser):
    send_command(ser, "LIST")
    files = []
    deadline = time.monotonic() + 15

    while time.monotonic() < deadline:
        line = read_line(ser, deadline - time.monotonic())

        if line == "ENDLIST":
            return files

        check_error(line)

        if " | " in line and line.endswith(" bytes"):
            filename, size_text = line.rsplit(" | ", 1)
            size_text = size_text.removesuffix(" bytes").strip()

            try:
                size = int(size_text)
            except ValueError:
                continue

            filename = Path(filename.replace("\\", "/")).name

            if filename and filename not in (".", ".."):
                files.append((filename, size))

    raise TimeoutError("Timed out while listing SD files.")


def transfer_file(filename, output_path, progress_callback):
    temp_path = output_path.with_suffix(output_path.suffix + ".part")

    try:
        with connect_esp32() as ser:
            send_command(ser, f"GET {filename}")

            header = read_line(ser)
            check_error(header)

            fields = header.split()

            if len(fields) != 2 or fields[0] != "FILE":
                raise RuntimeError(f"Unexpected file header: {header!r}")

            file_size = int(fields[1])

            if file_size <= 0:
                raise RuntimeError("The file is empty or has an invalid size.")

            received = 0
            progress_callback(0, file_size)

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
                        raise RuntimeError("Invalid block offset or size.")

                    block = bytearray()
                    last_progress = time.monotonic()

                    while len(block) < block_size:
                        chunk = ser.read(block_size - len(block))

                        if chunk:
                            block.extend(chunk)
                            last_progress = time.monotonic()
                        elif time.monotonic() - last_progress >= 15:
                            raise TimeoutError(
                                f"Transfer stalled at byte {received}."
                            )

                    actual_crc = zlib.crc32(block) & 0xFFFFFFFF

                    if actual_crc != expected_crc:
                        raise RuntimeError(
                            f"CRC32 verification failed at byte {received}."
                        )

                    output.write(block)
                    received += block_size

                    # Update progress only after a block passes CRC32.
                    progress_callback(received, file_size)

            send_command(ser, "CLOSE")
            response = read_line(ser)
            check_error(response)

            if response != "OK":
                raise RuntimeError(f"Unexpected CLOSE response: {response!r}")

        if temp_path.stat().st_size != file_size:
            raise RuntimeError("Final file-size verification failed.")

        temp_path.replace(output_path)
        return output_path

    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


# -------------------- METADATA FUNCTIONS --------------------

def load_tags(file_path):
    try:
        return ID3(file_path)
    except ID3NoHeaderError:
        return ID3()


def read_metadata(file_path):
    tags = load_tags(file_path)
    values = {}

    for label, tag_id, _ in TAG_FIELDS:
        tag = tags.get(tag_id)
        values[label] = (
            str(tag.text[0])
            if tag and tag.text
            else ""
        )

    return values


def save_metadata(file_path, values):
    tags = load_tags(file_path)

    for label, tag_id, tag_class in TAG_FIELDS:
        value = values[label].strip()

        if value:
            tags.setall(
                tag_id,
                [tag_class(encoding=3, text=value)]
            )
        else:
            tags.delall(tag_id)

    # ID3v2.3 is the version Windows Explorer reads most reliably.
    tags.save(file_path, v2_version=3)

    verified = read_metadata(file_path)

    for label, _, _ in TAG_FIELDS:
        if verified[label] != values[label].strip():
            raise RuntimeError(
                f"Metadata verification failed for {label}."
            )


# -------------------- WINDOWS FILE ACTIONS --------------------

def play_mp3(file_path):
    if not file_path.is_file():
        raise FileNotFoundError("The local MP3 file does not exist.")

    if os.name != "nt":
        raise OSError("Playback is configured for Windows.")

    os.startfile(str(file_path))


def open_file_properties(file_path, page="Details"):
    """Open the native Windows Properties dialog directly on the given tab.

    SHObjectProperties' last argument is the name of the initial tab,
    so passing "Details" opens Properties -> Details, which shows
    Title, Contributing artists, Album, Year and Genre.
    """
    if not file_path.is_file():
        raise FileNotFoundError("The local MP3 file does not exist.")

    if os.name != "nt":
        raise OSError("File Properties is configured for Windows.")

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    shell32.SHObjectProperties.argtypes = [
        ctypes.c_void_p,     # hwnd (owner window, may be None)
        ctypes.c_ulong,      # shop type
        ctypes.c_wchar_p,    # file path
        ctypes.c_wchar_p,    # initial tab name
    ]
    shell32.SHObjectProperties.restype = ctypes.c_int

    SHOP_FILEPATH = 2

    success = shell32.SHObjectProperties(
        None,
        SHOP_FILEPATH,
        str(file_path.resolve()),
        page,
    )

    if not success:
        error_code = ctypes.get_last_error()
        raise OSError(
            error_code,
            "Windows could not open the File Properties dialog."
        )


# -------------------- DESKTOP GUI --------------------

class ESDApplication:
    def __init__(self, root):
        self.root = root
        self.root.title("ESD Essentials - MP3 Metadata Editor")
        self.root.geometry("1040x880")
        self.root.minsize(900, 720)
        self.root.configure(bg=C["bg"])

        self.sans = pick_font(["Inter", "Segoe UI"], _FALLBACK_SANS)
        self.mono = pick_font(["IBM Plex Mono", "Consolas"], _FALLBACK_MONO)
        self.num = pick_font(["Barlow Condensed", "Segoe UI Semibold"],
                             _FALLBACK_NUM)

        self.events = queue.Queue()
        self.files = {}
        self.current_mp3 = WORK_DIR / "Nokia_received.mp3"
        self.tag_entries = {}
        self.transfer_active = False

        self.setup_styles()
        self.build_gui()
        self.root.after(100, self.process_events)

    # ---------- styling ----------

    def setup_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure(".", font=(self.sans, 10), background=C["panel"],
                        foreground=C["text"])

        style.configure("Panel.TFrame", background=C["panel"])
        style.configure("Panel.TLabel", background=C["panel"],
                        foreground=C["text2"], font=(self.sans, 10))
        style.configure("Muted.TLabel", background=C["panel"],
                        foreground=C["muted"], font=(self.mono, 9))
        style.configure("Field.TLabel", background=C["panel"],
                        foreground=C["muted"], font=(self.mono, 9))
        style.configure("Badge.TLabel", background=C["panel"],
                        foreground=C["red"], font=(self.mono, 10))

        # Buttons
        style.configure("Primary.TButton", background=C["mint"],
                        foreground=C["mint_text"],
                        font=(self.sans, 10, "bold"), padding=(14, 8),
                        borderwidth=1, bordercolor=C["mint"],
                        focusthickness=0, focuscolor=C["mint"])
        style.map("Primary.TButton",
                  background=[("pressed", C["green"]),
                              ("active", C["mint_hover"])],
                  bordercolor=[("active", C["mint_hover"])])

        style.configure("Default.TButton", background=C["btn"],
                        foreground=C["text2"], font=(self.sans, 10),
                        padding=(14, 8), borderwidth=1,
                        bordercolor="#36424D", focusthickness=0,
                        focuscolor=C["btn"])
        style.map("Default.TButton",
                  background=[("pressed", C["panel2"]),
                              ("active", C["btn_hover"])],
                  bordercolor=[("active", C["green"])],
                  foreground=[("active", C["text"])])

        # Entries
        style.configure("Dark.TEntry", fieldbackground=C["field"],
                        foreground=C["text2"], bordercolor=C["border2"],
                        lightcolor=C["border2"], darkcolor=C["border2"],
                        insertcolor=C["green"], padding=8)
        style.map("Dark.TEntry",
                  bordercolor=[("focus", C["green"])],
                  lightcolor=[("focus", C["green"])],
                  darkcolor=[("focus", C["green"])])

        # Treeview
        style.configure("Dark.Treeview", background=C["panel"],
                        fieldbackground=C["panel"], foreground="#A2B4C0",
                        rowheight=32, borderwidth=0, font=(self.mono, 10))
        style.configure("Dark.Treeview.Heading", background=C["panel2"],
                        foreground="#8193A1", font=(self.mono, 9),
                        padding=(14, 9), borderwidth=0, relief="flat")
        style.map("Dark.Treeview",
                  background=[("selected", C["sel"])],
                  foreground=[("selected", C["green"])])
        style.map("Dark.Treeview.Heading",
                  background=[("active", C["panel2"])])

        # Progress bar
        style.configure("Mint.Horizontal.TProgressbar",
                        troughcolor="#27363E", background=C["green"],
                        bordercolor="#27363E", lightcolor=C["green"],
                        darkcolor=C["green"], thickness=6)

        # Scrollbar
        style.configure("Vertical.TScrollbar", background=C["border"],
                        troughcolor=C["bg"], bordercolor=C["bg"],
                        arrowcolor=C["muted"], lightcolor=C["border"],
                        darkcolor=C["border"])
        style.map("Vertical.TScrollbar",
                  background=[("active", C["border2"])])

    def make_pill(self, parent, text, kind="green"):
        colors = {
            "green": (C["green_bg"], C["green_fg"]),
            "orange": ("#382C22", "#EAB082"),
            "red": ("#482830", "#FB929D"),
            "gray": (C["panel2"], C["muted"]),
        }
        bg, fg = colors[kind]
        return tk.Label(parent, text=text, bg=bg, fg=fg,
                        font=(self.mono, 8), padx=7, pady=3)

    def make_panel(self, parent, num, title, expand=False):
        """Dark bordered panel with a mono section number. Returns body."""
        outer = tk.Frame(parent, bg=C["panel"], highlightthickness=1,
                         highlightbackground=C["border"])
        outer.pack(fill="both" if expand else "x", expand=expand, pady=8)

        head = tk.Frame(outer, bg=C["panel"])
        head.pack(fill="x", padx=18, pady=14)

        tk.Label(head, text=num, bg=C["panel"], fg="#71808B",
                 font=(self.mono, 9)).pack(side="left")
        tk.Label(head, text=title, bg=C["panel"], fg=C["text"],
                 font=(self.sans, 11)).pack(side="left", padx=12)

        tk.Frame(outer, bg=C["border"], height=1).pack(fill="x")

        body = ttk.Frame(outer, style="Panel.TFrame", padding=18)
        body.pack(fill="both", expand=True)
        body.header = head
        return body

    def make_metric(self, parent, column, name, value, foot):
        card = tk.Frame(parent, bg=C["panel2"], highlightthickness=1,
                        highlightbackground=C["border"])
        card.grid(row=0, column=column, sticky="ew",
                  padx=(0 if column == 0 else 7, 0 if column == 3 else 7))

        tk.Label(card, text=name, bg=C["panel2"], fg="#96A6B3",
                 font=(self.mono, 8)).pack(anchor="w", padx=16, pady=(14, 0))

        value_label = tk.Label(card, text=value, bg=C["panel2"],
                               fg=C["text"], font=(self.num, 24))
        value_label.pack(anchor="w", padx=16, pady=(4, 0))

        tk.Label(card, text=foot, bg=C["panel2"], fg="#7E8F9C",
                 font=(self.sans, 8)).pack(anchor="w", padx=16, pady=(2, 14))
        return value_label

    # ---------- layout ----------

    def build_gui(self):
        # Left icon rail
        rail = tk.Frame(self.root, bg=C["rail"], width=64)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        tk.Frame(self.root, bg=C["border"], width=1).pack(side="left",
                                                          fill="y")

        tk.Label(rail, text="♪", bg=C["rail"], fg=C["green"],
                 font=(self.sans, 26, "bold")).pack(pady=(22, 18))
        tk.Label(rail, text="≡", bg=C["sel"], fg=C["green"],
                 font=(self.sans, 16), width=3, pady=5).pack()
        tk.Label(rail, text="ESD", bg=C["rail"], fg="#6B7B89",
                 font=(self.mono, 9)).pack(side="bottom", pady=22)

        right = tk.Frame(self.root, bg=C["bg"])
        right.pack(side="left", fill="both", expand=True)

        # Header
        header = tk.Frame(right, bg=C["bg"])
        header.pack(fill="x", padx=28, pady=(20, 12))

        tk.Label(header, text="ESD ESSENTIALS  ·  ESP32 + MICROSD + PYTHON",
                 bg=C["bg"], fg="#8797A5",
                 font=(self.sans, 8)).pack(anchor="w")

        title_row = tk.Frame(header, bg=C["bg"])
        title_row.pack(anchor="w", pady=(8, 0))
        tk.Label(title_row, text="MP3 Metadata Editor", bg=C["bg"],
                 fg=C["text"], font=(self.sans, 20, "bold")).pack(side="left")
        tk.Label(title_row, text="ID3v2.3", bg=C["bg"], fg="#A7B4BE",
                 font=(self.mono, 8), padx=7, pady=3,
                 highlightthickness=1,
                 highlightbackground="#34404A").pack(side="left", padx=16)

        tk.Frame(right, bg=C["border"], height=1).pack(fill="x")

        # Scrollable window: keeps all controls accessible on small screens.
        self.scroll_container = tk.Frame(right, bg=C["bg"])
        self.scroll_container.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(self.scroll_container, highlightthickness=0,
                                borderwidth=0, bg=C["bg"])
        self.vertical_scrollbar = ttk.Scrollbar(
            self.scroll_container, orient="vertical",
            command=self.canvas.yview
        )
        self.canvas.configure(yscrollcommand=self.vertical_scrollbar.set)

        self.vertical_scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.content_frame = tk.Frame(self.canvas, bg=C["bg"])
        self.content_frame.configure(padx=28, pady=10)
        self.canvas_window = self.canvas.create_window(
            (0, 0), window=self.content_frame, anchor="nw"
        )

        self.content_frame.bind(
            "<Configure>",
            lambda event: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")
            )
        )
        self.canvas.bind(
            "<Configure>",
            lambda event: self.canvas.itemconfigure(
                self.canvas_window, width=event.width
            )
        )

        self.root.bind_all("<MouseWheel>", self.on_mousewheel)

        # The frame is already embedded in the Canvas via create_window().
        # Do NOT pack/grid/place this frame again; doing so breaks scrolling.
        main = self.content_frame

        # Metric cards
        metrics = tk.Frame(main, bg=C["bg"])
        metrics.pack(fill="x", pady=(8, 4))
        for col in range(4):
            metrics.columnconfigure(col, weight=1, uniform="metric")

        self.make_metric(metrics, 0, "SERIAL PORT", PORT, "USB-UART to ESP32")
        self.make_metric(metrics, 1, "BAUD RATE", str(BAUD), "bits per second")
        self.make_metric(metrics, 2, "BLOCK SIZE", f"{BLOCK_SIZE} B",
                         "CRC32 per block")
        self.metric_files = self.make_metric(
            metrics, 3, "SD FILES", "—", "on microSD card"
        )

        # 01 Connection
        connection = self.make_panel(main, "01", "ESP32 Connection")

        ttk.Label(connection, text=f"Serial port: {PORT}   |   Baud: {BAUD}",
                  style="Muted.TLabel").pack(side="left")

        ttk.Button(connection, text="Connect ESP32",
                   style="Primary.TButton",
                   command=self.connect_clicked).pack(side="right")

        self.connection_status = ttk.Label(
            connection, text="●  NOT CONNECTED", style="Badge.TLabel"
        )
        self.connection_status.pack(side="right", padx=16)

        # 02 SD file manager
        sd_frame = self.make_panel(main, "02", "microSD File Manager",
                                   expand=True)

        controls = ttk.Frame(sd_frame, style="Panel.TFrame")
        controls.pack(fill="x", pady=(0, 12))

        ttk.Button(controls, text="List SD Files", style="Default.TButton",
                   command=self.list_clicked).pack(side="left")

        ttk.Button(controls, text="Transfer Selected MP3",
                   style="Primary.TButton",
                   command=self.transfer_clicked).pack(side="left", padx=10)

        tree_wrap = tk.Frame(sd_frame, bg=C["border"], padx=1, pady=1)
        tree_wrap.pack(fill="both", expand=True)

        self.file_list = ttk.Treeview(
            tree_wrap, columns=("size",), show="tree headings",
            height=4, selectmode="browse", style="Dark.Treeview"
        )
        self.file_list.heading("#0", text="FILENAME", anchor="w")
        self.file_list.heading("size", text="SIZE (BYTES)", anchor="e")
        self.file_list.column("#0", width=450)
        self.file_list.column("size", width=130, anchor="e")
        self.file_list.tag_configure("odd", background="#161E25")
        self.file_list.tag_configure("even", background=C["panel"])
        self.file_list.pack(fill="both", expand=True)

        # 03 Transfer progress
        progress_frame = self.make_panel(main, "03", "Transfer Progress")

        self.progress_label = ttk.Label(
            progress_frame, text="Ready to transfer", style="Panel.TLabel"
        )
        self.progress_label.pack(anchor="w", pady=(0, 10))

        self.progress_bar = ttk.Progressbar(
            progress_frame, orient="horizontal", mode="determinate",
            maximum=100, value=0, style="Mint.Horizontal.TProgressbar"
        )
        self.progress_bar.pack(fill="x")

        self.progress_details = ttk.Label(
            progress_frame, text="0 / 0 bytes", style="Muted.TLabel"
        )
        self.progress_details.pack(anchor="e", pady=(8, 0))

        # 04 Metadata editor
        metadata_frame = self.make_panel(main, "04", "MP3 Metadata Editor")

        self.file_label = ttk.Label(
            metadata_frame,
            text=f"Local file: {self.current_mp3.name}",
            style="Muted.TLabel"
        )
        self.file_label.grid(row=0, column=0, columnspan=2,
                             sticky="w", pady=(0, 12))

        for row, (label, _, _) in enumerate(TAG_FIELDS, start=1):
            ttk.Label(metadata_frame, text=label.upper(),
                      style="Field.TLabel").grid(
                row=row, column=0, sticky="w", padx=(0, 22), pady=6
            )

            entry = ttk.Entry(metadata_frame, style="Dark.TEntry",
                              font=(self.mono, 10))
            entry.grid(row=row, column=1, sticky="ew", pady=6)
            self.tag_entries[label] = entry

        metadata_frame.columnconfigure(1, weight=1)

        actions = ttk.Frame(metadata_frame, style="Panel.TFrame")
        actions.grid(row=len(TAG_FIELDS) + 1, column=0, columnspan=2,
                     sticky="w", pady=(16, 0))

        ttk.Button(actions, text="Load Metadata", style="Default.TButton",
                   command=self.load_clicked).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Save Metadata", style="Primary.TButton",
                   command=self.save_clicked).pack(side="left", padx=8)
        ttk.Button(actions, text="Verify Metadata", style="Default.TButton",
                   command=self.verify_clicked).pack(side="left", padx=8)

        file_actions = ttk.Frame(metadata_frame, style="Panel.TFrame")
        file_actions.grid(row=len(TAG_FIELDS) + 2, column=0, columnspan=2,
                          sticky="w", pady=(10, 0))

        ttk.Button(file_actions, text="▶  Play MP3",
                   style="Default.TButton",
                   command=self.play_clicked).pack(side="left", padx=(0, 8))
        ttk.Button(file_actions, text="Open File Properties (Details)",
                   style="Default.TButton",
                   command=self.properties_clicked).pack(side="left", padx=8)

        # Activity log
        log_frame = self.make_panel(main, "LOG", "Activity Log")

        self.log_box = tk.Text(
            log_frame, height=5, wrap="word", state="disabled",
            bg=C["log_bg"], fg="#8FA5B4", insertbackground=C["green"],
            relief="flat", padx=12, pady=10, font=(self.mono, 9),
            borderwidth=0, highlightthickness=1,
            highlightbackground=C["border"]
        )
        self.log_box.tag_configure("time", foreground="#5F7686")
        self.log_box.tag_configure("err", foreground=C["red"])
        self.log_box.pack(fill="both", expand=True)

        # Footer
        footer = tk.Frame(main, bg=C["bg"])
        footer.pack(fill="x", pady=(6, 14))
        tk.Label(footer, text="ESD ESSENTIALS  ·  ESP32 + microSD + Python",
                 bg=C["bg"], fg="#5F7686",
                 font=(self.mono, 8)).pack(side="left")
        tk.Label(footer, text="CRC32 VERIFIED TRANSFER", bg=C["bg"],
                 fg="#5F7686", font=(self.mono, 8)).pack(side="right")

        self.log("Application started.")
        self.log(f"Working folder: {WORK_DIR}")

        if self.current_mp3.is_file():
            self.log("Previously transferred MP3 found.")
            self.load_metadata_fields()

    def on_mousewheel(self, event):
        # Windows reports wheel movement in units of 120.
        if self.canvas.winfo_exists():
            units = -1 if event.delta > 0 else 1 if event.delta < 0 else 0
            if units:
                self.canvas.yview_scroll(units, "units")

    def log(self, message):
        stamp = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{stamp}] ", "time")
        self.log_box.insert(
            "end", message + "\n",
            "err" if message.startswith("ERROR") else ()
        )
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def process_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]

                if kind == "log":
                    self.log(event[1])

                elif kind == "connected":
                    self.connection_status.configure(
                        text="●  CONNECTED", foreground=C["green"]
                    )
                    self.log("ESP32 responded successfully to PING.")

                elif kind == "files":
                    self.file_list.delete(*self.file_list.get_children())
                    self.files.clear()

                    for index, (filename, size) in enumerate(event[1]):
                        self.files[filename] = size
                        self.file_list.insert(
                            "", "end", iid=filename,
                            text=f"  ♫  {filename}",
                            values=(f"{size:,}",),
                            tags=("odd" if index % 2 else "even",)
                        )

                    self.metric_files.configure(text=str(len(event[1])))
                    self.log(
                        f"Found {len(event[1])} file(s) on the SD card."
                    )

                elif kind == "progress":
                    received, total = event[1], event[2]
                    percentage = received * 100 / total if total else 0

                    self.progress_bar["value"] = percentage
                    self.progress_label.configure(
                        text=f"Transferring... {percentage:.1f}%"
                    )
                    self.progress_details.configure(
                        text=f"{received:,} / {total:,} bytes"
                    )

                elif kind == "transferred":
                    self.current_mp3 = event[1]
                    self.file_label.configure(
                        text=f"Local file: {self.current_mp3.name}"
                    )
                    self.transfer_active = False
                    self.progress_bar["value"] = 100
                    self.progress_label.configure(
                        text="Transfer complete — CRC32 verified"
                    )
                    self.log(f"Transfer completed: {self.current_mp3}")
                    self.load_metadata_fields()

                    messagebox.showinfo(
                        "Transfer Complete",
                        f"File transferred and CRC32 verified:\n"
                        f"{self.current_mp3}"
                    )

                elif kind == "error":
                    self.transfer_active = False
                    self.progress_label.configure(text="Operation failed")
                    self.log(f"ERROR: {event[1]}")
                    messagebox.showerror("Operation Failed", event[1])

        except queue.Empty:
            pass

        self.root.after(100, self.process_events)

    def run_background(self, task):
        def worker():
            try:
                task()
            except Exception as error:
                self.events.put(("error", str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def connect_clicked(self):
        def task():
            self.events.put(("log", f"Connecting to {PORT}..."))

            with connect_esp32():
                pass

            self.events.put(("connected",))

        self.run_background(task)

    def list_clicked(self):
        def task():
            self.events.put(("log", "Reading microSD directory..."))

            with connect_esp32() as ser:
                files = list_sd_files(ser)

            self.events.put(("files", files))

        self.run_background(task)

    def transfer_clicked(self):
        if self.transfer_active:
            messagebox.showinfo(
                "Transfer In Progress",
                "Please wait for the current transfer to finish."
            )
            return

        selection = self.file_list.selection()

        if not selection:
            messagebox.showwarning(
                "Select a File",
                "List the SD files and select an MP3 first."
            )
            return

        filename = selection[0]

        if not filename.lower().endswith(".mp3"):
            messagebox.showwarning(
                "Unsupported File",
                "Please select an MP3 file."
            )
            return

        output_path = WORK_DIR / f"{Path(filename).stem}_received.mp3"

        if output_path.exists():
            overwrite = messagebox.askyesno(
                "Replace File?",
                f"{output_path.name} already exists.\nReplace it?"
            )
            if not overwrite:
                return

        self.transfer_active = True
        self.progress_bar["value"] = 0
        self.progress_label.configure(text="Starting transfer...")
        self.progress_details.configure(text="0 / 0 bytes")

        def task():
            self.events.put(("log", f"Transferring {filename}..."))

            def report_progress(received, total):
                self.events.put(("progress", received, total))

            result = transfer_file(
                filename, output_path, report_progress
            )

            self.events.put(("transferred", result))

        self.run_background(task)

    def load_metadata_fields(self):
        if not self.current_mp3.is_file():
            self.log("No local MP3 file is available yet.")
            return

        values = read_metadata(self.current_mp3)

        for label, value in values.items():
            entry = self.tag_entries[label]
            entry.delete(0, "end")
            entry.insert(0, value)

        self.file_label.configure(
            text=f"Local file: {self.current_mp3.name}"
        )
        self.log(f"Loaded metadata from {self.current_mp3.name}.")

    def load_clicked(self):
        try:
            self.load_metadata_fields()
        except Exception as error:
            messagebox.showerror("Load Failed", str(error))

    def get_entry_values(self):
        return {
            label: entry.get()
            for label, entry in self.tag_entries.items()
        }

    def save_clicked(self):
        if not self.current_mp3.is_file():
            messagebox.showerror(
                "File Not Found",
                "Transfer an MP3 before editing metadata."
            )
            return

        try:
            save_metadata(
                self.current_mp3, self.get_entry_values()
            )
            self.log("Metadata saved and verified successfully.")

            messagebox.showinfo(
                "Success",
                "MP3 metadata saved and verified."
            )

        except Exception as error:
            messagebox.showerror("Save Failed", str(error))

    def verify_clicked(self):
        if not self.current_mp3.is_file():
            messagebox.showerror(
                "File Not Found",
                "Transfer an MP3 before verifying metadata."
            )
            return

        try:
            saved = read_metadata(self.current_mp3)
            expected = {
                label: value.strip()
                for label, value in self.get_entry_values().items()
            }

            for label in expected:
                if saved[label] != expected[label]:
                    raise RuntimeError(
                        f"{label} differs from the saved value.\n"
                        f"Editor: {expected[label]!r}\n"
                        f"File: {saved[label]!r}"
                    )

            self.log("All five metadata fields match the saved file.")

            messagebox.showinfo(
                "Verification Successful",
                "All five metadata fields match the saved file."
            )

        except Exception as error:
            self.log(f"Metadata verification failed: {error}")
            messagebox.showerror("Verification Failed", str(error))

    def play_clicked(self):
        try:
            play_mp3(self.current_mp3)
            self.log(f"Opened for playback: {self.current_mp3.name}")
        except Exception as error:
            messagebox.showerror("Playback Failed", str(error))

    def properties_clicked(self):
        try:
            open_file_properties(self.current_mp3, page="Details")
            self.log(
                f"Opened Properties > Details: {self.current_mp3.name}"
            )
        except Exception as error:
            messagebox.showerror("Properties Failed", str(error))


if __name__ == "__main__":
    root = tk.Tk()
    app = ESDApplication(root)
    root.mainloop()
