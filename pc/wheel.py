#!/usr/bin/env python3
"""
Veer — приёмник для Linux.
Слушает UDP-пакеты от Android-приложения и создаёт виртуальный руль/геймпад
через uinput. Любая игра / Steam / Proton увидит обычное устройство ввода.

Протокол пакета (текст, одна строка):
    V1,<steer -1..1>,<gas 0..1>,<brake 0..1>,<btnA 0/1>,<btnB 0/1>

Ответ с силой виброотдачи на телефон:
    R1,<rumble 0..1>

Запуск:
    python wheel.py                  # порт 5555
    python wheel.py 6000             # другой порт
    python wheel.py -v               # подробный лог (каждый пакет)
    python wheel.py -d               # диагностика при старте + подробный лог
"""
import argparse
import ctypes
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


class Color:
    """ANSI-escape для цветного вывода в терминале."""
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    CYAN    = "\033[96m"
    MAGENTA = "\033[95m"
    RESET   = "\033[0m"
    BOLD    = "\033[1m"

    @staticmethod
    def supports_color():
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


C = Color


# Описание виртуального устройства: ось руля + аналоговые газ/тормоз + кнопки.
# Педали дублируются на两组 осей для максимальной совместимости:
#   ABS_GAS/ABS_BRAKE — Steam Input мапит их в триггеры Xbox-контроллера;
#   ABS_Z/ABS_RZ      — стандартные «колёсные» оси, их читают симрейсинги
#                       напрямую через evdev (когда Steam Input выключен).
# Руль дублируется на ABS_X (джойстик) и ABS_WHEEL (руль) — некоторые
# симрейсинги (например, Live for Speed, Richard Burns Rally) читают
# только ABS_WHEEL, а ABS_X игнорируют.
CAP = {
    e.EV_KEY: [e.BTN_A, e.BTN_B, e.BTN_TRIGGER, e.BTN_THUMB],
    e.EV_ABS: [
        (e.ABS_X,     AbsInfo(0, -32767, 32767, 0, 0, 0)),  # руль (джойстик)
        (e.ABS_WHEEL, AbsInfo(0, -32767, 32767, 0, 0, 0)),  # руль (wheel)
        (e.ABS_GAS,   AbsInfo(0, 0, 255, 0, 0, 0)),         # газ  (Steam trigger)
        (e.ABS_BRAKE, AbsInfo(0, 0, 255, 0, 0, 0)),         # тормоз (Steam trigger)
        (e.ABS_Z,     AbsInfo(0, 0, 255, 0, 0, 0)),         # газ  (wheel evdev)
        (e.ABS_RZ,    AbsInfo(0, 0, 255, 0, 0, 0)),         # тормоз (wheel evdev)
    ],
    e.EV_FF: [e.FF_RUMBLE],
}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _c(tag, text):
    """Оборачивает текст в ANSI-цвет, если терминал поддерживает."""
    if not C.supports_color():
        return text
    return f"{tag}{text}{C.RESET}"


def green(s):
    return _c(C.GREEN, s)


def cyan(s):
    return _c(C.CYAN, s)


def yellow(s):
    return _c(C.YELLOW, s)


def red(s):
    return _c(C.RED, s)


def magenta(s):
    return _c(C.MAGENTA, s)


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


def force_feedback_worker(ui, sock, state, state_lock, verbose=False):
    """Читает rumble от игры из uinput и отправляет силу вибрации телефону."""
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
            if verbose and (force or value > 0.01):
                client_ip = client[0]
                print(f"  {magenta('RUMBLE')}: {value:.2f} → {cyan(client_ip)}")
            last_sent = value
            last_keepalive = now
        except OSError:
            pass

    def current_strength():
        now = time.monotonic()
        expired = [effect_id for effect_id, until in active_until.items()
                   if until is not None and until <= now]
        for effect_id in expired:
            active_until.pop(effect_id, None)

        strength = 0.0
        for effect_id in active_until:
            effect = effects.get(effect_id)
            if effect is not None:
                strength = max(strength, effect["strength"])
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
                                if verbose:
                                    print(f"  {magenta('FF')}: загружен effect_id={effect.id} сила={strength:.2f} длина={effect.ff_replay.length}ms")
                            upload.retval = 0
                        finally:
                            ui.dll._uinput_end_upload(ui.fd, ctypes.byref(upload))

                    elif event.type == e.EV_UINPUT and event.code == e.UI_FF_ERASE:
                        erase = begin_erase(ui, event.value)
                        try:
                            effects.pop(erase.effect_id, None)
                            active_until.pop(erase.effect_id, None)
                            if verbose:
                                print(f"  {magenta('FF')}: стёрт effect_id={erase.effect_id}")
                            erase.retval = 0
                        finally:
                            ui.dll._uinput_end_erase(ui.fd, ctypes.byref(erase))

                    elif event.type == e.EV_FF:
                        effect = effects.get(event.code)
                        if event.value > 0 and effect is not None:
                            length = effect["length"]
                            active_until[event.code] = (
                                time.monotonic() + length / 1000.0 if length else None
                            )
                            if verbose:
                                print(f"  {magenta('FF')}: PLAY effect_id={event.code}")
                        else:
                            active_until.pop(event.code, None)
                            if verbose:
                                print(f"  {magenta('FF')}: STOP effect_id={event.code}")

            send_strength(current_strength())
        except OSError:
            time.sleep(0.05)


def discovery_responder(verbose=False):
    """Отвечает на broadcast телефона, чтобы он сам нашёл IP этого ПК."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", DISCOVERY_PORT))
    last_log = 0.0
    while True:
        data, addr = s.recvfrom(64)
        if data.strip() == b"VEER_DISCOVER":
            s.sendto(b"VEER_HERE", addr)
            now = time.monotonic()
            if verbose or (now - last_log > 5.0):
                tag = "DISCOVER"
                print(f"  {green(tag)}: запрос от {cyan(addr[0])} → ответил VEER_HERE")
                last_log = now


def _get_iface_ips():
    """Возвращает список (интерфейс, ip) для всех IPv4-интерфейсов кроме lo."""
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
    """Ищет /dev/input/eventX, соответствующий созданному uinput-устройству."""
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
    return "не найден"


def _check_firewall():
    """Подсказки по фаерволу для распространённых систем."""
    hints = []
    if shutil.which("ufw"):
        hints.append("sudo ufw allow 5555/udp  # данные")
        hints.append("sudo ufw allow 5556/udp  # авто-поиск")
    if shutil.which("firewall-cmd"):
        hints.append("sudo firewall-cmd --add-port=5555/udp --add-port=5556/udp")
    return hints


def run_debug_diagnostics(port):
    """Выводит диагностическую информацию при старте."""
    print(f"\n{cyan('=== Диагностика Veer ===')}\n")

    print(f"{C.BOLD}Сеть:{C.RESET}")
    ifaces = _get_iface_ips()
    if ifaces:
        for name, ip in ifaces:
            print(f"  {green(name)}: {cyan(ip)}")
    else:
        print(f"  {yellow('IPv4-интерфейсы не найдены')}")
    print(f"  Порт данных:     UDP :{port}")
    print(f"  Порт авто-поиска: UDP :{DISCOVERY_PORT}")

    print(f"\n{C.BOLD}Фаервол:{C.RESET}")
    hints = _check_firewall()
    if hints:
        for h in hints:
            print(f"  {yellow(h)}")
    else:
        print(f"  ufw/firewalld не обнаружены — проверь вручную")

    print(f"\n{C.BOLD}uinput:{C.RESET}")
    if os.path.exists("/dev/uinput"):
        print(f"  {green('/dev/uinput')} доступен")
    else:
        print(f"  {red('sudo modprobe uinput')}")

    print()


def main():
    parser = argparse.ArgumentParser(
        description="Veer — виртуальный руль для Linux (принимает UDP от телефона)")
    parser.add_argument("port", nargs="?", type=int, default=5555,
                        help="UDP порт для данных (по умолчанию 5555)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Подробный лог: каждый пакет, rumble, FF-события")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="Диагностика при старте + подробный лог")
    args = parser.parse_args()

    port = args.port
    verbose = args.verbose or args.debug

    if args.debug:
        run_debug_diagnostics(port)

    ui = UInput(CAP, name="Veer Android Wheel",
                vendor=0x1209, product=0x4711, version=1, max_effects=32)

    if args.debug:
        dev_path = _find_uinput_dev(ui)
        print(f"  Устройство: {cyan(dev_path)}")
        print(f"  Проверка:    {green(f'jstest {dev_path}')}  или  {green('ls /dev/input/by-id/')}")
        print()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))

    threading.Thread(target=discovery_responder, args=(verbose,), daemon=True).start()
    state = {"client": None}
    state_lock = threading.Lock()
    threading.Thread(target=force_feedback_worker,
                     args=(ui, sock, state, state_lock, verbose),
                     daemon=True).start()

    print(f"Слушаю UDP :{port} (авто-поиск на :{DISCOVERY_PORT})")
    print(f'Виртуальное устройство: {green("Veer Android Wheel")} (проверь: ls /dev/input/by-id/)')
    print("Ctrl+C для выхода.")
    if verbose:
        print(f"  ({green('verbose')}: показываю каждый пакет)")
    else:
        print(f"  (запусти с {green('-v')} чтобы видеть каждый пакет, с {green('-d')} для диагностики)")

    # Статистика.
    pkt_count = 0
    pkt_window_start = time.monotonic()
    last_rate_show = 0.0
    rate = 0.0
    last_client = None
    last_pkt_time = time.monotonic()
    warned_timeout = False

    try:
        while True:
            data, addr = sock.recvfrom(1024)
            try:
                line = data.decode().strip().splitlines()[-1]
                if not line.startswith("V1,"):
                    # Не наш протокол — логируем, чтобы рассинхрон версий
                    # приложения/приёмника не прятался в тишине.
                    print(f"  {yellow('??')} от {cyan(addr[0])}: {line[:60]!r} (не V1-протокол)")
                    continue
                fields = line.split(",")
                if len(fields) != 6:
                    hint = "устаревший APK?"
                    if len(fields) == 9 and ",000," in line:
                        hint = "баг локали (запятая вместо точки в дробях) — обнови APK"
                    print(f"  {yellow('??')} от {cyan(addr[0])}: {len(fields)} полей вместо 6 — "
                          f"{hint} {line[:60]!r}")
                    continue
                _, steer, gas, brake, a, b = fields
                steer, gas, brake = float(steer), float(gas), float(brake)
                a, b = int(a), int(b)
            except (ValueError, UnicodeDecodeError) as exc:
                print(f"  {yellow('??')} от {cyan(addr[0])}: ошибка разбора {exc}: {data[:60]!r}")
                continue

            now = time.monotonic()
            last_pkt_time = now
            warned_timeout = False

            with state_lock:
                prev = state.get("client")
                state["client"] = addr

            client_key = addr[0]

            # Клиент подключился / сменился — всегда показываем.
            if prev is None or prev[0] != client_key:
                if prev is None:
                    print(f"  {green('КЛИЕНТ')}: {cyan(client_key)} подключился")
                else:
                    print(f"  {yellow('КЛИЕНТ')}: сменился {cyan(prev[0])} → {cyan(client_key)}")
                last_client = client_key
                pkt_count = 0
                pkt_window_start = now
                last_rate_show = 0.0

            pkt_count += 1

            # Показываем статистику по времени (каждые ~0.5с) а не по количеству.
            if pkt_count >= 10 and now - last_rate_show >= 0.5:
                elapsed = now - pkt_window_start
                if elapsed > 0:
                    rate = pkt_count / elapsed
                print(f"  {cyan(f'{rate:5.0f} пак/с')} от {green(client_key)}", end="")
                if steer != 0 or gas > 0 or brake > 0:
                    parts = [f" руль:{steer:+.2f}"]
                    if gas > 0:
                        parts.append(f"газ:{gas:.0f}")
                    if brake > 0:
                        parts.append(f"торм:{brake:.0f}")
                    print("".join(parts), end="")
                print()
                pkt_count = 0
                pkt_window_start = now
                last_rate_show = now

            g = int(clamp(gas,   0.0, 1.0) * 255)
            b_ = int(clamp(brake, 0.0, 1.0) * 255)
            steer_int = int(clamp(steer, -1.0, 1.0) * 32767)
            ui.write(e.EV_ABS, e.ABS_X,     steer_int)  # руль (джойстик)
            ui.write(e.EV_ABS, e.ABS_WHEEL, steer_int)  # руль (wheel)
            ui.write(e.EV_ABS, e.ABS_GAS,   g)          # Steam trigger (RT)
            ui.write(e.EV_ABS, e.ABS_BRAKE, b_)         # Steam trigger (LT)
            ui.write(e.EV_ABS, e.ABS_Z,     g)          # wheel evdev throttle
            ui.write(e.EV_ABS, e.ABS_RZ,    b_)         # wheel evdev brake
            ui.write(e.EV_KEY, e.BTN_A, a)
            ui.write(e.EV_KEY, e.BTN_B, b)
            ui.syn()

            try:
                sock.sendto(b"A1\n", addr)
            except OSError:
                pass

            # Verbose: каждая строка с деталями пакета.
            if verbose:
                gas_str = f" газ:{gas:.0f}" if gas > 0 else "      "
                brake_str = f" торм:{brake:.0f}" if brake > 0 else "       "
                print(f"    руль:{steer:+.2f}{gas_str}{brake_str} ← {green(client_key)}")

    except KeyboardInterrupt:
        pass
    finally:
        if last_client is not None:
            print(f"  {yellow('КЛИЕНТ')}: {cyan(last_client)} отключился")
        ui.close()
        sock.close()
        print("\nВыход.")


if __name__ == "__main__":
    main()
