#!/usr/bin/env bash
# Установка видеоотдела на чистый сервер Ubuntu (DigitalOcean и любой другой).
# Запуск:  bash install.sh     (из папки с кодом)  или с REPO_URL=... для скачивания с GitHub
set -euo pipefail
APP=/opt/videodept

if [ "$(id -u)" != "0" ]; then echo "Запусти от root: sudo bash install.sh"; exit 1; fi

echo "== 1/6 Ставлю ffmpeg и Python"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ffmpeg python3-venv python3-pip git fonts-dejavu-core >/dev/null

echo "== 2/6 Копирую программы в $APP"
if [ -n "${REPO_URL:-}" ]; then
  if [ -d "$APP/.git" ]; then git -C "$APP" pull -q; else git clone -q "$REPO_URL" "$APP"; fi
else
  SRC="$(cd "$(dirname "$0")/.." && pwd)"
  if [ "$SRC" != "$APP" ]; then mkdir -p "$APP"; cp -r "$SRC"/. "$APP"/; fi
fi
mkdir -p "$APP/fonts"
[ -f "$APP/fonts/DejaVuSans-Bold.ttf" ] || cp /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf "$APP/fonts/"

echo "== 3/6 Ставлю библиотеки (несколько минут)"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt" "python-telegram-bot[job-queue]>=21,<23"

echo "   скачиваю модель распознавания речи (для субтитров)"
"$APP/venv/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')" >/dev/null 2>&1 \
  && echo "   модель речи готова" || echo "   ВНИМАНИЕ: модель речи не скачалась, субтитров не будет. Пришли этот экран Claude."

echo "== 4/6 Проверяю память"
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$MEM_MB" -lt 3500 ] && ! swapon --show | grep -q swapfile; then
  echo "   памяти ${MEM_MB} МБ, добавляю 2 ГБ подкачки для распознавания речи"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== 5/6 Настройки"
ENV="$APP/bot/.env"
[ -f "$ENV" ] || cp "$APP/bot/.env.example" "$ENV"
ask() {  # ask KEY "вопрос"
  local cur; cur=$(grep -E "^$1=" "$ENV" | cut -d= -f2- || true)
  if [ -z "$cur" ]; then
    read -r -p "$2: " val < /dev/tty
    sed -i "s|^$1=.*|$1=$val|" "$ENV"
  fi
}
ask BOT_TOKEN "Токен нового бота от @BotFather"
ask OWNER_ID "Твой Telegram ID (от @userinfobot)"
ask ANTHROPIC_API_KEY "Ключ Claude API (sk-ant-...)"
chmod 600 "$ENV"

API_ID=$(grep -E '^TELEGRAM_API_ID=' "$ENV" | cut -d= -f2-)
API_HASH=$(grep -E '^TELEGRAM_API_HASH=' "$ENV" | cut -d= -f2-)
if [ -n "$API_ID" ] && [ -n "$API_HASH" ]; then
  echo "   включаю свой сервер Telegram API (видео до 2 ГБ)"
  command -v docker >/dev/null || apt-get install -y -qq docker.io >/dev/null
  docker rm -f tg-bot-api >/dev/null 2>&1 || true
  mkdir -p /var/lib/telegram-bot-api
  docker run -d --name tg-bot-api --restart always -p 127.0.0.1:8081:8081 \
    -v /var/lib/telegram-bot-api:/var/lib/telegram-bot-api \
    -e TELEGRAM_API_ID="$API_ID" -e TELEGRAM_API_HASH="$API_HASH" -e TELEGRAM_LOCAL=1 \
    aiogram/telegram-bot-api:latest >/dev/null
  sed -i "s|^LOCAL_BOT_API=.*|LOCAL_BOT_API=http://127.0.0.1:8081|" "$ENV"
  TOKEN=$(grep -E '^BOT_TOKEN=' "$ENV" | cut -d= -f2-)
  curl -s "https://api.telegram.org/bot$TOKEN/logOut" >/dev/null || true
fi

echo "== 6/6 Запускаю бота как службу (сам перезапускается и стартует после перезагрузки)"
cat > /etc/systemd/system/videobot.service <<UNIT
[Unit]
Description=Videodept Telegram bot
After=network-online.target

[Service]
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python $APP/bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now videobot >/dev/null
systemctl restart videobot
sleep 3
systemctl is-active --quiet videobot && echo "Готово! Напиши своему боту /start" \
  || { echo "Бот не запустился, вот лог:"; journalctl -u videobot -n 30 --no-pager; }
echo "Лог бота:  journalctl -u videobot -f      Настройки:  nano $ENV"
