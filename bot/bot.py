#!/usr/bin/env python3
"""
Telegram-бот видеоотдела. Работает на сервере круглосуточно.

- Присылаешь клипы с телефона → бот монтирует ролик (automontage.py) и присылает готовый.
- Каждый день в заданное время бот сам делает ИИ-ролик (reels.py) по очереди тем.
- Под каждым роликом кнопки: опубликовать в Telegram-канал, в VK или удалить.

Настройки лежат в файле .env рядом с ботом (пример: .env.example).
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import sys
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from activity import Activity, serve_panel
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

BOT_DIR = Path(__file__).resolve().parent
ROOT = BOT_DIR.parent  # папка ai-video с reels.py и automontage.py


def load_env(path):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(BOT_DIR / ".env")
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("videobot")

TOKEN = os.environ["BOT_TOKEN"]
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Moscow"))
DAILY_TIMES = [t.strip() for t in os.environ.get("DAILY_TIMES", "09:00,13:00").split(",") if t.strip()]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()          # @mychannel или -100...
VK_TOKEN = os.environ.get("VK_USER_TOKEN", "").strip()          # токен пользователя с правами video, wall
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "").strip().lstrip("-")
LOCAL_API = os.environ.get("LOCAL_BOT_API", "").strip()         # http://127.0.0.1:8081 для файлов до 2 ГБ
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
WHISPER = os.environ.get("WHISPER_MODEL", "small")
MUSIC = os.environ.get("MUSIC_FILE", "").strip()

DATA.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA / "state.json"
work_lock = asyncio.Lock()
ACT = Activity(DATA / "activity.json", TZ)
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8080"))


# ---------------------------------------------------------------- состояние

def load_state():
    st = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    st.setdefault("owner", int(os.environ["OWNER_ID"]) if os.environ.get("OWNER_ID") else None)
    st.setdefault("auto", os.environ.get("AUTO_PUBLISH", "0") == "1")
    st.setdefault("topics", [])
    st.setdefault("used", [])
    st.setdefault("draft", {"clips": [], "code": None, "title": None})
    st.setdefault("pending", {})
    if not st["topics"] and not st["used"]:
        plan = ROOT / "plans" / "week1_ai_topics.txt"
        if plan.exists():
            st["topics"] = [t.strip() for t in plan.read_text(encoding="utf-8").splitlines()
                            if t.strip() and not t.strip().startswith("#")]
    return st


def save_state():
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


STATE = load_state()
STATE.setdefault("panel_key", secrets.token_urlsafe(12))
save_state()


def is_owner(update: Update):
    uid = update.effective_user.id if update.effective_user else None
    if STATE["owner"] is None and uid:
        STATE["owner"] = uid  # первый, кто написал боту, становится владельцем
        save_state()
    return uid == STATE["owner"]


# ---------------------------------------------------------------- запуск программ

async def run_tool(args, timeout=3600):
    """Запускает reels.py / automontage.py, по ходу сообщает панели, кто из помощников работает.

    Возвращает (путь к ролику, список предупреждений вроде «распознавание не удалось»).
    """
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, *args, cwd=str(ROOT), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    lines, warnings = [], []

    async def read():
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if line.startswith("@@") and "|" in line:
                agent, task = line[2:].split("|", 1)
                ACT.set_active(agent, task)
                continue
            lines.append(line)
            if line.strip().startswith("!"):
                warnings.append(line.strip().lstrip("! ").strip())
        await proc.wait()

    await asyncio.wait_for(read(), timeout)
    text = "\n".join(lines)
    log.info("%s\n%s", " ".join(map(str, args)), text[-3000:])
    m = re.findall(r"Готово: (.+?\.mp4)", text)
    if proc.returncode != 0 or not m:
        raise RuntimeError(text.strip().splitlines()[-1] if text.strip() else "ошибка без текста")
    p = Path(m[-1].strip())
    return (p if p.is_absolute() else (ROOT / p)), warnings


def post_text(video):
    p = video.with_name(video.stem + "_post.txt")
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def publish_buttons(pid):
    row = []
    if CHANNEL_ID:
        row.append(Btn("📢 Telegram", callback_data=f"pub:tg:{pid}"))
    if VK_TOKEN and VK_GROUP_ID:
        row.append(Btn("🟦 VK", callback_data=f"pub:vk:{pid}"))
    rows = []
    if len(row) == 2:
        rows.append([Btn("✅ Опубликовать везде", callback_data=f"pub:all:{pid}")])
    if row:
        rows.append(row)
    rows.append([Btn("🗑 Удалить", callback_data=f"del:{pid}")])
    return Kb(rows)


WARN_HINTS = {
    "распознавание": "речь не распознана, поэтому нет субтитров и заголовка",
    "озвучка": "голос не сгенерировался, ролик без озвучки",
    "картинка": "часть картинок не сгенерировалась, вместо них цветной фон",
    "ANTHROPIC_API_KEY": "нет ключа Claude, заголовок и пост не написаны",
}


def explain(warnings):
    out = []
    for w in warnings:
        hint = next((h for k, h in WARN_HINTS.items() if k in w), None)
        out.append(f"• {hint}\n  ({w[:200]})" if hint else f"• {w[:200]}")
    return "⚠️ Не всё получилось:\n" + "\n".join(dict.fromkeys(out)) if out else ""


async def deliver(app, video, title, auto=False, warnings=()):
    """Отправляет готовый ролик владельцу (и публикует сразу, если включён автопостинг)."""
    note = explain(warnings)
    if note:
        await app.bot.send_message(STATE["owner"], note)
    pid = uuid.uuid4().hex[:8]
    post = post_text(video)
    STATE["pending"][pid] = {"video": str(video), "post": post, "title": title}
    save_state()
    if auto:
        res = await publish(pid, "all")
        await app.bot.send_message(STATE["owner"], f"Опубликовал ИИ-ролик «{title}»: {res}")
        return
    with open(video, "rb") as f:
        await app.bot.send_video(STATE["owner"], f, caption=(post or title)[:1024],
                                 supports_streaming=True, reply_markup=publish_buttons(pid),
                                 read_timeout=300, write_timeout=300)


# ---------------------------------------------------------------- публикация

def vk_upload(video, post):
    """Загрузка ролика в сообщество VK с постом на стене (метод video.save)."""
    r = requests.post("https://api.vk.com/method/video.save", data={
        "access_token": VK_TOKEN, "v": "5.199", "group_id": VK_GROUP_ID,
        "name": (post.splitlines()[0] if post else "Видео")[:120],
        "description": post[:5000], "wallpost": 1}, timeout=60).json()
    if "error" in r:
        raise RuntimeError(r["error"].get("error_msg"))
    with open(video, "rb") as f:
        up = requests.post(r["response"]["upload_url"], files={"video_file": f}, timeout=900)
    up.raise_for_status()
    return f"https://vk.com/video-{VK_GROUP_ID}_{r['response']['video_id']}"


async def publish(pid, where):
    item = STATE["pending"].get(pid)
    if not item:
        return "ролик уже удалён"
    video, post, done = Path(item["video"]), item["post"], []
    ACT.job_start("Публикация", item.get("title") or video.stem)
    if where in ("tg", "all") and CHANNEL_ID:
        ACT.set_active("publisher", "выкладывает ролик в Telegram-канал")
        try:
            with open(video, "rb") as f:
                await APP.bot.send_video(CHANNEL_ID, f, caption=post[:1024], supports_streaming=True,
                                         read_timeout=300, write_timeout=300)
            done.append("Telegram ✅")
            ACT.published()
        except Exception as e:
            done.append(f"Telegram ❌ {e}")
    if where in ("vk", "all") and VK_TOKEN and VK_GROUP_ID:
        ACT.set_active("publisher", "загружает ролик в VK")
        try:
            url = await asyncio.to_thread(vk_upload, video, post)
            done.append(f"VK ✅ {url}")
            ACT.published()
        except Exception as e:
            done.append(f"VK ❌ {e}")
    if any("✅" in d for d in done):
        item["done"] = True
        save_state()
    ACT.job_end(ok=bool(done), note=", ".join(done))
    return ", ".join(done) or "нет настроенных площадок (CHANNEL_ID или VK в .env)"


# ---------------------------------------------------------------- ИИ-ролики по расписанию

def refill_topics():
    """Когда темы кончились, Claude придумывает 14 новых по брифу."""
    import anthropic
    ACT.set_active("ideas", "придумывает 14 новых тем по брифу")
    brand = (ROOT / "brand.md").read_text(encoding="utf-8")
    used = "\n".join(STATE["used"][-60:])
    msg = anthropic.Anthropic().messages.create(model=MODEL, max_tokens=2000, messages=[{
        "role": "user", "content":
        f"{brand}\n\nПридумай 14 новых тем для коротких роликов этого автора, которые делает ИИ "
        f"без съёмки (про Китай, карго, проверку товара, выбор товаров из брифа). Не повторяй:\n{used}\n\n"
        "Ответь только списком: одна тема на строку, без нумерации."}])
    text = "".join(b.text for b in msg.content if b.type == "text")
    topics = [re.sub(r"^[\d.\-\s]+", "", t).strip() for t in text.splitlines()]
    STATE["topics"].extend(t for t in topics if len(t) > 8)
    save_state()
    ACT.set_active("ideas", f"добавил {len(topics)} тем в очередь")


async def make_ai_reel(app, topic=None, auto=None):
    async with work_lock:
        if topic is None:
            if not STATE["topics"]:
                ACT.job_start("Новые темы", "очередь закончилась")
                await asyncio.to_thread(refill_topics)
                ACT.job_end()
            topic = STATE["topics"].pop(0)
            save_state()
        ACT.job_start("ИИ-ролик", topic)
        args = ["reels.py", "--topic", topic, "--out", str(DATA / "ai")]
        if MUSIC:
            args += ["--music", MUSIC]
        try:
            video, warnings = await run_tool(args)
        except Exception as e:
            ACT.job_end(ok=False, note=str(e)[:120])
            raise
        ACT.job_end(ok=True, video=True, note="предупреждений: %d" % len(warnings) if warnings else "")
    STATE["used"].append(topic)
    save_state()
    await deliver(app, video, topic, auto=STATE["auto"] if auto is None else auto, warnings=warnings)


async def daily_job(ctx: ContextTypes.DEFAULT_TYPE):
    if not STATE["owner"]:
        return
    try:
        await make_ai_reel(ctx.application)
    except Exception as e:
        log.exception("daily job")
        await ctx.bot.send_message(STATE["owner"], f"⚠️ ИИ-ролик по расписанию не получился: {e}")


# ---------------------------------------------------------------- команды

HELP = """Я видеоотдел 🎬

<b>Твои видео</b>: пришли один или несколько клипов. В подписи к видео (или отдельным сообщением) напиши кодовое слово, например <code>ПАНЕЛЬ</code>. Можно добавить заголовок через |: <code>ПАНЕЛЬ | Панели прямо с завода</code>. Потом нажми «Смонтировать».

<b>ИИ-ролики</b> выходят сами каждый день в {times} (МСК).
/ai тема — сделать ИИ-ролик сейчас
/next — сделать следующий ролик из очереди
/topics — очередь тем
/add тема — добавить тему в очередь
/auto — автопубликация без одобрения: {auto}
/reset — сбросить присланные клипы
/panel — панель: кто из помощников что делает"""


async def cmd_start(update: Update, ctx):
    if not is_owner(update):
        return
    await update.message.reply_html(HELP.format(times=", ".join(DAILY_TIMES),
                                                auto="вкл" if STATE["auto"] else "выкл"))


async def cmd_ai(update: Update, ctx):
    if not is_owner(update):
        return
    topic = " ".join(ctx.args).strip()
    if not topic:
        await update.message.reply_text("Напиши тему после команды: /ai Как работает карго")
        return
    await update.message.reply_text(f"Делаю ролик «{topic}», это займёт несколько минут...")
    asyncio.create_task(safe(update, make_ai_reel(ctx.application, topic, auto=False)))


async def cmd_next(update: Update, ctx):
    if not is_owner(update):
        return
    await update.message.reply_text("Делаю следующий ролик из очереди...")
    asyncio.create_task(safe(update, make_ai_reel(ctx.application, auto=False)))


async def cmd_topics(update: Update, ctx):
    if not is_owner(update):
        return
    t = STATE["topics"]
    body = "\n".join(f"{i + 1}. {x}" for i, x in enumerate(t[:20])) or "пусто, придумаю новые сам"
    await update.message.reply_text(f"Очередь тем ({len(t)}):\n{body}")


async def cmd_add(update: Update, ctx):
    if not is_owner(update):
        return
    topic = " ".join(ctx.args).strip()
    if topic:
        STATE["topics"].insert(0, topic)
        save_state()
        await update.message.reply_text(f"Добавил первой в очередь: {topic}")


async def cmd_auto(update: Update, ctx):
    if not is_owner(update):
        return
    STATE["auto"] = not STATE["auto"]
    save_state()
    await update.message.reply_text(
        "Автопубликация ИИ-роликов включена: буду публиковать сразу и присылать отчёт."
        if STATE["auto"] else "Автопубликация выключена: сначала присылаю ролик на одобрение.")


def public_host():
    if os.environ.get("PUBLIC_HOST"):
        return os.environ["PUBLIC_HOST"]
    for url in ("http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address",
                "https://api.ipify.org"):
        try:
            ip = requests.get(url, timeout=3).text.strip()
            if re.fullmatch(r"[\d.]+", ip):
                return ip
        except Exception:
            pass
    return "IP-сервера"


async def cmd_panel(update: Update, ctx):
    if not is_owner(update):
        return
    host = await asyncio.to_thread(public_host)
    url = f"http://{host}:{PANEL_PORT}/?key={STATE['panel_key']}"
    await update.message.reply_text(
        f"Панель видеоотдела (видно, кто из помощников что делает):\n{url}\n\n"
        "Ссылка личная, не пересылай её. Добавь страницу в закладки или на экран «Домой».",
        disable_web_page_preview=True)


def panel_extra():
    return {"topics": STATE["topics"][:10], "topics_count": len(STATE["topics"]),
            "pending": sum(1 for v in STATE["pending"].values() if not v.get("done")), "schedule": DAILY_TIMES, "auto": STATE["auto"]}


async def cmd_reset(update: Update, ctx):
    if not is_owner(update):
        return
    reset_draft()
    await update.message.reply_text("Клипы сброшены.")


def reset_draft():
    for c in STATE["draft"]["clips"]:
        Path(c).unlink(missing_ok=True)
    STATE["draft"] = {"clips": [], "code": None, "title": None}
    save_state()


async def safe(update, coro):
    try:
        await coro
    except Exception as e:
        log.exception("task")
        await update.effective_chat.send_message(f"⚠️ Не получилось: {e}")


# ---------------------------------------------------------------- клипы с телефона

def parse_caption(text):
    if not text:
        return None, None
    code, _, title = text.partition("|")
    code = code.strip().split()[0].upper().strip("«»\"'") if code.strip() else None
    return code, (title.strip() or None)


def draft_summary():
    d = STATE["draft"]
    return (f"Клипов: {len(d['clips'])}. Кодовое слово: {d['code'] or 'не указано'}."
            + (f" Заголовок: {d['title']}." if d["title"] else " Заголовок придумает ИИ."))


DRAFT_KB = Kb([[Btn("🎬 Смонтировать", callback_data="montage"),
                Btn("✖️ Сбросить", callback_data="reset")]])


async def on_clip(update: Update, ctx):
    if not is_owner(update):
        return
    msg = update.message
    media = msg.video or msg.document
    if not media:
        return
    try:
        f = await media.get_file(read_timeout=600)
    except Exception as e:
        await msg.reply_text(
            f"Не могу скачать файл ({e}). Обычный Telegram-бот принимает файлы до 20 МБ: "
            "отправь видео сжатым (как видео, не файлом) или включи LOCAL_BOT_API на сервере.")
        return
    inbox = DATA / "inbox"
    inbox.mkdir(exist_ok=True)
    path = inbox / f"{len(STATE['draft']['clips']) + 1:02d}_{uuid.uuid4().hex[:6]}.mp4"
    await f.download_to_drive(path, read_timeout=600)
    STATE["draft"]["clips"].append(str(path))
    code, title = parse_caption(msg.caption)
    if code:
        STATE["draft"]["code"] = code
    if title:
        STATE["draft"]["title"] = title
    save_state()
    # альбом приходит пачкой сообщений: отвечаем один раз, через пару секунд после последнего клипа
    for job in ctx.job_queue.get_jobs_by_name("draft"):
        job.schedule_removal()
    ctx.job_queue.run_once(draft_reply, 3, chat_id=msg.chat_id, name="draft")


async def draft_reply(ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_message(ctx.job.chat_id, draft_summary(), reply_markup=DRAFT_KB)


async def on_text(update: Update, ctx):
    if not is_owner(update):
        return
    if STATE["draft"]["clips"]:
        code, title = parse_caption(update.message.text)
        STATE["draft"]["code"] = code or STATE["draft"]["code"]
        STATE["draft"]["title"] = title or STATE["draft"]["title"]
        save_state()
        await update.message.reply_text(draft_summary(), reply_markup=DRAFT_KB)
    else:
        await update.message.reply_text("Пришли клипы для монтажа или напиши /start, чтобы увидеть команды.")


async def montage(update: Update, ctx):
    d = STATE["draft"]
    if not d["clips"]:
        await update.effective_chat.send_message("Сначала пришли клипы.")
        return
    clips, code, title = list(d["clips"]), d["code"], d["title"]
    STATE["draft"] = {"clips": [], "code": None, "title": None}
    save_state()
    wait = "" if not work_lock.locked() else " Сейчас занят другим роликом, твой следующий в очереди."
    await update.effective_chat.send_message(f"Монтирую {len(clips)} клип(а), пришлю готовый ролик.{wait}")
    name = dt.datetime.now(TZ).strftime("montage_%Y%m%d_%H%M%S")
    args = ["automontage.py", *clips, "--ai", "--whisper", WHISPER, "--out", str(DATA / "montage"),
            "--name", name]
    if code:
        args += ["--code", code]
    if title:
        args += ["--title", title]
    if MUSIC:
        args += ["--music", MUSIC]
    async with work_lock:
        ACT.job_start("Монтаж", title or code or f"{len(clips)} клип(а)")
        try:
            video, warnings = await run_tool(args)
        except Exception as e:
            ACT.job_end(ok=False, note=str(e)[:120])
            raise
        ACT.job_end(ok=True, video=True)
    for c in clips:
        Path(c).unlink(missing_ok=True)
    await deliver(ctx.application, video, title or "Твой ролик", warnings=warnings)


async def on_button(update: Update, ctx):
    q = update.callback_query
    if not is_owner(update):
        await q.answer()
        return
    await q.answer()
    data = q.data
    if data == "montage":
        await q.edit_message_reply_markup(None)
        asyncio.create_task(safe(update, montage(update, ctx)))
    elif data == "reset":
        reset_draft()
        await q.edit_message_text("Клипы сброшены.")
    elif data.startswith("pub:"):
        _, where, pid = data.split(":")
        await q.edit_message_reply_markup(None)
        res = await publish(pid, where)
        await q.message.reply_text(f"Публикация: {res}")
    elif data.startswith("del:"):
        pid = data.split(":")[1]
        item = STATE["pending"].pop(pid, None)
        save_state()
        if item:
            v = Path(item["video"])
            for p in (v, v.with_name(v.stem + "_post.txt"), v.with_suffix(".json")):
                p.unlink(missing_ok=True)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("Удалил.")


# ---------------------------------------------------------------- main

def build_app():
    b = Application.builder().token(TOKEN).read_timeout(60).write_timeout(300)
    if LOCAL_API:
        b = b.base_url(f"{LOCAL_API}/bot").base_file_url(f"{LOCAL_API}/file/bot").local_mode(True)
    return b.build()


APP = build_app()


def main():
    app = APP
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("ai", cmd_ai))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("auto", cmd_auto))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_clip))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    for t in DAILY_TIMES:
        h, m = map(int, t.split(":"))
        app.job_queue.run_daily(daily_job, dt.time(h, m, tzinfo=TZ), name=f"daily_{t}")
    serve_panel(ACT, STATE["panel_key"], PANEL_PORT, panel_extra)
    log.info("Бот запущен, ИИ-ролики в %s, панель на порту %s", DAILY_TIMES, PANEL_PORT)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
