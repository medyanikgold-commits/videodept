#!/usr/bin/env python3
"""
Автомонтаж видео с телефона: склеивает клипы, вырезает паузы, делает вертикаль 1080x1920,
добавляет субтитры с подсветкой слов, заголовок-крючок, призыв с кодовым словом и музыку.

Примеры:
    python automontage.py clip1.mp4 clip2.mp4 --code ПАНЕЛЬ
    python automontage.py fabrika.mp4 --title "Солнечные панели в 2 раза дешевле" --code ПАНЕЛЬ --music bg.mp3
    python automontage.py fabrika.mp4 --ai --code СТАНЦИЯ        # заголовок и текст поста придумает Claude

Распознавание речи работает локально и бесплатно (faster-whisper), первый запуск скачает модель.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

W, H, FPS = 1080, 1920, 30
HERE = Path(__file__).resolve().parent
FONT_FILE = HERE / "fonts" / "DejaVuSans-Bold.ttf"
FONT_NAME = "DejaVu Sans"


def run(cmd, **kw):
    subprocess.run(cmd, check=True, **kw)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out)


def has_audio(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                          "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return bool(out)


# ---------------------------------------------------------------- 1. Вертикаль и склейка

def normalize(src, out):
    """Любой клип -> 1080x1920, 30 к/с. Горизонтальное видео кладётся на размытый фон."""
    vf = (f"[0:v]split[a][b];"
          f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:2[bg];"
          f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={FPS},format=yuv420p[v]")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
    if not has_audio(src):
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    cmd += ["-filter_complex", vf, "-map", "[v]", "-map", "0:a:0" if has_audio(src) else "1:a",
            "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", str(out)]
    run(cmd)


def join(clips, out, work):
    lst = work / "clips.txt"
    lst.write_text("".join(f"file '{Path(c).resolve().as_posix()}'\n" for c in clips),
                   encoding="utf-8")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(out)])


# ---------------------------------------------------------------- 2. Распознавание речи

WHISPER_CHILD = r"""
import json, sys
from faster_whisper import WhisperModel
model = WhisperModel(sys.argv[2], device="auto", compute_type="int8")
segments, _ = model.transcribe(sys.argv[1], language="ru", word_timestamps=True, vad_filter=True)
words = [{"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3)}
         for seg in segments for w in (seg.words or []) if w.word.strip()]
open(sys.argv[3], "w", encoding="utf-8").write(json.dumps(words, ensure_ascii=False))
"""


def transcribe(video, model_size, work):
    """Слова с таймкодами: [{"w": "слово", "s": 1.2, "e": 1.5}, ...].

    Распознавание идёт в отдельном процессе: если оно упадёт (нет модели, мало памяти),
    монтаж продолжится без субтитров, с нарезкой пауз по тишине.
    """
    wav, out = work / "audio16k.wav", work / "words_raw.json"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-ac", "1", "-ar", "16000",
         str(wav)])
    print(f"  распознаю речь (модель {model_size}, первый раз скачается)...")
    res = subprocess.run([sys.executable, "-c", WHISPER_CHILD, str(wav), model_size, str(out)],
                         capture_output=True, text=True)
    if res.returncode != 0 or not out.exists():
        last = (res.stderr.strip().splitlines() or ["неизвестная ошибка"])[-1]
        print(f"  ! распознавание не удалось ({last[:150]}): без субтитров, паузы режу по тишине")
        return None
    return json.loads(out.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 3. Вырезаем паузы

def speech_segments_from_words(words, total, max_gap, pad_in=0.12, pad_out=0.2):
    segs = []
    for wd in words:
        s, e = max(0, wd["s"] - pad_in), min(total, wd["e"] + pad_out)
        if segs and s - segs[-1][1] <= max_gap:
            segs[-1][1] = max(segs[-1][1], e)
        else:
            segs.append([s, e])
    return segs


def speech_segments_from_silence(video, total, max_gap):
    """Без распознавания: ищем тишину средствами ffmpeg и оставляем всё остальное."""
    err = subprocess.run(["ffmpeg", "-i", str(video), "-af",
                          f"silencedetect=noise=-35dB:d={max_gap}", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    segs, cur = [], 0.0
    for i, s in enumerate(starts):
        if s - cur > 0.05:
            segs.append([cur, s + 0.15])
        cur = max(0.0, ends[i] - 0.15) if i < len(ends) else total
    if total - cur > 0.05:
        segs.append([cur, total])
    return segs or [[0, total]]


def keep_silent_clips(segs, bounds):
    """Клип без речи (просто съёмка фабрики) не вырезается, а остаётся целиком."""
    segs = [list(x) for x in segs]
    for a, b in bounds:
        speech = sum(max(0, min(b, e) - max(a, s)) for s, e in segs)
        if speech < 0.3:
            segs.append([a, b])
    segs.sort()
    merged = []
    for s, e in segs:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def cut(video, segs, out):
    expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in segs)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
         "-vf", f"select='{expr}',setpts=N/FRAME_RATE/TB",
         "-af", f"aselect='{expr}',asetpts=N/SR/TB",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-c:a", "aac", "-b:a", "192k", str(out)])


def remap_words(words, segs):
    """Переносим таймкоды слов на новую (укороченную) шкалу времени."""
    res, offset = [], 0.0
    for a, b in segs:
        for wd in words:
            if wd["s"] >= a - 1e-3 and wd["e"] <= b + 1e-3:
                res.append({"w": wd["w"], "s": wd["s"] - a + offset, "e": wd["e"] - a + offset})
        offset += b - a
    return res


# ---------------------------------------------------------------- 4. Субтитры и надписи (ASS)

def ass_time(t):
    t = max(0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def ass_escape(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def chunk_words(words, max_words=3, max_chars=18):
    chunks, cur = [], []
    for wd in words:
        cur.append(wd)
        text = " ".join(x["w"] for x in cur)
        if len(cur) >= max_words or len(text) >= max_chars or re.search(r"[.!?,:;]$", wd["w"]):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words, total, title, cta, cta_sub, out):
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{FONT_NAME},84,&H0000DDFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,60,60,560,1
Style: Title,{FONT_NAME},86,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB0000000,-1,0,0,0,100,100,0,0,3,22,0,8,70,70,260,1
Style: Cta,{FONT_NAME},96,&H0000DDFF,&H00FFFFFF,&H00000000,&HC0000000,-1,0,0,0,100,100,0,0,3,26,0,5,70,70,0,1
Style: CtaSub,{FONT_NAME},58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,5,2,5,70,70,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    cta_start = max(0.0, total - 3.5) if cta else total
    for ch in chunk_words(words or []):
        s, e = ch[0]["s"], max(ch[-1]["e"], ch[0]["s"] + 0.4)
        if s >= cta_start:
            break
        e = min(e, cta_start)
        # караоке: каждое слово загорается жёлтым в момент произнесения
        parts = []
        for k, wd in enumerate(ch):
            nxt = ch[k + 1]["s"] if k + 1 < len(ch) else e
            cs = max(1, int(round((nxt - wd["s"]) * 100)))
            parts.append(f"{{\\k{cs}}}{ass_escape(wd['w'].upper())}")
        ev.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Sub,,0,0,0,,"
                  "{\\fad(60,0)}" + " ".join(parts))
    if title:
        ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(min(3.5, total))},Title,,0,0,0,,"
                  "{\\fad(150,250)}" + ass_escape(title.upper()))
    if cta:
        ev.append(f"Dialogue: 1,{ass_time(cta_start)},{ass_time(total)},Cta,,0,0,0,,"
                  "{\\fad(200,0)\\pos(540,880)}" + ass_escape(cta.upper()))
        if cta_sub:
            ev.append(f"Dialogue: 1,{ass_time(cta_start)},{ass_time(total)},CtaSub,,0,0,0,,"
                      "{\\fad(200,0)\\pos(540,1060)}" + ass_escape(cta_sub))
    out.write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 5. Помощь Claude (необязательно)

AI_PROMPT = """Вот расшифровка вертикального ролика автора (бриф ниже).
Придумай:
- hook: заголовок-крючок на первые 3 секунды, до 6 слов, без кавычек;
- post: текст под роликом для VK и Instagram: 2-3 предложения, призыв написать кодовое слово «{code}», 5-8 хэштегов.
Не выдумывай цены и факты, которых нет в расшифровке.
Ответь ТОЛЬКО JSON: {{"hook": "...", "post": "..."}}

Бриф:
{brand}

Расшифровка:
{text}"""


def ai_hook_and_post(words, code, model):
    import anthropic
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  ! нет ANTHROPIC_API_KEY, пропускаю заголовок и пост от ИИ")
        return {}
    brand = (HERE / "brand.md").read_text(encoding="utf-8") if (HERE / "brand.md").exists() else ""
    text = " ".join(w["w"] for w in words)
    msg = anthropic.Anthropic().messages.create(
        model=model, max_tokens=1500,
        messages=[{"role": "user", "content": AI_PROMPT.format(
            code=code or "ХОЧУ", brand=brand, text=text)}])
    raw = "".join(b.text for b in msg.content if b.type == "text")
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else {}


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Автомонтаж вертикальных роликов")
    ap.add_argument("clips", nargs="+", help="видео с телефона, в нужном порядке")
    ap.add_argument("--title", help="заголовок на первые 3 секунды")
    ap.add_argument("--code", help="кодовое слово для заказа, например ПАНЕЛЬ")
    ap.add_argument("--cta", help="свой текст призыва (по умолчанию «Пиши <код> в комментариях»)")
    ap.add_argument("--music", help="фоновая музыка mp3")
    ap.add_argument("--music-volume", type=float, default=0.10)
    ap.add_argument("--max-pause", type=float, default=0.45,
                    help="паузы длиннее этого (сек) вырезаются")
    ap.add_argument("--keep-pauses", action="store_true", help="не вырезать паузы")
    ap.add_argument("--no-subs", action="store_true", help="без субтитров")
    ap.add_argument("--whisper", default="small",
                    help="модель распознавания: tiny, base, small, medium (точнее, но медленнее)")
    ap.add_argument("--words", help="готовая расшифровка words.json (пропустить распознавание)")
    ap.add_argument("--ai", action="store_true", help="заголовок и текст поста придумает Claude")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--out", default="output")
    ap.add_argument("--name", help="имя файла результата")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = args.name or Path(args.clips[0]).stem + "_montage"
    work = out_dir / f"{name}_work"
    work.mkdir(exist_ok=True)

    print("1/5 Делаю вертикаль и склеиваю клипы...")
    norm = []
    for i, c in enumerate(args.clips):
        p = work / f"norm_{i}.mp4"
        normalize(c, p)
        norm.append(p)
    joined = work / "joined.mp4"
    join(norm, joined, work)
    total = duration(joined)
    bounds, t = [], 0.0
    for p in norm:
        d = duration(p)
        bounds.append((t, t + d))
        t += d

    print("2/5 Расшифровываю речь...")
    if args.words:
        words = json.loads(Path(args.words).read_text(encoding="utf-8"))
    elif args.no_subs and args.keep_pauses:
        words = None
    else:
        words = transcribe(joined, args.whisper, work)
    if words is not None:
        (out_dir / f"{name}_words.json").write_text(json.dumps(words, ensure_ascii=False),
                                                    encoding="utf-8")

    print("3/5 Вырезаю паузы...")
    if args.keep_pauses:
        segs = [[0, total]]
    elif words:
        segs = speech_segments_from_words(words, total, args.max_pause)
    else:
        segs = speech_segments_from_silence(joined, total, max(args.max_pause, 0.6))
    if not args.keep_pauses:
        segs = keep_silent_clips(segs, bounds)
    cutv = work / "cut.mp4"
    cut(joined, segs, cutv)
    new_total = duration(cutv)
    words2 = remap_words(words, segs) if words else []
    print(f"   было {total:.1f} с, стало {new_total:.1f} с")

    title, post = args.title, None
    if args.ai and words:
        print("   Claude придумывает заголовок и текст поста...")
        res = ai_hook_and_post(words2, args.code, args.model)
        title = title or res.get("hook")
        post = res.get("post")
    cta = args.cta or (f"Пиши «{args.code}» в комментариях" if args.code else None)
    cta_sub = "Предзаказ из Китая · проверю товар на фабрике" if cta else None

    print("4/5 Накладываю субтитры и надписи...")
    fonts = work / "fonts"
    fonts.mkdir(exist_ok=True)
    shutil.copy(FONT_FILE, fonts / FONT_FILE.name)
    build_ass([] if args.no_subs else words2, new_total, title, cta, cta_sub, work / "subs.ass")

    print("5/5 Свожу звук и сохраняю...")
    final = (out_dir / f"{name}.mp4").resolve()
    # ffmpeg запускается из рабочей папки, чтобы пути к субтитрам работали и в Windows
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", "cut.mp4"]
    if args.music:
        cmd += ["-stream_loop", "-1", "-i", str(Path(args.music).resolve()), "-filter_complex",
                f"[0:v]ass=subs.ass:fontsdir=fonts[v];"
                f"[1:a]volume={args.music_volume},"
                f"afade=t=out:st={max(0, new_total - 1.5):.2f}:d=1.5[m];"
                f"[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
                "-map", "[v]", "-map", "[a]"]
    else:
        cmd += ["-vf", "ass=subs.ass:fontsdir=fonts", "-map", "0:v", "-map", "0:a"]
    cmd += ["-t", f"{new_total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final)]
    run(cmd, cwd=work)
    if post:
        (out_dir / f"{name}_post.txt").write_text(post, encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    print(f"Готово: {final}  ({duration(final):.1f} с)")
    if post:
        print(f"Текст поста: {out_dir / (name + '_post.txt')}")


if __name__ == "__main__":
    main()
