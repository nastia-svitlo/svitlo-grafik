#!/usr/bin/env python3
"""Круглий графік відключень для Telegram-каналу.

Бере графік погодинних відключень (ГПВ) із сайту Черкасиобленерго,
малює круглий «годинник» для однієї черги і публікує його в канал.
Якщо графік не змінився, нічого не публікує.
"""
import hashlib
import html
import io
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta

from PIL import Image, ImageDraw, ImageFont

try:
    from zoneinfo import ZoneInfo
    try:
        KYIV = ZoneInfo("Europe/Kyiv")
    except Exception:
        KYIV = ZoneInfo("Europe/Kiev")
except Exception:  # дуже старий Python
    KYIV = None

# ---------- налаштування (беруться з GitHub, тут лише запасні значення) ----------
QUEUE = os.environ.get("QUEUE", "2.1")
CHANNEL = os.environ.get("CHANNEL", "")
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
PLACE = os.environ.get("PLACE", "")  # підпис на картинці, напр. «Чалого, 106»
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

SITE = "https://www.cherkasyoblenergo.com"
NEWS_URL = SITE + "/media?lang=uk"
MAX_ARTICLES = 8

MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня",
          "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
WEEKDAYS = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]


def now_kyiv():
    return datetime.now(KYIV) if KYIV else datetime.utcnow() + timedelta(hours=3)


# ------------------------------- сайт -------------------------------
def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept-Language": "uk,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def to_text(page):
    page = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr|td)>", "\n", page)
    page = re.sub(r"<[^>]+>", " ", page)
    page = html.unescape(page).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", page)


def article_links(list_html):
    """Посилання на новини про ГПВ, від найновішої до старішої."""
    seen, out = set(), []
    for href in re.findall(r'href="([^"]+)"', list_html):
        href = html.unescape(href)
        if "/media/" not in href or "hpv" not in href.lower():
            continue
        url = urllib.parse.urljoin(SITE, href)
        key = url.split("?")[0]
        if key not in seen:
            seen.add(key)
            out.append(url)
    return out[:MAX_ARTICLES]


TITLE_RE = re.compile(
    r"рафік\w*\s+погодинних\s+відключень\s*\(ГПВ\)\s+на\s+(\d{1,2})\s+([а-яіїєґ']+)", re.I)
STAMP_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{1,2}:\d{2})")
RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})")


def parse_ranges(s):
    """'07:00 - 09:00, 23:00 - 00:00' -> [(420, 540), (1380, 1440)] у хвилинах."""
    out = []
    for h1, m1, h2, m2 in RANGE_RE.findall(s):
        a, b = int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)
        if b == 0 or b <= a:
            b = 1440
        a, b = max(0, min(a, 1440)), max(0, min(b, 1440))
        if b > a:
            out.append((a, b))
    out.sort()
    merged = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def parse_article(text, queue):
    """Повертає (день, місяць, інтервали, 'дд.мм гг:хв' публікації) або None."""
    t = TITLE_RE.search(text)
    if not t:
        return None
    day, mname = int(t.group(1)), t.group(2).lower()
    if mname not in MONTHS:
        return None
    month = MONTHS.index(mname) + 1
    # на сторінці має бути сам розклад (рядки черг), інакше це не той текст
    if not re.search(r"(?<![\d.])[1-6]\.[12]\s+\d{1,2}:\d{2}", text):
        return None
    q = re.search(r"(?<![\d.])" + re.escape(queue) +
                  r"\s+((?:\d{1,2}:\d{2}\s*[-–—]\s*\d{1,2}:\d{2}\s*[,;]?\s*)+)", text)
    ranges = parse_ranges(q.group(1)) if q else []
    s = STAMP_RE.search(text)
    stamp = (s.group(1)[:5] + " " + s.group(2)) if s else ""
    return day, month, ranges, stamp


def load_schedules(queue, dates):
    """{дата: (інтервали, штамп)} для потрібних дат; береться найновіша новина."""
    found = {}
    for url in article_links(fetch(NEWS_URL)):
        try:
            parsed = parse_article(to_text(fetch(url)), queue)
        except Exception as e:  # одна зламана новина не має зупиняти все
            print("  пропускаю", url, "-", e)
            continue
        if not parsed:
            continue
        day, month, ranges, stamp = parsed
        for d in dates:
            if (d.day, d.month) == (day, month) and d not in found:
                found[d] = (ranges, stamp)
                print("  знайдено графік на", d, "->", fmt_ranges(ranges) or "без відключень")
        if len(found) == len(dates):
            break
    return found


# ----------------------------- картинка -----------------------------
BG = (247, 245, 239)
INK = (31, 41, 51)
MUTED = (120, 128, 138)
GREEN = (126, 200, 133)
RED = (229, 72, 77)
PILL = (255, 214, 79)

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/dejavu/",
             "/usr/share/fonts/TTF/", "/Library/Fonts/", ""]


def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        try:
            return ImageFont.truetype(d + name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def fmt_time(m):
    return "%02d:%02d" % (m // 60, m % 60)


def fmt_ranges(ranges):
    return ", ".join("%s–%s" % (fmt_time(a), fmt_time(b)) for a, b in ranges)


def fmt_hours(minutes):
    h = minutes / 60
    return ("%d" % h if h == int(h) else ("%.1f" % h).replace(".", ",")) + " год"


def render(date, ranges, queue, stamp="", place=""):
    S = 2  # малюємо вдвічі більше і зменшуємо, щоб краї були гладкі
    W, H = 1080 * S, 1370 * S
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    def text(xy, s, f, fill=INK, anchor="la"):
        d.text(xy, s, font=f, fill=fill, anchor=anchor)

    # шапка
    text((70 * S, 78 * S), "Графік відключень", font(50 * S, True), anchor="lm")
    if place:
        text((70 * S, 132 * S), place, font(30 * S), MUTED, anchor="lm")
    pf = font(38 * S, True)
    label = "Черга " + queue
    pw = d.textlength(label, font=pf) + 56 * S
    d.rounded_rectangle((W - 70 * S - pw, 46 * S, W - 70 * S, 112 * S), 33 * S, fill=PILL)
    text((W - 70 * S - pw / 2, 79 * S), label, pf, anchor="mm")

    # кільце: 48 півгодинних шматочків, 00 зверху, за годинниковою стрілкою
    cx, cy = W // 2, 640 * S
    R_out, R_in = 400 * S, 285 * S
    off = [False] * 48
    for a, b in ranges:
        for i in range(48):
            if a < (i + 1) * 30 and b > i * 30:
                off[i] = True
    box = (cx - R_out, cy - R_out, cx + R_out, cy + R_out)
    for i in range(48):
        d.pieslice(box, -90 + i * 7.5, -90 + (i + 1) * 7.5 + 0.2, fill=RED if off[i] else GREEN)
    for h in range(24):  # проміжки між годинами
        ang = math.radians(-90 + h * 15)
        d.line((cx, cy, cx + (R_out + 4 * S) * math.cos(ang), cy + (R_out + 4 * S) * math.sin(ang)),
               fill=BG, width=7 * S)
    d.ellipse((cx - R_in, cy - R_in, cx + R_in, cy + R_in), fill=BG)

    # цифри годин
    hf = font(30 * S, True)
    for h in range(24):
        ang = math.radians(-90 + h * 15)
        text((cx + (R_out + 42 * S) * math.cos(ang), cy + (R_out + 42 * S) * math.sin(ang)),
             "%02d" % h, hf, MUTED, anchor="mm")

    # середина
    total = sum(b - a for a, b in ranges)
    text((cx, cy - 105 * S), WEEKDAYS[date.weekday()], font(40 * S), MUTED, anchor="mm")
    text((cx, cy - 20 * S), date.strftime("%d.%m"), font(118 * S, True), anchor="mm")
    if total:
        text((cx, cy + 82 * S), "без світла", font(34 * S), MUTED, anchor="mm")
        text((cx, cy + 138 * S), fmt_hours(total), font(52 * S, True), RED, anchor="mm")
    else:
        text((cx, cy + 82 * S), "відключень", font(34 * S), MUTED, anchor="mm")
        text((cx, cy + 132 * S), "немає", font(46 * S, True), (70, 150, 80), anchor="mm")

    # список інтервалів під кільцем (по 2 в рядку)
    lf = font(38 * S, True)
    y0 = 1160 * S
    if ranges:
        rows = [ranges[i:i + 2] for i in range(0, len(ranges), 2)]
        for r, row in enumerate(rows[:3]):
            cell = 440 * S
            x0 = cx - cell * len(row) / 2
            for c, (a, b) in enumerate(row):
                mx = x0 + cell * c + cell / 2
                s = "%s – %s" % (fmt_time(a), fmt_time(b))
                tw = d.textlength(s, font=lf)
                yy = y0 + r * 58 * S
                d.ellipse((mx - tw / 2 - 34 * S, yy - 10 * S, mx - tw / 2 - 14 * S, yy + 10 * S), fill=RED)
                text((mx + 12 * S, yy), s, lf, anchor="mm")
    else:
        text((cx, y0 + 20 * S), "Світло має бути весь день", font(36 * S), MUTED, anchor="mm")

    # підпис унизу
    foot = "Черкасиобленерго" + (" · опубліковано " + stamp if stamp else "")
    text((cx, H - 38 * S), foot, font(24 * S), MUTED, anchor="mm")

    return img.resize((W // S, H // S), Image.LANCZOS)


# ----------------------------- Telegram -----------------------------
def tg(method, fields, photo=None):
    url = "https://api.telegram.org/bot%s/%s" % (TOKEN, method)
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, k, v)).encode()
    if photo is not None:
        body += ("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"g.png\"\r\n"
                 "Content-Type: image/png\r\n\r\n" % boundary).encode() + photo + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"ok": False, "description": e.read().decode("utf-8", "replace")}


def caption(date, ranges, queue, today):
    when = "сьогодні" if date == today else "завтра"
    head = "🗓 <b>Графік на %s, %s (%s)</b>, черга %s" % (
        when, date.strftime("%d.%m"), WEEKDAYS[date.weekday()], queue)
    if not ranges:
        return head + "\n✅ Відключень не заплановано"
    lines = ["🔴 <b>%s – %s</b> (%s)" % (fmt_time(a), fmt_time(b), fmt_hours(b - a)) for a, b in ranges]
    return head + "\n" + "\n".join(lines)


# ------------------------------ запуск ------------------------------
def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        # приклад без інтернету: python bot.py --demo "07:00-09:00, 17:00-19:00"
        ranges = parse_ranges(sys.argv[2] if len(sys.argv) > 2 else "")
        render(now_kyiv().date(), ranges, QUEUE, now_kyiv().strftime("%d.%m %H:%M"), PLACE).save("demo.png")
        print("demo.png готово:", fmt_ranges(ranges))
        return

    if not TOKEN or not CHANNEL:
        sys.exit("Немає TELEGRAM_TOKEN або CHANNEL — перевірте налаштування на GitHub.")

    today = now_kyiv().date()
    dates = [today, today + timedelta(days=1)]
    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        state = {}

    print("Черга", QUEUE, "| шукаю графіки на", dates[0], "і", dates[1])
    found = load_schedules(QUEUE, dates)
    if not found:
        print("Графіків на ці дати на сайті немає — нічого не публікую.")

    for date in dates:
        if date not in found:
            continue
        ranges, stamp = found[date]
        key = date.isoformat()
        digest = hashlib.sha1(json.dumps(ranges).encode()).hexdigest()[:12]
        old = state.get(key, {})
        if old.get("hash") == digest:
            print(key, "- без змін")
            continue
        buf = io.BytesIO()
        render(date, ranges, QUEUE, stamp, PLACE).save(buf, "PNG")
        res = tg("sendPhoto", {"chat_id": CHANNEL, "parse_mode": "HTML",
                               "caption": caption(date, ranges, QUEUE, today)}, buf.getvalue())
        if not res.get("ok"):
            sys.exit("Telegram не прийняв картинку: %s" % res.get("description"))
        print(key, "- опубліковано")
        if old.get("message_id"):  # прибираємо застарілу картинку на цю ж дату
            tg("deleteMessage", {"chat_id": CHANNEL, "message_id": old["message_id"]})
        state[key] = {"hash": digest, "message_id": res["result"]["message_id"]}

    keep = {(today + timedelta(days=i)).isoformat() for i in (-1, 0, 1)}
    state = {k: v for k, v in state.items() if k in keep}
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
