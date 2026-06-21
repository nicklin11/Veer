#!/usr/bin/env python3
"""
Veer TUI — текстовый дашборд для виртуального руля.

Запуск:
    pc/veer-tui.py                  # TUI-режим (curses)
    pc/veer-tui.py --no-tui         # обычный лог, как wheel.py
    pc/veer-tui.py --port 6000      # другой порт
    pc/veer-tui.py --debug          # диагностика при старте

TUI показывает в реальном времени:
  - ASCII-руль с положением
  - Прогресс-бары газа/тормоза
  - Частоту пакетов, IP клиента
  - Rumble-индикатор
  - Путь uinput устройства
  - Сетевые интерфейсы
"""
import argparse
import ctypes
import curses
import fcntl
import os
import shutil
import socket
import struct
import sys
import threading
import time
import select

from evdev import UInput, AbsInfo, ecodes as e, ff

DISCOVERY_PORT = 5556

# ---------------------------------------------------------------------------
#  Shared utilities (copied from wheel.py)
# ---------------------------------------------------------------------------

class Color:
    RED     = "[91m"
    GREEN   = "[92m"
    YELLOW  = "[93m"
    CYAN    = "[96m"
    MAGENTA = "[95m"
    RESET   = "[0m"
    BOLD    = "[1m"

    @staticmethod
    def supports_color():
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

C = Color

CAP = {
    e.EV_KEY: [e.BTN_A, e.BTN_B, e.BTN_TRIGGER, e.BTN_THUMB],
    e.EV_ABS: [
        (e.ABS_WHEEL, AbsInfo(0, -32767, 32767, 0, 0, 0)),
        (e.ABS_Y,     AbsInfo(0, 0, 32767, 0, 0, 0)),
        (e.ABS_RZ,    AbsInfo(0, 0, 32767, 0, 0, 0)),
        (e.ABS_GAS,   AbsInfo(0, 0, 255, 0, 0, 0)),
        (e.ABS_BRAKE, AbsInfo(0, 0, 255, 0, 0, 0)),
        (e.ABS_Z,     AbsInfo(0, 0, 255, 0, 0, 0)),
    ],
    e.EV_FF: [e.FF_RUMBLE],
}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _c(tag, text):
    if not C.supports_color():
        return text
    return f"{tag}{text}{C.RESET}"


def green(s):   return _c(C.GREEN, s)
def cyan(s):    return _c(C.CYAN, s)
def yellow(s):  return _c(C.YELLOW, s)
def red(s):     return _c(C.RED, s)
def magenta(s): return _c(C.MAGENTA, s)


def rumble_strength(effect):
    rumble = effect.u.ff_rumble_effect
    strongest = max(rumble.strong_magnitude, rumble.weak_magnitude)
    return clamp(strongest / 65535.0, 0.0, 1.0)


def begin_upload(ui, request_id):
    upload = ff.UInputUpload()
    upload.request_id = request_id
    ret = ui.dll._uinput_begin_upload(ui.fd, ctypes.byref(upload))
    if ret:
        raise OSError(ret, "UI_BEGIN_FF_UPLOAD failed")
    return upload


def begin_erase(ui, request_id):
    erase = ff.UInputErase()
    erase.request_id = request_id
    ret = ui.dll._uinput_begin_erase(ui.fd, ctypes.byref(erase))
    if ret:
        raise OSError(ret, "UI_BEGIN_FF_ERASE failed")
    return erase


def force_feedback_worker(ui, sock, state, state_lock, tui=None):
    """Reads rumble from game via uinput, sends to phone."""
    effects = {}
    active_until = {}
    last_sent = None
    last_keepalive = 0.0

    def send_strength(value, force=False):
        nonlocal last_sent, last_keepalive
        now = time.monotonic()
        if not force and last_sent == value and now - last_keepalive < 0.1:
            return
        with state_lock:
            client = state.get("client")
        if client is None:
            return
        try:
            sock.sendto(f"R1,{value:.3f}\n".encode(), client)
            last_sent = value
            last_keepalive = now
        except OSError:
            pass

    def current_strength():
        now = time.monotonic()
        expired = [eid for eid, until in active_until.items()
                   if until is not None and until <= now]
        for eid in expired:
            active_until.pop(eid, None)
        strength = 0.0
        for eid in active_until:
            eff = effects.get(eid)
            if eff is not None:
                strength = max(strength, eff["strength"])
        return strength

    while True:
        try:
            ready, _, _ = select.select([ui.fd], [], [], 0.05)
            if ready:
                for event in ui.read():
                    if event.type == e.EV_UINPUT and event.code == e.UI_FF_UPLOAD:
                        upload = begin_upload(ui, event.value)
                        try:
                            effect = upload.effect
                            if effect.type == e.FF_RUMBLE:
                                strength = rumble_strength(effect)
                                effects[effect.id] = {
                                    "strength": strength,
                                    "length": effect.ff_replay.length,
                                }
                            upload.retval = 0
                        finally:
                            ui.dll._uinput_end_upload(ui.fd, ctypes.byref(upload))
                    elif event.type == e.EV_UINPUT and event.code == e.UI_FF_ERASE:
                        erase = begin_erase(ui, event.value)
                        try:
                            effects.pop(erase.effect_id, None)
                            active_until.pop(erase.effect_id, None)
                            erase.retval = 0
                        finally:
                            ui.dll._uinput_end_erase(ui.fd, ctypes.byref(erase))
                    elif event.type == e.EV_FF:
                        eff = effects.get(event.code)
                        if event.value > 0 and eff is not None:
                            length = eff["length"]
                            active_until[event.code] = (
                                time.monotonic() + length / 1000.0 if length else None
                            )
                        else:
                            active_until.pop(event.code, None)
            send_strength(current_strength())
        except OSError:
            time.sleep(0.05)


def discovery_responder(tui=None):
    """Responds to phone broadcasts so it finds this PC."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", DISCOVERY_PORT))
    while True:
        data, addr = s.recvfrom(64)
        if data.strip() == b"VEER_DISCOVER":
            s.sendto(b"VEER_HERE", addr)


def _get_iface_ips():
    """Returns list of (iface, ip) for all IPv4 interfaces except lo."""
    result = []
    SIOCGIFADDR = 0x8915
    for name in sorted(os.listdir("/sys/class/net/")):
        if name == "lo":
            continue
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ifreq = struct.pack("256s", name[:15].encode())
            res = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, ifreq)
            ip = socket.inet_ntoa(res[20:24])
            sock.close()
            result.append((name, ip))
        except OSError:
            pass
    return result


def _find_uinput_dev(ui):
    """Finds /dev/input/eventX matching our uinput device."""
    try:
        dev = ui.device
        if dev is not None:
            return dev.path
    except Exception:
        pass
    for base in sorted(os.listdir("/dev/input/")):
        if not base.startswith("event"):
            continue
        path = f"/dev/input/{base}"
        try:
            with open(path, "rb") as f:
                name_buf = fcntl.ioctl(f.fileno(), 0x81004506, bytes(256))
                name = name_buf.rstrip(b"\x00").decode(errors="replace")
                if name == "Veer Android Wheel":
                    return path
        except OSError:
            pass
    return "not found"


# ---------------------------------------------------------------------------
#  TUI — curses dashboard
# ---------------------------------------------------------------------------

class VeerTUI:
    """Curses TUI that displays wheel, pedals, stats in real time."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)   # status ok
        curses.init_pair(2, curses.COLOR_YELLOW, -1)  # warning
        curses.init_pair(3, curses.COLOR_RED, -1)     # error
        curses.init_pair(4, curses.COLOR_CYAN, -1)    # info
        curses.init_pair(5, curses.COLOR_MAGENTA, -1) # rumble
        self.max_y, self.max_x = stdscr.getmaxyx()

        # Shared state
        self.steer = 0.0
        self.gas = 0.0
        self.brake = 0.0
        self.pkt_rate = 0.0
        self.client_ip = "-"
        self.rumble = 0.0
        self.uinput_path = "-"
        self.ifaces = _get_iface_ips()
        self.connected = False
        self.pkt_count = 0
        self.log_lines = []

    def log(self, msg):
        self.log_lines.append(msg)
        if len(self.log_lines) > 50:
            self.log_lines.pop(0)

    def draw_progress_bar(self, y, x, width, fraction, color_pair=0):
        """Draws a horizontal progress bar at (y,x)."""
        fill = int(clamp(fraction, 0.0, 1.0) * (width - 2))
        bar = "[" + ("=" * fill) + (" " * (width - 2 - fill)) + "]"
        if color_pair:
            self.stdscr.attron(curses.color_pair(color_pair))
        self.stdscr.addstr(y, x, bar)
        if color_pair:
            self.stdscr.attroff(curses.color_pair(color_pair))

    def draw_wheel(self, y, x, width, steer):
        """Draws an ASCII steering wheel indicator."""
        # Wheel arc: 21 chars wide, steer maps to center offset
        half = (width - 2) // 2
        pos = -int(clamp(steer, -1.0, 1.0) * half)
        center = half
        idx = center + pos
        chars = [" "] * (width - 2)
        # Wheel rim
        for i in range(width - 2):
            if i == idx:
                chars[i] = "O"
            elif abs(i - center) <= 2:
                chars[i] = "-"
        bar = "(" + "".join(chars) + ")"
        self.stdscr.addstr(y, x, bar, curses.color_pair(4))
        # Label
        label = f"steer: {steer:+.2f}"
        self.stdscr.addstr(y + 1, x + (width - len(label)) // 2, label)

    def draw(self):
        """Redraws the entire TUI."""
        self.stdscr.erase()
        self.max_y, self.max_x = self.stdscr.getmaxyx()
        if self.max_y < 15 or self.max_x < 50:
            self.stdscr.addstr(0, 0, "Terminal too small (need >=50x15)")
            self.stdscr.refresh()
            return

        # ── Header ──
        title = "VEER — Android Wheel Receiver"
        self.stdscr.addstr(0, (self.max_x - len(title)) // 2, title,
                           curses.A_BOLD | curses.color_pair(4))

        # Connection status
        status_color = curses.color_pair(1) if self.connected else curses.color_pair(2)
        status_text = f"Connected: {self.client_ip}" if self.connected else "Waiting for phone..."
        self.stdscr.addstr(1, 2, status_text, status_color)

        # Packet rate
        rate_text = f"Packets/s: {self.pkt_rate:5.0f}"
        self.stdscr.addstr(1, self.max_x - len(rate_text) - 2, rate_text,
                           curses.color_pair(4))

        # ── Wheel ──
        wheel_width = min(40, self.max_x - 4)
        wheel_y = 3
        wheel_x = (self.max_x - wheel_width) // 2
        self.draw_wheel(wheel_y, wheel_x, wheel_width, self.steer)

        # ── Pedals ──
        bar_width = min(30, (self.max_x - 6) // 2)
        gas_y = 6
        brake_y = 6
        gas_x = 2
        brake_x = self.max_x - bar_width - 2

        self.stdscr.addstr(gas_y, gas_x, "GAS", curses.A_BOLD | curses.color_pair(1))
        self.draw_progress_bar(gas_y + 1, gas_x, bar_width, self.gas, 1)
        self.stdscr.addstr(gas_y + 2, gas_x, f"{self.gas*100:3.0f}%")

        self.stdscr.addstr(brake_y, brake_x, "BRAKE", curses.A_BOLD | curses.color_pair(3))
        self.draw_progress_bar(brake_y + 1, brake_x, bar_width, self.brake, 3)
        self.stdscr.addstr(brake_y + 2, brake_x, f"{self.brake*100:3.0f}%")

        # ── Rumble ──
        rumble_y = 9
        if self.rumble > 0.01:
            rumble_bar_width = min(30, self.max_x - 4)
            rumble_x = (self.max_x - rumble_bar_width) // 2
            self.stdscr.addstr(rumble_y, rumble_x - 4, "RUMBLE:",
                               curses.color_pair(5))
            self.draw_progress_bar(rumble_y, rumble_x + 3, rumble_bar_width,
                                   self.rumble, 5)
            self.stdscr.addstr(rumble_y, rumble_x + 3 + rumble_bar_width + 1,
                               f"{self.rumble*100:3.0f}%", curses.color_pair(5))

        # ── Info panel ──
        info_y = 11
        self.stdscr.addstr(info_y, 2, "─" * (self.max_x - 4),
                           curses.color_pair(4))
        info_y += 1
        self.stdscr.addstr(info_y, 2, f"uinput: {self.uinput_path}",
                           curses.color_pair(4))
        info_y += 1
        ifaces_text = " | ".join(f"{n}: {ip}" for n, ip in self.ifaces)
        self.stdscr.addstr(info_y, 2, f"Network: {ifaces_text}",
                           curses.color_pair(4))

        # ── Log lines ──
        log_y = info_y + 2
        for i, line in enumerate(self.log_lines[-5:]):
            if log_y + i < self.max_y - 1:
                self.stdscr.addstr(log_y + i, 2, line[:self.max_x - 4])

        # ── Footer ──
        self.stdscr.addstr(self.max_y - 1, 2,
                           "Ctrl+C to exit  |  Back = menu (on phone)",
                           curses.A_DIM)

        self.stdscr.refresh()


# ---------------------------------------------------------------------------
#  Main — TUI mode
# ---------------------------------------------------------------------------

def run_tui(port):
    """Entry point for curses TUI mode."""
    ui = UInput(CAP, name="Veer Android Wheel",
                vendor=0x1209, product=0x4711, version=1, max_effects=32)
    uinput_path = _find_uinput_dev(ui)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.05)

    threading.Thread(target=discovery_responder, daemon=True).start()
    state = {"client": None}
    state_lock = threading.Lock()
    threading.Thread(target=force_feedback_worker,
                     args=(ui, sock, state, state_lock),
                     daemon=True).start()

    def curses_main(stdscr):
        tui = VeerTUI(stdscr)
        tui.uinput_path = uinput_path

        pkt_count = 0
        pkt_window_start = time.monotonic()
        last_update = 0.0
        last_client = None

        while True:
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                # Still update TUI periodically
                now = time.monotonic()
                if now - last_update >= 0.1:
                    tui.draw()
                    last_update = now
                continue

            try:
                line = data.decode().strip().splitlines()[-1]
                if not line.startswith("V1,"):
                    continue
                fields = line.split(",")
                if len(fields) != 6:
                    continue
                _, steer, gas, brake, a, b = fields
                steer, gas, brake = float(steer), float(gas), float(brake)
                a, b = int(a), int(b)
            except (ValueError, UnicodeDecodeError):
                continue

            now = time.monotonic()
            with state_lock:
                prev = state.get("client")
                state["client"] = addr

            client_key = addr[0]
            if prev is None or prev[0] != client_key:
                tui.connected = True
                tui.client_ip = client_key
                last_client = client_key
                pkt_count = 0
                pkt_window_start = now

            pkt_count += 1

            # Update TUI state
            tui.steer = steer
            tui.gas = gas
            tui.brake = brake

            # Update rate and redraw ~10 fps
            if pkt_count >= 5 and now - last_update >= 0.1:
                elapsed = now - pkt_window_start
                if elapsed > 0:
                    tui.pkt_rate = pkt_count / elapsed
                tui.draw()
                pkt_count = 0
                pkt_window_start = now
                last_update = now

            # Write to uinput
            steer_int = int(clamp(steer, -1.0, 1.0) * 32767)
            g = int(clamp(gas, 0.0, 1.0) * 255)
            b_ = int(clamp(brake, 0.0, 1.0) * 255)
            gas_int = int((1.0 - clamp(gas, 0.0, 1.0)) * 32767)
            brake_int = int((1.0 - clamp(brake, 0.0, 1.0)) * 32767)
            ui.write(e.EV_ABS, e.ABS_WHEEL, steer_int)
            ui.write(e.EV_ABS, e.ABS_Y,     gas_int)
            ui.write(e.EV_ABS, e.ABS_RZ,    brake_int)
            ui.write(e.EV_ABS, e.ABS_GAS,   g)
            ui.write(e.EV_ABS, e.ABS_BRAKE, b_)
            ui.write(e.EV_ABS, e.ABS_Z,     g)
            ui.write(e.EV_KEY, e.BTN_A, a)
            ui.write(e.EV_KEY, e.BTN_B, b)
            ui.syn()

            try:
                sock.sendto(b"A1\n", addr)
            except OSError:
                pass

    try:
        curses.wrapper(curses_main)
    except KeyboardInterrupt:
        pass
    finally:
        ui.close()
        sock.close()


# ---------------------------------------------------------------------------
#  Main — no-TUI mode (same as wheel.py)
# ---------------------------------------------------------------------------

def run_notui(port, debug=False):
    """Non-TUI mode: behaves like wheel.py with log output."""
    if debug:
        print(f"\n{C.CYAN}=== Veer Diagnostics ==={C.RESET}\n")
        ifaces = _get_iface_ips()
        print(f"{C.BOLD}Network:{C.RESET}")
        for name, ip in ifaces:
            print(f"  {C.GREEN}{name}{C.RESET}: {C.CYAN}{ip}{C.RESET}")
        print(f"  Data port:     UDP :{port}")
        print(f"  Discovery port: UDP :{DISCOVERY_PORT}")
        print(f"\n{C.BOLD}uinput:{C.RESET}")
        if os.path.exists("/dev/uinput"):
            print(f"  {C.GREEN}/dev/uinput available{C.RESET}")
        else:
            print(f"  {C.RED}sudo modprobe uinput{C.RESET}")
        print()

    ui = UInput(CAP, name="Veer Android Wheel",
                vendor=0x1209, product=0x4711, version=1, max_effects=32)

    if debug:
        dev_path = _find_uinput_dev(ui)
        print(f"  Device: {C.CYAN}{dev_path}{C.RESET}")
        print()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))

    threading.Thread(target=discovery_responder, daemon=True).start()
    state = {"client": None}
    state_lock = threading.Lock()
    threading.Thread(target=force_feedback_worker,
                     args=(ui, sock, state, state_lock),
                     daemon=True).start()

    print(f"Listening UDP :{port} (discovery on :{DISCOVERY_PORT})")
    print(f"Virtual device: {C.GREEN}Veer Android Wheel{C.RESET}")
    print("Ctrl+C to exit.")

    pkt_count = 0
    pkt_window_start = time.monotonic()
    last_rate_show = 0.0
    rate = 0.0
    last_client = None
    warned_timeout = False

    try:
        while True:
            data, addr = sock.recvfrom(1024)
            try:
                line = data.decode().strip().splitlines()[-1]
                if not line.startswith("V1,"):
                    print(f"  {C.YELLOW}??{C.RESET} from {C.CYAN}{addr[0]}{C.RESET}: {line[:60]!r} (not V1)")
                    continue
                fields = line.split(",")
                if len(fields) != 6:
                    hint = "old APK?"
                    if len(fields) == 9 and ",000," in line:
                        hint = "locale bug (comma instead of dot) — update APK"
                    print(f"  {C.YELLOW}??{C.RESET} from {C.CYAN}{addr[0]}{C.RESET}: {len(fields)} fields instead of 6 — {hint}")
                    continue
                _, steer, gas, brake, a, b = fields
                steer, gas, brake = float(steer), float(gas), float(brake)
                a, b = int(a), int(b)
            except (ValueError, UnicodeDecodeError) as exc:
                print(f"  {C.YELLOW}??{C.RESET} from {C.CYAN}{addr[0]}{C.RESET}: parse error {exc}: {data[:60]!r}")
                continue

            now = time.monotonic()
            with state_lock:
                prev = state.get("client")
                state["client"] = addr

            client_key = addr[0]
            if prev is None or prev[0] != client_key:
                if prev is None:
                    print(f"  {C.GREEN}CLIENT{C.RESET}: {C.CYAN}{client_key}{C.RESET} connected")
                else:
                    print(f"  {C.YELLOW}CLIENT{C.RESET}: changed {C.CYAN}{prev[0]}{C.RESET} → {C.CYAN}{client_key}{C.RESET}")
                last_client = client_key
                pkt_count = 0
                pkt_window_start = now
                last_rate_show = 0.0

            pkt_count += 1
            if pkt_count >= 10 and now - last_rate_show >= 0.5:
                elapsed = now - pkt_window_start
                if elapsed > 0:
                    rate = pkt_count / elapsed
                parts = [f"  {C.CYAN}{rate:5.0f} pkt/s{C.RESET} from {C.GREEN}{client_key}{C.RESET}"]
                if steer != 0 or gas > 0 or brake > 0:
                    parts.append(f" wheel:{steer:+.2f}")
                    if gas > 0:
                        parts.append(f"gas:{gas:.0f}")
                    if brake > 0:
                        parts.append(f"brake:{brake:.0f}")
                print("".join(parts))
                pkt_count = 0
                pkt_window_start = now
                last_rate_show = now

            g = int(clamp(gas, 0.0, 1.0) * 255)
            b_ = int(clamp(brake, 0.0, 1.0) * 255)
            steer_int = int(clamp(steer, -1.0, 1.0) * 32767)
            gas_int = int((1.0 - clamp(gas, 0.0, 1.0)) * 32767)
            brake_int = int((1.0 - clamp(brake, 0.0, 1.0)) * 32767)
            ui.write(e.EV_ABS, e.ABS_WHEEL, steer_int)
            ui.write(e.EV_ABS, e.ABS_Y,     gas_int)
            ui.write(e.EV_ABS, e.ABS_RZ,    brake_int)
            ui.write(e.EV_ABS, e.ABS_GAS,   g)
            ui.write(e.EV_ABS, e.ABS_BRAKE, b_)
            ui.write(e.EV_ABS, e.ABS_Z,     g)
            ui.write(e.EV_KEY, e.BTN_A, a)
            ui.write(e.EV_KEY, e.BTN_B, b)
            ui.syn()

            try:
                sock.sendto(b"A1\n", addr)
            except OSError:
                pass

    except KeyboardInterrupt:
        pass
    finally:
        if last_client is not None:
            print(f"  {C.YELLOW}CLIENT{C.RESET}: {C.CYAN}{last_client}{C.RESET} disconnected")
        ui.close()
        sock.close()
        print("\nExit.")


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Veer TUI — virtual steering wheel receiver with dashboard")
    parser.add_argument("port", nargs="?", type=int, default=5555,
                        help="UDP data port (default 5555)")
    parser.add_argument("--no-tui", action="store_true",
                        help="Disable TUI, log to stdout like wheel.py")
    parser.add_argument("--debug", action="store_true",
                        help="Show diagnostics at startup")
    args = parser.parse_args()

    if args.no_tui:
        run_notui(args.port, debug=args.debug)
    else:
        run_tui(args.port)


if __name__ == "__main__":
    main()
