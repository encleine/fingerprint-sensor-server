#!/usr/bin/env python3
"""
R307 fingerprint helper (auto-config, remembers settings)
- First run: asks for COM port and desired module baudrate, writes to settings.cfg,
  configures the module to that baud, and tells you to power-cycle. Next run uses it silently.
- Robust error handling, timeouts, retries.
- Optional flags if you ever want to change things: --reset-config, --set-baud, --port, --baud
"""

import argparse
import sys
import time
import struct
from pathlib import Path

# Third-party
try:
    import serial
    import serial.tools.list_ports
except Exception as e:
    print("❌ pyserial is required. Install with: pip install pyserial")
    raise

# Pillow is optional (for PNG). If missing, we'll save a PGM instead.
try:
    from PIL import Image  # pip install pillow

    PIL_OK = True
except Exception:
    PIL_OK = False

R307_ADDR = 0xFFFFFFFF  # default module address
START = 0xEF01

PID_COMMAND = 0x01
PID_DATA = 0x02
PID_ACK = 0x07
PID_END_DATA = 0x08

# Instruction codes (subset)
CMD_GENIMG = 0x01
CMD_UPIMAGE = 0x0A
CMD_IMG2TZ = 0x02
CMD_REGMODEL = 0x05
CMD_STORE = 0x06
CMD_WRITE_REG = 0x0E  # SetSysPara / Write System Parameter

REG_BAUD = 0x04  # baud rate control (value N => baud = 9600*N)

# Supported baud mapping (module-side)
BAUD_TO_N = {9600: 1, 19200: 2, 28800: 3, 38400: 4, 48000: 5, 57600: 6, 115200: 12}
SUPPORTED_BAUDS = sorted(BAUD_TO_N.keys())

CFG_FILE = Path("settings.cfg")
LOG_FILE = Path("r307_log.txt")


def log(msg: str):
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# -------------------- config helpers --------------------
def read_cfg():
    cfg = {}
    if not CFG_FILE.exists():
        return cfg
    try:
        for line in CFG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    except Exception as e:
        log(f"Failed reading {CFG_FILE}: {e}")
    return cfg


def write_cfg(cfg: dict):
    try:
        lines = [f"{k}={v}" for k, v in cfg.items()]
        CFG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        log(f"Failed writing {CFG_FILE}: {e}")


def reset_cfg():
    try:
        if CFG_FILE.exists():
            CFG_FILE.unlink()
            print(f"🧹 Removed {CFG_FILE}")
    except Exception as e:
        log(f"Failed to remove {CFG_FILE}: {e}")


# -------------------- serial helpers --------------------
def list_serial_ports():
    return [p.device for p in serial.tools.list_ports.comports()]


def choose_serial_port():
    ports = list_serial_ports()
    if not ports:
        print("❌ No serial ports found. Plug in your USB–TTL adapter then try again.")
        return None
    print("Available serial ports:")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p}")
    try:
        choice = input(f"Select port [0-{len(ports)-1}] (Enter for 0): ").strip()
    except EOFError:
        choice = ""
    idx = int(choice) if choice.isdigit() else 0
    if idx < 0 or idx >= len(ports):
        print("Invalid selection. Using first port.")
        idx = 0
    return ports[idx]


def prompt_baudrate():
    print("Choose desired module baudrate (will be saved and used next time):")
    print("Options:", ", ".join(str(b) for b in SUPPORTED_BAUDS))
    try:
        raw = input(f"Enter baud [{SUPPORTED_BAUDS[-2]}]: ").strip()  # default 57600
    except EOFError:
        raw = ""
    if raw == "":
        return SUPPORTED_BAUDS[-2]  # 57600 default
    try:
        val = int(raw)
    except ValueError:
        print("Invalid number. Using 57600.")
        return 57600
    if val not in SUPPORTED_BAUDS:
        print("Unsupported baud. Using 57600.")
        return 57600
    return val


# -------------------- driver --------------------
class R307:
    def __init__(self, port, baud=57600, timeout=2):
        try:
            self.ser = serial.Serial(port, baudrate=baud, timeout=timeout)
        except Exception as e:
            log(f"Serial open error on {port}: {e}")
            raise

    @staticmethod
    def _checksum(pid, content_bytes):
        length = len(content_bytes) + 2
        chk = pid + (length >> 8 & 0xFF) + (length & 0xFF) + sum(content_bytes)
        return chk & 0xFFFF

    def _write_packet(self, pid, content_bytes, addr=R307_ADDR):
        length = len(content_bytes) + 2
        pkt = struct.pack(">H", START)
        pkt += struct.pack(">I", addr)
        pkt += struct.pack("B", pid)
        pkt += struct.pack(">H", length)
        pkt += content_bytes
        pkt += struct.pack(">H", self._checksum(pid, content_bytes))
        self.ser.write(pkt)

    def _read_exact(self, n, overall_timeout=None):
        start_time = time.time()
        buf = b""
        while len(buf) < n:
            if (
                overall_timeout is not None
                and (time.time() - start_time) > overall_timeout
            ):
                raise TimeoutError(
                    f"Serial read exceeded overall timeout ({overall_timeout}s)"
                )
            chunk = self.ser.read(n - len(buf))
            if not chunk:
                continue
            buf += chunk
        return buf

    def _read_packet(self, overall_timeout=5):
        hdr = self._read_exact(2 + 4 + 1 + 2, overall_timeout=overall_timeout)
        (start,) = struct.unpack(">H", hdr[:2])
        if start != START:
            raise ValueError("Bad start code (expected 0xEF01)")
        pid = hdr[6]
        (length,) = struct.unpack(">H", hdr[7:9])
        if length < 2:
            raise ValueError("Invalid length in packet header")
        body = self._read_exact(length, overall_timeout=overall_timeout)
        content = body[:-2]
        (chk_rx,) = struct.unpack(">H", body[-2:])
        if self._checksum(pid, content) != chk_rx:
            raise ValueError("Checksum mismatch on received packet")
        return pid, content

    def _command(self, ins_byte, params=b"", ack_timeout=5):
        try:
            self._write_packet(PID_COMMAND, bytes([ins_byte]) + params)
            pid, content = self._read_packet(overall_timeout=ack_timeout)
        except TimeoutError as e:
            log(f"Timeout waiting for ACK to instruction 0x{ins_byte:02X}: {e}")
            raise
        if pid != PID_ACK:
            raise ValueError(f"Expected ACK, got PID 0x{pid:02X}")
        return content

    def gen_img(self):
        content = self._command(CMD_GENIMG)
        return content[0]

    def up_image(self, stream_timeout=10):
        content = self._command(CMD_UPIMAGE)
        conf = content[0]
        if conf != 0x00:
            raise RuntimeError(f"UpImage NACK: 0x{conf:02X}")
        image_bytes = bytearray()
        start_time = time.time()
        while True:
            if (time.time() - start_time) > stream_timeout:
                raise TimeoutError("Timed out while receiving image data")
            pid, content = self._read_packet(overall_timeout=stream_timeout)
            if pid in (PID_DATA, PID_END_DATA):
                image_bytes.extend(content)
                if pid == PID_END_DATA:
                    break
            else:
                raise ValueError(f"Unexpected PID during image upload: 0x{pid:02X}")
        return bytes(image_bytes)

    def write_reg(self, reg, value):
        content = self._command(CMD_WRITE_REG, bytes([reg, value]))
        conf = content[0]
        if conf != 0x00:
            raise RuntimeError(f"WRITE_REG failed: 0x{conf:02X}")
        return True

    def set_baudrate(self, baudrate):
        if baudrate not in BAUD_TO_N:
            raise ValueError(
                f"Unsupported baudrate. Choose one of: {', '.join(map(str, SUPPORTED_BAUDS))}"
            )
        N = BAUD_TO_N[baudrate]
        self.write_reg(REG_BAUD, N)
        return N

    @staticmethod
    def decode_uart_image_to_8bit(raw_bytes, width=256, height=288):
        n_pixels = width * height
        if len(raw_bytes) * 2 < n_pixels:
            raise ValueError("Not enough data for 256x288 image")
        out = bytearray(n_pixels)
        i = 0
        for b in raw_bytes:
            if i < n_pixels:
                out[i] = ((b >> 4) & 0x0F) * 17
                i += 1
            if i < n_pixels:
                out[i] = (b & 0x0F) * 17
                i += 1
        return bytes(out)


def save_image(bytes8, width=256, height=288, basename="fingerprint"):
    if PIL_OK:
        try:
            from PIL import Image

            img = Image.frombytes("L", (width, height), bytes8)
            out = Path(f"{basename}.png")
            img.save(out)
            print(f"✅ Saved image to {out.resolve()}")
            return out
        except Exception as e:
            log(f"Pillow save error: {e}")
            print("⚠️ Pillow save failed; falling back to PGM.")
    out = Path(f"{basename}.pgm")
    try:
        with out.open("wb") as f:
            header = f"P5\n{width} {height}\n255\n".encode("ascii")
            f.write(header)
            f.write(bytes8)
        print(f"✅ Saved image to {out.resolve()} (PGM)")
    except Exception as e:
        log(f"PGM save error: {e}")
        print(f"❌ Failed to save image: {e}")
    return out


# -------------------- CLI --------------------
def parse_args():
    ap = argparse.ArgumentParser(description="R307 fingerprint image capture")
    ap.add_argument(
        "--port", help="Serial port (e.g., COM7, /dev/ttyUSB0). Saves to settings.cfg"
    )
    ap.add_argument("--baud", type=int, help="Session baudrate (overrides saved)")
    ap.add_argument(
        "--reset-config",
        action="store_true",
        help="Forget saved settings and prompt again",
    )
    ap.add_argument(
        "--set-baud", type=int, help="Change module UART baud to this value and exit"
    )
    ap.add_argument(
        "--timeout", type=float, default=2.0, help="Serial read timeout (default 2)"
    )
    ap.add_argument(
        "--wait",
        type=float,
        default=15.0,
        help="Max seconds to wait for finger (default 15)",
    )
    ap.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Retries for UpImage on transient errors (default 2)",
    )
    return ap.parse_args()


def first_run_setup(cfg):
    # 1) Choose port if missing
    if "port" not in cfg or not cfg["port"]:
        port = choose_serial_port()
        if not port:
            sys.exit(1)
        cfg["port"] = port
        write_cfg(cfg)
        print(f"✅ Saved port to {CFG_FILE}: {port}")

    # 2) Ask desired module baud if missing
    if "baud" not in cfg or not cfg["baud"]:
        desired = prompt_baudrate()
        cfg["baud"] = str(desired)
        write_cfg(cfg)
        print(f"✅ Saved desired baud to {CFG_FILE}: {desired}")

        # Configure module now at its current baud (assume factory 57600)
        print("Configuring module baud...")
        try:
            sensor = R307(port=cfg["port"], baud=57600, timeout=2)
            sensor.set_baudrate(desired)
            print(f"✅ Module baud set to {desired}.")
            print("🔌 Please power-cycle (restart) the fingerprint sensor now.")
            print("ℹ️ Next run will use the saved baud automatically.")
        except Exception as e:
            log(f"First-run set baud error: {e}")
            print(f"❌ Failed to set module baud: {e}")
            print("You can retry later with:  python r307_capture.py --set-baud <rate>")
        # Exit after first-time config
        sys.exit(0)


def main():
    args = parse_args()

    if args.reset_config:
        reset_cfg()

    cfg = read_cfg()

    # Apply CLI overrides (also persist if provided)
    if args.port:
        cfg["port"] = args.port
    if args.baud:
        cfg["baud"] = str(args.baud)

    # If first time (missing port or baud), run setup (prompts once and exits after configuring baud)
    if "port" not in cfg or "baud" not in cfg or not cfg["port"] or not cfg["baud"]:
        first_run_setup(cfg)

    # Persist overrides or ensure file exists
    write_cfg(cfg)

    # Handle explicit --set-baud later runs
    if args.set_baud:
        try:
            sensor = R307(port=cfg["port"], baud=int(cfg["baud"]), timeout=args.timeout)
        except Exception as e:
            print(f"❌ Failed to open {cfg['port']} at {cfg['baud']}: {e}")
            sys.exit(1)
        try:
            sensor.set_baudrate(args.set_baud)
            cfg["baud"] = str(args.set_baud)
            write_cfg(cfg)
            print(f"✅ Module baud set to {args.set_baud}. Saved to {CFG_FILE}.")
            print(
                "🔌 Power-cycle the sensor, then run again (it will use the saved baud)."
            )
            sys.exit(0)
        except Exception as e:
            log(f"Set baud error: {e}")
            print(f"❌ Failed to set module baud: {e}")
            sys.exit(7)

    # Normal capture run using saved config
    port = cfg["port"]
    baud = int(cfg["baud"]) if "baud" in cfg else 57600
    print(f"Using port: {port}  |  baud: {baud}")
    try:
        sensor = R307(port=port, baud=baud, timeout=args.timeout)
    except Exception as e:
        print(f"❌ Failed to open {port} at {baud}: {e}")
        print("Tips:")
        print(" • Check Device Manager for the correct COM port.")
        print(" • Install the USB–TTL driver (CH340/CP2102/FTDI).")
        print(
            " • Ensure no other app is using the port (Arduino Serial Monitor, etc.)."
        )
        print(
            " • If you changed the module baud elsewhere, run:  python r307_capture.py --reset-config"
        )
        sys.exit(1)

    # Wait for finger
    print("Place finger on the sensor...")
    t0 = time.time()
    while True:
        try:
            rc = sensor.gen_img()
        except TimeoutError as e:
            log(f"GenImg timeout: {e}")
            rc = 0x02  # behave like "no finger"
        except Exception as e:
            log(f"GenImg error: {e}")
            print(f"❌ Error during capture: {e}")
            sys.exit(1)

        if rc == 0x00:
            print("Image captured.")
            break
        elif rc == 0x02:
            if (time.time() - t0) > args.wait:
                print("⌛ Timed out waiting for finger. Try again.")
                sys.exit(2)
            time.sleep(0.05)
            continue
        elif rc == 0x03:
            print("⚠️ Collecting image failed. Adjust finger placement and try again.")
        else:
            print(f"⚠️ GenImg returned code 0x{rc:02X}. Retrying...")

        if (time.time() - t0) > args.wait:
            print("⌛ Timed out waiting for finger. Try again.")
            sys.exit(2)
        time.sleep(0.1)

    # Download image with limited retries
    attempt = 0
    while True:
        try:
            print("Downloading image...")
            raw = sensor.up_image()
            break
        except TimeoutError as e:
            attempt += 1
            log(f"UpImage timeout (attempt {attempt}): {e}")
            if attempt > args.retries:
                print(
                    "❌ Timed out while receiving image data. Try again or lower baud rate."
                )
                sys.exit(3)
            print("⟳ Timeout; retrying...")
            time.sleep(0.2)
        except RuntimeError as e:
            log(f"UpImage runtime error: {e}")
            print(f"❌ Device declined image upload: {e}")
            sys.exit(4)
        except Exception as e:
            log(f"UpImage unexpected error: {e}")
            print(f"❌ Unexpected error during image download: {e}")
            sys.exit(5)

    try:
        pixels8 = R307.decode_uart_image_to_8bit(raw)
    except Exception as e:
        log(f"Decode error: {e}")
        print(f"❌ Failed to decode image data: {e}")
        raw_path = Path("fingerprint_uart.raw")
        try:
            raw_path.write_bytes(raw)
            print(f"📝 Saved raw UART bytes to {raw_path.resolve()} for analysis.")
        except Exception as e2:
            log(f"Failed to save raw: {e2}")
        sys.exit(6)

    # INFO: modified part of the script I got from the telegram group
    # removed save_image because I don't need it
    # my new function send the image byes to stdout right away to read in the go server
    send_image_to_stdout(pixels8)


# INFO:
# added by me to send the image bytes to stdout rather than saving it to a file
# then reading the file in the go server and sending it :p
def send_image_to_stdout(bytes8: bytes, width=256, height=288):
    if PIL_OK:
        try:
            from PIL import Image

            img = Image.frombytes("L", (width, height), bytes8)
            img.save(sys.stdout.buffer, "PNG", quality=100)
            img.close()

            return
        except Exception as e:
            log(f"Pillow save error: {e}")
            print("⚠️ Pillow save failed;")
