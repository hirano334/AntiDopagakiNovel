#!/bin/sh
# USB でつないだ Android から http://localhost:8000 でアプリを開けるようにする。
# Service Worker は localhost なら HTTP でも動くので、一度開いてホーム画面に追加すれば
# 以後は USB を外しても完全にオフラインで使える。本やアプリを更新したときだけ、これを再実行して開き直す。
cd "$(dirname "$0")"
PORT=8000
# WSL では USB 機器が見えないので Windows 側の adb.exe を使う
ADB=$(command -v adb || command -v adb.exe) || { echo "adb が見つからない"; exit 1; }
"$ADB" reverse tcp:$PORT tcp:$PORT || exit 1
echo "スマホの Chrome で http://localhost:$PORT を開く（Ctrl+C で終了）"
exec python3 -m http.server $PORT -d app
