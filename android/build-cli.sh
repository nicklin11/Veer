#!/usr/bin/env bash
# Сборка APK без Android Studio, на твоём штатном Gradle + JDK.
# Требуется: Gradle >= 9.4.1 и JDK 17..26 (твои 9.5.1 + 26 подходят).
# Качает только command-line tools + минимальный SDK (~250 МБ) в ~/android-sdk.
# Запуск:  bash android/build-cli.sh
set -euo pipefail
cd "$(dirname "$0")"

SDK="${ANDROID_HOME:-$HOME/android-sdk}"
CT_VER="11076708"          # сборка command-line tools (если 404 — свежий номер: developer.android.com/studio#command-tools)
PLATFORM="android-36"
BUILD_TOOLS="36.0.0"

command -v java   >/dev/null || { echo "Нет java. Нужен JDK 17..26."; exit 1; }
command -v gradle >/dev/null || { echo "Нет gradle. Установи: sudo pacman -S gradle"; exit 1; }

# command-line tools (sdkmanager)
if [ ! -x "$SDK/cmdline-tools/latest/bin/sdkmanager" ]; then
  echo ">> Скачиваю Android command-line tools…"
  mkdir -p "$SDK/cmdline-tools"
  tmp="$(mktemp -d)"
  wget -q --show-progress -O "$tmp/ct.zip" \
    "https://dl.google.com/android/repository/commandlinetools-linux-${CT_VER}_latest.zip"
  unzip -q "$tmp/ct.zip" -d "$tmp"
  rm -rf "$SDK/cmdline-tools/latest"
  mv "$tmp/cmdline-tools" "$SDK/cmdline-tools/latest"
  rm -rf "$tmp"
fi

export ANDROID_HOME="$SDK"
export PATH="$SDK/cmdline-tools/latest/bin:$SDK/platform-tools:$PATH"

echo ">> Лицензии + минимальный SDK…"
yes | sdkmanager --licenses >/dev/null || true
sdkmanager "platform-tools" "platforms;${PLATFORM}" "build-tools;${BUILD_TOOLS}"

# где SDK
echo "sdk.dir=$SDK" > local.properties

echo ">> Сборка штатным gradle…"
gradle assembleDebug

APK="$(pwd)/app/build/outputs/apk/debug/app-debug.apk"
echo
echo "Готово. APK:"
echo "  $APK"
echo
echo "Поставить по USB (отладка включена):"
echo "  $SDK/platform-tools/adb install -r $APK"
echo "…или скинь APK на телефон и открой."
