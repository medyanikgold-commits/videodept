#!/usr/bin/env python3
"""
ИИ-отдел для рилс: тема -> сценарий -> озвучка -> картинки -> титры -> вертикальное видео 1080x1920.

Примеры:
    python reels.py --topic "5 фактов о кофе"                # всё сделает сам (нужен ANTHROPIC_API_KEY)
    python reels.py --script examples/coffee.json            # взять готовый сценарий
    python reels.py --topic "..." --music music.mp3          # с фоновой музыкой
    python reels.py --script my.json --images my_pics/       # свои картинки: 1.jpg, 2.jpg, ...

Каждый шаг можно заменить: свой сценарий (JSON), свои картинки (папка), своя музыка.
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import urllib.parse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1080, 1920, 30
HERE = Path(__file__).resolve().parent


def stage(agent, text):
    """Сообщает панели видеоотдела, какой помощник сейчас чем занят."""
    print(f"@@{agent}|{text}", flush=True)

# ---------------------------------------------------------------- 1. Сценарий

SCRIPT_PROMPT = """Ты сценарист вирусных вертикальных роликов (VK Клипы, Reels, Shorts).
{brand}
Напиши сценарий ролика на тему: «{topic}».
Длительность около {seconds} секунд, язык: русский, стиль: {style}.

Правила:
- Первая сцена это крючок: интрига или вопрос, который не даёт пролистать.
- Последняя сцена это призыв: подписаться, сохранить или написать комментарий
  (если в брифе есть свой призыв, используй его).
- Каждая сцена 1-2 коротких предложения для озвучки (10-25 слов).
- caption это 2-5 самых ярких слов сцены для крупной надписи на экране.
- image_prompt это описание картинки на английском для генератора изображений:
  конкретно, что в кадре, свет, стиль; вертикальный кадр; без текста на картинке.

Ответь ТОЛЬКО JSON без пояснений:
{{"title": "...", "post": "текст под роликом: 2-3 предложения, призыв и 5-8 хэштегов",
  "scenes": [{{"voice": "...", "caption": "...", "image_prompt": "..."}}]}}"""


def load_brand(path):
    if path and Path(path).exists():
        return "\nБриф автора, строго следуй ему:\n" + Path(path).read_text(encoding="utf-8") + "\n"
    return ""


def write_script(topic, seconds, style, model, brand=""):
    try:
        import anthropic
    except ImportError:
        sys.exit("Установите библиотеку: pip install anthropic")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Нужен ключ Claude API в переменной ANTHROPIC_API_KEY "
                 "(console.anthropic.com), либо передайте готовый сценарий через --script.")
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=4000,
        messages=[{"role": "user", "content": SCRIPT_PROMPT.format(
            topic=topic, seconds=seconds, style=style, brand=brand)}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        sys.exit("Модель вернула не JSON:\n" + text)
    return json.loads(m.group(0))


# ---------------------------------------------------------------- 2. Озвучка

async def _tts(text, voice, rate, out):
    import edge_tts
    await edge_tts.Communicate(text, voice, rate=rate).save(str(out))


def media_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True).stdout.strip()
    return float(out)


def voice_scene(text, voice, rate, out_mp3):
    """Озвучка бесплатным голосом Microsoft Edge. Без интернета: тишина по длине текста."""
    try:
        asyncio.run(_tts(text, voice, rate, out_mp3))
        if out_mp3.stat().st_size > 0:
            return media_duration(out_mp3) + 0.25
    except Exception as e:  # нет edge-tts или сети
        print(f"  ! озвучка недоступна ({type(e).__name__}), делаю ролик без голоса")
    dur = max(2.5, len(text.split()) / 2.6)  # ~2.6 слова в секунду
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=44100:cl=stereo", "-t", f"{dur:.2f}", str(out_mp3)],
                   check=True)
    return dur


# ---------------------------------------------------------------- 3. Картинки

PALETTES = [((255, 94, 98), (255, 195, 113)), ((67, 97, 238), (76, 201, 240)),
            ((114, 9, 183), (247, 37, 133)), ((6, 214, 160), (17, 138, 178)),
            ((255, 159, 28), (231, 29, 54)), ((58, 12, 163), (72, 149, 239))]


def placeholder_image(i, caption, out):
    """Запасной фон: градиент с мягкими кругами, если генератор картинок недоступен."""
    a, b = PALETTES[i % len(PALETTES)]
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        t = y / H
        c = tuple(int(a[k] * (1 - t) + b[k] * t) for k in range(3))
        for x in range(W):
            px[x, y] = c
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for k in range(6):
        r = 180 + 70 * ((i + k) % 4)
        cx, cy = (k * 397 + i * 131) % W, (k * 613 + i * 251) % H
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 40))
    img = Image.alpha_composite(img.convert("RGBA"), layer.filter(ImageFilter.GaussianBlur(30)))
    img.convert("RGB").save(out, quality=92)


def generate_image(prompt, out, i, caption, provider):
    """Генерация картинки. pollinations: бесплатно и без ключа. openai: нужен OPENAI_API_KEY."""
    import requests
    try:
        if provider == "pollinations":
            url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
                   + f"?width={W}&height={H}&nologo=true&seed={i + 1}")
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            out.write_bytes(r.content)
            Image.open(out).verify()
            return
        if provider == "openai":
            import base64
            r = requests.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                json={"model": "gpt-image-1", "prompt": prompt, "size": "1024x1536", "n": 1},
                timeout=300)
            r.raise_for_status()
            out.write_bytes(base64.b64decode(r.json()["data"][0]["b64_json"]))
            return
    except Exception as e:
        print(f"  ! картинка не сгенерировалась ({type(e).__name__}), ставлю фон")
    placeholder_image(i, caption, out)


def fit_cover(src, out):
    """Обрезать любую картинку под 1080x1920 с небольшим запасом для «наезда» камеры."""
    img = Image.open(src).convert("RGB")
    tw, th = int(W * 1.15), int(H * 1.15)
    scale = max(tw / img.width, th / img.height)
    img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1), Image.LANCZOS)
    left, top = (img.width - tw) // 2, (img.height - th) // 2
    img.crop((left, top, left + tw, top + th)).save(out, quality=92)


# ---------------------------------------------------------------- 4. Титры

def find_font(user_font=None):
    candidates = [user_font, HERE / "fonts" / "DejaVuSans-Bold.ttf",
                  "C:/Windows/Fonts/arialbd.ttf",
                  "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                  "/Library/Fonts/Arial Bold.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    sys.exit("Не найден шрифт, укажите его через --font путь/к/шрифту.ttf")


def text_png(text, font_path, size, out, y_center, color=(255, 255, 255),
             highlight=None, wrap=16, box=False):
    """Прозрачный PNG 1080x1920 с крупным текстом в обводке (стиль рилс)."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, size)
    lines = textwrap.wrap(text.upper(), width=wrap) or [""]
    lh = int(size * 1.18)
    y = y_center - lh * len(lines) // 2
    if box:
        widths = [d.textlength(l, font=font) for l in lines]
        bw = max(widths) + 70
        d.rounded_rectangle([(W - bw) / 2, y - 30, (W + bw) / 2, y + lh * len(lines) + 20],
                            radius=36, fill=(0, 0, 0, 150))
    for n, line in enumerate(lines):
        tw = d.textlength(line, font=font)
        fill = highlight if (highlight and n == 0) else color
        d.text(((W - tw) / 2, y + n * lh), line, font=font, fill=fill,
               stroke_width=max(4, size // 12), stroke_fill=(0, 0, 0))
    img.save(out)


def subtitle_chunks(text, dur, words_per_chunk=3):
    """Делит фразу на куски по 3 слова и раскладывает по времени пропорционально длине."""
    words = text.split()
    chunks = [" ".join(words[i:i + words_per_chunk]) for i in range(0, len(words), words_per_chunk)]
    total = sum(len(c) for c in chunks) or 1
    t, res = 0.0, []
    for c in chunks:
        d = dur * len(c) / total
        res.append((c, t, t + d))
        t += d
    return res


# ---------------------------------------------------------------- 5. Сборка

def render_scene(idx, scene, img, audio, dur, font, work, title=None):
    caption_png = work / f"cap_{idx}.png"
    text_png(scene["caption"], font, 104, caption_png, y_center=460,
             highlight=(255, 221, 0), wrap=14)
    subs = subtitle_chunks(scene["voice"], dur)
    sub_pngs = []
    for k, (txt, a, b) in enumerate(subs):
        p = work / f"sub_{idx}_{k}.png"
        text_png(txt, font, 72, p, y_center=1450, wrap=20, box=True)
        sub_pngs.append((p, a, b))

    frames = int(dur * FPS) + 1
    zoom_in = idx % 2 == 0  # чередуем наезд и отъезд камеры
    z = "min(1+0.0009*on,1.15)" if zoom_in else "max(1.15-0.0009*on,1)"
    inputs = ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(img), "-i", str(audio),
              "-loop", "1", "-t", f"{dur:.3f}", "-i", str(caption_png)]
    for p, _, _ in sub_pngs:
        inputs += ["-i", str(p)]
    fc = (f"[0:v]scale={int(W*1.15)}:{int(H*1.15)},zoompan=z='{z}':d={frames}:"
          f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},"
          f"eq=brightness=-0.06,format=yuv420p[bg];"
          f"[2:v]format=rgba,fade=in:st=0:d=0.3:alpha=1[cap];"
          f"[bg][cap]overlay=0:0[v0]")
    last = "v0"
    for k, (_, a, b) in enumerate(sub_pngs):
        fc += f";[{last}][{3 + k}:v]overlay=0:0:enable='between(t,{a:.2f},{b:.2f})'[v{k + 1}]"
        last = f"v{k + 1}"
    out = work / f"scene_{idx}.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", fc,
                    "-map", f"[{last}]", "-map", "1:a", "-t", f"{dur:.3f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", str(FPS),
                    "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", str(out)],
                   check=True)
    return out


def concat(scenes, music, out, work):
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in scenes))
    joined = work / "joined.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(joined)], check=True)
    if not music:
        shutil.copy(joined, out)
        return
    # фоновая музыка тише голоса, по кругу на всю длину, затухание в конце
    dur = media_duration(joined)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(joined),
                    "-stream_loop", "-1", "-i", str(music), "-filter_complex",
                    f"[1:a]volume=0.12,afade=t=out:st={max(0, dur - 1.5):.2f}:d=1.5[m];"
                    f"[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
                    "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
                    "-t", f"{dur:.3f}", str(out)], check=True)


# ---------------------------------------------------------------- main

def slug(s):
    return re.sub(r"[^\w]+", "_", s.lower()).strip("_")[:40] or "reel"


def make_reel(script, args, out_dir):
    name = slug(script.get("title") or "reel")
    work = out_dir / f"{name}_work"
    work.mkdir(exist_ok=True)
    (out_dir / f"{name}.json").write_text(json.dumps(script, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    if script.get("post"):
        (out_dir / f"{name}_post.txt").write_text(script["post"], encoding="utf-8")
    print(f"   «{script.get('title')}», сцен: {len(script['scenes'])}")

    font = find_font(args.font)
    user_imgs = sorted(Path(args.images).iterdir()) if args.images else []
    rendered = []
    for i, sc in enumerate(script["scenes"]):
        n = len(script["scenes"])
        print(f"Сцена {i + 1}/{n}: {sc['caption']}")
        stage("voice", f"озвучивает сцену {i + 1} из {n}")
        audio = work / f"voice_{i}.mp3"
        dur = voice_scene(sc["voice"], args.voice, args.rate, audio)
        raw = work / f"raw_{i}.jpg"
        stage("artist", f"рисует картинку к сцене {i + 1} из {n}: {sc['caption']}")
        if i < len(user_imgs):
            shutil.copy(user_imgs[i], raw)
        elif args.image_provider == "none":
            placeholder_image(i, sc["caption"], raw)
        else:
            generate_image(sc["image_prompt"], raw, i, sc["caption"], args.image_provider)
        img = work / f"img_{i}.jpg"
        fit_cover(raw, img)
        stage("editor", f"монтирует сцену {i + 1} из {n}")
        rendered.append(render_scene(i, sc, img, audio, dur, font, work))

    final = out_dir / f"{name}.mp4"
    print("Склеиваю ролик...")
    stage("editor", "склеивает ролик и сводит звук")
    concat(rendered, args.music, final, work)
    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    print(f"Готово: {final}  ({media_duration(final):.1f} с)")
    return final


def main():
    ap = argparse.ArgumentParser(description="ИИ-отдел: автоматическая сборка рилс")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--topic", help="тема ролика, сценарий напишет Claude")
    src.add_argument("--script", help="готовый сценарий JSON")
    src.add_argument("--batch", help="файл с темами, по одной на строку: сделает все ролики")
    ap.add_argument("--brand", default=str(HERE / "brand.md"), help="бриф бренда для сценариста")
    ap.add_argument("--seconds", type=int, default=35, help="желаемая длина ролика")
    ap.add_argument("--style", default="живой, разговорный")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--voice", default="ru-RU-DmitryNeural",
                    help="голос: ru-RU-DmitryNeural (муж.) или ru-RU-SvetlanaNeural (жен.)")
    ap.add_argument("--rate", default="+8%", help="скорость речи, например +10%%")
    ap.add_argument("--images", help="папка со своими картинками 1.jpg, 2.png, ...")
    ap.add_argument("--image-provider", default="pollinations",
                    choices=["pollinations", "openai", "none"])
    ap.add_argument("--music", help="фоновая музыка mp3")
    ap.add_argument("--font", help="свой шрифт .ttf")
    ap.add_argument("--out", default="output", help="папка для результатов")
    ap.add_argument("--keep-work", action="store_true", help="не удалять промежуточные файлы")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    brand = load_brand(args.brand)

    if args.script:
        make_reel(json.loads(Path(args.script).read_text(encoding="utf-8")), args, out_dir)
        return
    topics = [args.topic] if args.topic else [
        t.strip() for t in Path(args.batch).read_text(encoding="utf-8").splitlines()
        if t.strip() and not t.strip().startswith("#")]
    for n, topic in enumerate(topics, 1):
        print(f"\n=== Ролик {n}/{len(topics)}: {topic}\nПишу сценарий...")
        stage("writer", f"пишет сценарий «{topic}»")
        try:
            make_reel(write_script(topic, args.seconds, args.style, args.model, brand),
                      args, out_dir)
        except Exception as e:  # один сбойный ролик не останавливает всю пачку
            print(f"  ! ролик пропущен: {e}")


if __name__ == "__main__":
    main()
