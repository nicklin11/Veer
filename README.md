# Veer — телефон как руль для гонок на Linux

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Android-приложение превращает смартфон в **беспроводной руль** для гоночных игр на Linux.
Наклон телефона = поворот руля, тач-педали = газ/тормоз. Всё по Wi-Fi, без проводов.

```
[ Телефон: Veer ] --UDP/Wi-Fi--> [ Linux: wheel.py ] --uinput--> [ Игра/Steam ]
```

## Возможности

- 🎮 **Виртуальный руль** — наклон телефона (акселерометр), 270° полного хода
- 🦶 **Аналоговые педали** — газ и тормоз (сила нажатия)
- 📶 **Wi-Fi** — UDP ~100 Гц, задержка 2–10 мс
- 🔍 **Авто-поиск ПК** — broadcast по сети, не надо вводить IP
- 🌀 **Виброотдача** — игра шлёт rumble → вибрация телефона
- 🎯 **Совместимость со Steam Input** — триггеры газ/тормоз работают сразу
- 🕶️ **VR-ready** — кнопок на главном экране нет, меню через кнопку «Назад»
- 🖥️ **TUI-дашборд** — `pc/veer-tui.py` с实时-отображением руля, педалей и статистики

## Быстрый старт

### 1. ПК — приёмник

```bash
# Зависимости
pip install -r pc/requirements.txt
sudo modprobe uinput

# Права на /dev/uinput (один раз)
echo 'KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo usermod -aG input $USER
# перелогиниться

# Запуск (выбери один)
python pc/wheel.py              # обычный лог
python pc/veer-tui.py            # TUI-дашборд (curses)
python pc/veer-tui.py --no-tui   # TUI-скрипт в режиме лога
```

Если есть фаервол — открой UDP 5555–5556:
```bash
sudo ufw allow 5555/udp
sudo ufw allow 5556/udp
```

### 2. Телефон — установка APK

Собери APK (см. ниже) или скажи `gradle assembleDebug`:

```bash
cd android
gradle assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

### 3. Игра

1. Запусти `python pc/wheel.py` на ПК
2. Открой Veer на телефоне — нажми кнопку «Назад» → **Подключение** → **Найти ПК**
3. Нажми **Центр** (калибровка нуля)
4. Наклоняй телефон — руль крутится

> В Steam: если руль не работает в игре — **отключи Steam Input** для этой игры
> (Свойства → Контроллер → Отключить Steam Input).

## Сборка Android-приложения

### Вариант A — CLI (рекомендуется, ~250 МБ)

```bash
sudo pacman -S --needed gradle unzip wget
bash android/build-cli.sh   # скачает SDK и соберёт APK
```

### Вариант B — Android Studio

`File → Open → android/` → Run ▶.

APK: `android/app/build/outputs/apk/debug/app-debug.apk`

## Управление

| Действие | Как |
|----------|-----|
| **Руль** | Наклон телефона влево/вправо (горизонтально) |
| **Газ** | Нажатие на правую педаль (сильнее = больше) |
| **Тормоз** | Нажатие на левую педаль |
| **Калибровка центра** | Кнопка «Назад» → Центр |
| **Меню** | Кнопка «Назад» на телефоне |
| **Подключение** | Кнопка «Назад» → Подключение |
| **Выход** | Кнопка «Назад» → Выход |

## Структура проекта

```
Veer/
├── pc/
│   ├── wheel.py              # Linux-приёмник (виртуальный руль)
│   ├── veer-tui.py           # TUI-дашборд (curses)
│   └── requirements.txt
├── android/
│   ├── app/src/main/         # Kotlin-приложение
│   │   ├── java/com/veer/wheel/
│   │   │   ├── MainActivity.kt   # главный экран + сенсоры + UDP
│   │   │   ├── WheelView.kt      # отрисовка руля (Canvas)
│   │   │   └── PedalView.kt      # отрисовка педалей
│   │   └── res/layout/
│   │       ├── activity_main.xml
│   │       └── dialog_connect.xml
│   ├── build.gradle.kts
│   └── build-cli.sh
├── LICENSE
└── README.md
```

## Протокол

UDP, текст, одна строка:

```
V1,<steer -1..1>,<gas 0..1>,<brake 0..1>,<btnA 0/1>,<btnB 0/1>
```

Ответ с виброотдачей: `R1,<rumble 0..1>`

## Лицензия

MIT — делай что хочешь.
