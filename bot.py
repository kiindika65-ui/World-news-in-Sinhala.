import asyncio
import hashlib
import html
import json
import os
import random
import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import edge_tts
import feedparser
import numpy as np
import requests
from moviepy.editor import AudioFileClip, VideoClip
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps


PAGE_NAME = os.getenv("PAGE_NAME", "World news in Sinhala")
VOICE = os.getenv("VOICE", "si-LK-ThiliniNeural")

VIDEO_SECONDS_MIN = int(os.getenv("VIDEO_SECONDS_MIN", "35"))
VIDEO_SECONDS_MAX = int(os.getenv("VIDEO_SECONDS_MAX", "55"))
MAX_OLD_ITEMS = int(os.getenv("MAX_OLD_ITEMS", "1000"))
MAX_VIDEOS_PER_RUN = int(os.getenv("MAX_VIDEOS_PER_RUN", "1"))
USE_NEWS_IMAGES = os.getenv("USE_NEWS_IMAGES", "true").lower() == "true"

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
ASSET_DIR = Path(os.getenv("ASSET_DIR", "assets"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

FPS = int(os.getenv("FPS", "24"))
W, H = 1080, 1920

NEWS_FEEDS = [
    "https://www.lankadeepa.lk/rss/latest_news/1",
    "https://www.lankadeepa.lk/rss/world_news/14",
    "https://www.lankadeepa.lk/rss/business/9",
    "https://www.lankadeepa.lk/rss/sports/3",
    "https://adaderana.lk/rss.php",
    "https://www.dailymirror.lk/rss/breaking-news",
    "https://www.dailymirror.lk/rss/world-news",
    "https://dailynews.lk/feed",
    "https://dinamina.lk/feed",
    "https://news.lk/news?format=feed",
    "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/headlines/section/topic/NATION?hl=en-US&gl=US&ceid=US:en",
]

BANNED_TITLE_WORDS = [
    "live updates",
    "opinion",
    "analysis:",
    "sponsored",
    "advertisement",
    "newsletter",
]


def prepare_folders():
    for folder in [OUTPUT_DIR, ASSET_DIR]:
        if folder.exists() and not folder.is_dir():
            folder.unlink()
        folder.mkdir(parents=True, exist_ok=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"used": []}
    return {"used": []}


def save_state(state: dict):
    state["used"] = state.get("used", [])[-MAX_OLD_ITEMS:]
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"&nbsp;", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def has_sinhala(text: str) -> bool:
    return bool(re.search(r"[\u0D80-\u0DFF]", text or ""))


def source_from_entry(entry) -> str:
    source = ""

    if hasattr(entry, "source") and isinstance(entry.source, dict):
        source = entry.source.get("title", "")

    if not source:
        source = getattr(entry, "author", "") or getattr(entry, "publisher", "")

    if not source:
        link = getattr(entry, "link", "")
        host = urlparse(link).netloc.replace("www.", "")
        source = host.split(".")[0].title() if host else "News Update"

    return clean_text(source)[:50]


def strip_source_from_title(title: str):
    if " - " in title:
        headline, source = title.rsplit(" - ", 1)
        if len(headline) > 12:
            return headline.strip(), source.strip()
    return title.strip(), ""


def item_id(title: str, link: str) -> str:
    base = re.sub(r"[^a-z0-9අ-෿]+", " ", f"{title} {link}".lower()).strip()
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def is_good_item(title: str, summary: str) -> bool:
    if len(title) < 12 or len(title) > 190:
        return False

    title_lower = title.lower()

    if any(word in title_lower for word in BANNED_TITLE_WORDS):
        return False

    return True


def extract_first_image_from_html(text: str) -> str:
    if not text:
        return ""

    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', text, re.I)

    if match:
        return html.unescape(match.group(1))

    return ""


def get_news_image_url(entry) -> str:
    try:
        media_content = getattr(entry, "media_content", [])

        if media_content:
            for media in media_content:
                url = media.get("url", "")
                medium = media.get("medium", "")

                if url and (
                    "image" in medium.lower()
                    or url.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                ):
                    return url

                if url:
                    return url
    except Exception:
        pass

    try:
        media_thumbnail = getattr(entry, "media_thumbnail", [])

        if media_thumbnail:
            for media in media_thumbnail:
                url = media.get("url", "")
                if url:
                    return url
    except Exception:
        pass

    try:
        links = getattr(entry, "links", [])

        for link in links:
            href = link.get("href", "")
            link_type = link.get("type", "")
            rel = link.get("rel", "")

            if href and (
                "image" in link_type.lower()
                or rel.lower() in ["enclosure", "thumbnail"]
                or href.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
            ):
                return href
    except Exception:
        pass

    raw_summary = getattr(entry, "summary", "")
    image = extract_first_image_from_html(raw_summary)

    if image:
        return image

    try:
        if getattr(entry, "content", None):
            raw_content = entry.content[0].get("value", "")
            image = extract_first_image_from_html(raw_content)

            if image:
                return image
    except Exception:
        pass

    return ""


def download_news_image(image_url: str, item_id_value: str) -> Path | None:
    if not image_url:
        return None

    if not image_url.lower().startswith(("http://", "https://")):
        return None

    headers = {
        "User-Agent": "SinhalaNewsVideoBot/2.0"
    }

    try:
        response = requests.get(image_url, headers=headers, timeout=25)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()

        if "image" not in content_type and not image_url.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp")
        ):
            return None

        image_path = ASSET_DIR / f"news_image_{item_id_value}.jpg"

        image = Image.open(BytesIO(response.content)).convert("RGB")
        image.save(image_path, "JPEG", quality=92)

        return image_path

    except Exception as e:
        print(f"Image download failed: {e}")
        return None


def fetch_items() -> list[dict]:
    items = []
    feeds = NEWS_FEEDS[:]
    random.shuffle(feeds)

    headers = {
        "User-Agent": "SinhalaNewsVideoBot/2.0"
    }

    for feed_url in feeds:
        try:
            response = requests.get(feed_url, headers=headers, timeout=25)
            response.raise_for_status()
            parsed = feedparser.parse(response.content)

        except Exception as e:
            print(f"News source failed: {feed_url} -> {e}")
            continue

        for entry in parsed.entries[:25]:
            raw_title = clean_text(getattr(entry, "title", ""))
            summary = clean_text(getattr(entry, "summary", ""))
            link = clean_text(getattr(entry, "link", ""))
            source = source_from_entry(entry)

            title, title_source = strip_source_from_title(raw_title)

            if title_source:
                source = title_source

            if not is_good_item(title, summary):
                continue

            uid = item_id(title, link)

            items.append({
                "id": uid,
                "title": title,
                "summary": summary,
                "link": link,
                "source": source,
                "image_url": get_news_image_url(entry),
                "origin": feed_url,
            })

    random.shuffle(items)
    return items


def pick_new_items(items: list[dict], state: dict, count: int) -> list[dict]:
    used = set(state.get("used", []))
    picked = []
    seen_titles = set()

    for item in items:
        title_key = re.sub(
            r"[^a-z0-9අ-෿]+",
            " ",
            item["title"].lower()
        ).strip()[:100]

        if item["id"] in used:
            continue

        if title_key in seen_titles:
            continue

        seen_titles.add(title_key)
        picked.append(item)

        if len(picked) >= count:
            break

    return picked


def make_original_script(item: dict) -> str:
    title = item["title"].strip(" .")
    summary = clean_text(item.get("summary", "")).strip(" .")

    if len(summary) > 190:
        summary = summary[:190].rsplit(" ", 1)[0] + "."

    if not has_sinhala(title):
        title = f"විදේශ පුවතක් ලෙස වාර්තා වන සිදුවීමක්: {title}"

    if summary and not has_sinhala(summary):
        summary = ""

    openers = [
        "ලෝක පුවත් සිංහල වෙතින් ඔබට ඉක්මන් පුවත් යාවත්කාලීනයක්.",
        "මෙන්න මේ මොහොතේ අවධානයට ලක්ව ඇති ප්‍රධාන පුවතක්.",
        "අලුත්ම පුවත් වාර්තාවල සඳහන් වන වැදගත් තොරතුරක් මෙන්න.",
        "මේ වන විට ජනතාවගේ අවධානයට ලක්ව ඇති පුවතක් මෙන්න.",
    ]

    middles = [
        f"ප්‍රධාන සිරස්තලය මෙයයි. {title}.",
        f"මේ වන විට වාර්තා වන ප්‍රධාන සිදුවීම මෙයයි. {title}.",
        f"මේ පුවත සම්බන්ධයෙන් අවධානය යොමු වී ඇත්තේ මෙයටයි. {title}.",
    ]

    context = ""

    if summary and summary.lower() not in title.lower():
        context = f" සරලව කියනවා නම්, {summary}"

    closers = [
        "මෙය සංවර්ධනය වෙමින් පවතින පුවතක් බැවින් නව තොරතුරු පසුව වෙනස් විය හැක.",
        "වැඩි විශ්වාසදායක තොරතුරු ලැබෙන විට අපි තවත් යාවත්කාලීන ගෙන එන්නෙමු.",
        "තහවුරු කළ තොරතුරු සඳහා විශ්වාසදායක පුවත් මූලාශ්‍ර පරීක්ෂා කරන්න.",
        "තවත් වැදගත් පුවත් සඳහා ලෝක පුවත් සිංහල සමඟ රැඳී සිටින්න.",
    ]

    script = f"{random.choice(openers)} {random.choice(middles)}{context} {random.choice(closers)}"
    script = clean_text(script)

    words = script.split()

    if len(words) > 110:
        script = " ".join(words[:110]).rsplit(".", 1)[0] + "."

    return script


async def make_voice(text: str, out_mp3: Path):
    communicate = edge_tts.Communicate(
        text=text,
        voice=VOICE,
        rate="+0%",
        volume="+0%"
    )

    await communicate.save(str(out_mp3))


def get_font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansSinhala-Bold.ttf"
        if bold else
        "/usr/share/fonts/truetype/noto/NotoSansSinhala-Regular.ttf",

        "/usr/share/fonts/truetype/noto/NotoSansSinhalaUI-Bold.ttf"
        if bold else
        "/usr/share/fonts/truetype/noto/NotoSansSinhalaUI-Regular.ttf",

        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]

    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


def wrap_lines(text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines = []
    line = ""

    dummy = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(dummy)

    for word in words:
        test = (line + " " + word).strip()
        box = draw.textbbox((0, 0), test, font=font)

        if box[2] - box[0] <= max_width:
            line = test
        else:
            if line:
                lines.append(line)

            line = word

    if line:
        lines.append(line)

    return lines


def split_caption_chunks(script: str) -> list[str]:
    words = script.split()
    chunks = []
    current = []

    for word in words:
        current.append(word)

        if len(current) >= 6 or word.endswith((".", "?", "!", "।")):
            chunks.append(" ".join(current))
            current = []

    if current:
        chunks.append(" ".join(current))

    return chunks


def draw_round_rect(draw, xy, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(
        xy,
        radius=radius,
        fill=fill,
        outline=outline,
        width=width
    )


def make_generated_news_background(seed: int) -> Image.Image:
    random.seed(seed)

    base1 = np.array([6, 10, 25], dtype=np.uint8)
    base2 = np.array([25, 55, 95], dtype=np.uint8)

    y = np.linspace(0, 1, H)[:, None]
    grad = (base1 * (1 - y) + base2 * y).astype(np.uint8)
    img = np.repeat(grad[:, None, :], W, axis=1)

    im = Image.fromarray(img, "RGB").convert("RGBA")
    draw = ImageDraw.Draw(im)

    for x in range(-300, W + 300, 120):
        draw.line((x, 0, x + 400, H), fill=(255, 255, 255, 18), width=2)

    for y_pos in range(0, H, 120):
        draw.line((0, y_pos, W, y_pos), fill=(255, 255, 255, 14), width=1)

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    for _ in range(20):
        x = random.randint(-250, W)
        y0 = random.randint(-250, H)
        r = random.randint(160, 480)
        alpha = random.randint(12, 45)
        d.ellipse((x, y0, x + r, y0 + r), fill=(255, 255, 255, alpha))

    overlay = overlay.filter(ImageFilter.GaussianBlur(30))
    im = Image.alpha_composite(im, overlay)

    return im.convert("RGB")


def cover_resize_image(
    img: Image.Image,
    width: int,
    height: int,
    zoom: float = 1.0
) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGB")

    iw, ih = img.size
    target_ratio = width / height
    img_ratio = iw / ih

    if img_ratio > target_ratio:
        new_h = height
        new_w = int(height * img_ratio)
    else:
        new_w = width
        new_h = int(width / img_ratio)

    new_w = int(new_w * zoom)
    new_h = int(new_h * zoom)

    img = img.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - width) // 2
    top = (new_h - height) // 2

    return img.crop((left, top, left + width, top + height))


def make_image_background(
    image_path: Path | None,
    seed: int,
    t: float,
    duration: float,
    rolling_text: str = ""
) -> Image.Image:
    if image_path and image_path.exists():
        try:
            img = Image.open(image_path).convert("RGB")
            zoom = 1.08 + 0.05 * (t / max(duration, 0.1))
            bg = cover_resize_image(img, W, H, zoom=zoom)

            # Strong blur + dark overlay hides logos/brands/watermarks better.
            bg = bg.filter(ImageFilter.GaussianBlur(10))

            dark = Image.new("RGBA", (W, H), (0, 0, 0, 135))
            bg = Image.alpha_composite(bg.convert("RGBA"), dark).convert("RGB")

            # Extra masks for common logo positions.
            mask = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(mask)

            d.rectangle((0, 0, W, 260), fill=(0, 0, 0, 145))
            d.rectangle((0, H - 360, W, H), fill=(0, 0, 0, 160))
            d.rectangle((0, 0, 260, 260), fill=(0, 0, 0, 150))
            d.rectangle((W - 260, 0, W, 260), fill=(0, 0, 0, 150))
            d.rectangle((0, H - 260, 300, H), fill=(0, 0, 0, 150))
            d.rectangle((W - 300, H - 260, W, H), fill=(0, 0, 0, 150))

            bg = Image.alpha_composite(bg.convert("RGBA"), mask).convert("RGB")
            return bg

        except Exception as e:
            print(f"Could not use image: {e}")

    # No image found: rolling Sinhala text background.
    bg = make_generated_news_background(seed).convert("RGBA")
    draw = ImageDraw.Draw(bg)

    font = get_font(44, True)
    small_font = get_font(30, False)

    text = rolling_text or "නවතම පුවත් යාවත්කාලීනයක්"
    lines = wrap_lines(text, font, 900)

    repeated = []

    for _ in range(9):
        repeated.extend(lines)
        repeated.append("")

    total_height = max(1, len(repeated) * 68)
    scroll_y = int(H - ((t * 85) % (total_height + H)))

    y = scroll_y

    for line in repeated:
        if -100 < y < H + 100:
            draw.text(
                (90, y),
                line,
                font=font,
                fill=(255, 255, 255, 75)
            )

        y += 68

    draw.text(
        (90, 300),
        PAGE_NAME,
        font=small_font,
        fill=(255, 255, 255, 120)
    )

    dark = Image.new("RGBA", (W, H), (0, 0, 0, 90))
    bg = Image.alpha_composite(bg, dark)

    return bg.convert("RGB")


def make_frame_builder(
    item: dict,
    script: str,
    duration: float,
    image_path: Path | None
):
    title_font = get_font(54, True)
    caption_font = get_font(48, True)
    brand_font = get_font(38, True)
    label_font = get_font(34, True)
    small_font = get_font(30, False)
    tiny_font = get_font(26, False)

    title = item["title"]

    if not has_sinhala(title):
        title = "විදේශ පුවත් යාවත්කාලීනයක්"

    title_lines = wrap_lines(title, title_font, 900)[:4]

    captions = split_caption_chunks(script)
    total_chars = sum(max(1, len(c)) for c in captions)

    starts = []
    cursor = 0.0

    for cap in captions:
        starts.append(cursor)
        cursor += duration * (len(cap) / total_chars)

    def caption_at(t: float) -> str:
        idx = 0

        for i, st in enumerate(starts):
            if t >= st:
                idx = i
            else:
                break

        return captions[min(idx, len(captions) - 1)] if captions else ""

    def draw_gradient_overlay(im: Image.Image):
        im = im.convert("RGBA")

        top = Image.new("RGBA", (W, 420), (0, 0, 0, 0))
        top_px = top.load()

        for yy in range(420):
            alpha = int(200 * (1 - yy / 420))

            for xx in range(W):
                top_px[xx, yy] = (0, 0, 0, alpha)

        im.alpha_composite(top, (0, 0))

        bottom = Image.new("RGBA", (W, 760), (0, 0, 0, 0))
        bottom_px = bottom.load()

        for yy in range(760):
            alpha = int(230 * (yy / 760))

            for xx in range(W):
                bottom_px[xx, yy] = (0, 0, 0, alpha)

        im.alpha_composite(bottom, (0, H - 760))

        return im

    def frame(t: float):
        seed = int(item["id"][:6], 16)

        bg = make_image_background(
            image_path=image_path,
            seed=seed,
            t=t,
            duration=duration,
            rolling_text=script
        )

        im = draw_gradient_overlay(bg)
        draw = ImageDraw.Draw(im)

        progress = max(0, min(1, t / max(duration, 0.1)))

        draw_round_rect(
            draw,
            (50, 55, 1030, 155),
            20,
            fill=(6, 12, 28, 230),
            outline=(255, 255, 255, 65),
            width=2
        )

        draw.text(
            (82, 82),
            PAGE_NAME,
            font=brand_font,
            fill=(255, 255, 255, 255)
        )

        draw_round_rect(
            draw,
            (745, 75, 1000, 135),
            14,
            fill=(185, 20, 32, 245)
        )

        draw.text(
            (780, 89),
            "පුවත්",
            font=tiny_font,
            fill=(255, 255, 255, 255)
        )

        draw_round_rect(
            draw,
            (60, 895, 355, 970),
            14,
            fill=(185, 20, 32, 245)
        )

        draw.text(
            (95, 915),
            "ප්‍රධාන පුවත",
            font=label_font,
            fill=(255, 255, 255, 255)
        )

        draw_round_rect(
            draw,
            (50, 970, 1030, 1305),
            26,
            fill=(5, 12, 28, 232),
            outline=(255, 255, 255, 70),
            width=2
        )

        y = 1010

        for line in title_lines:
            draw.text(
                (85, y),
                line,
                font=title_font,
                fill=(255, 255, 255, 255)
            )

            y += 68

        draw.text(
            (85, 1250),
            "නවතම යාවත්කාලීනය",
            font=small_font,
            fill=(220, 230, 245, 235)
        )

        cap = caption_at(t)
        cap_lines = wrap_lines(cap, caption_font, 900)[:3]

        draw_round_rect(
            draw,
            (50, 1370, 1030, 1665),
            26,
            fill=(255, 255, 255, 235),
            outline=(255, 255, 255, 80),
            width=2
        )

        y = 1415

        for line in cap_lines:
            draw.text(
                (85, y),
                line,
                font=caption_font,
                fill=(5, 12, 28, 255)
            )

            y += 64

        draw_round_rect(
            draw,
            (50, 1710, 1030, 1760),
            10,
            fill=(5, 12, 28, 225)
        )

        draw_round_rect(
            draw,
            (50, 1710, int(50 + 980 * progress), 1760),
            10,
            fill=(185, 20, 32, 245)
        )

        footer = "නවතම පුවත් • පැහැදිලි විස්තර • තවත් පුවත් සඳහා Follow කරන්න"

        draw.text(
            (65, 1810),
            footer[:90],
            font=tiny_font,
            fill=(245, 245, 245, 235)
        )

        return np.array(im.convert("RGB"))

    return frame


def safe_filename(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9අ-෿]+", "_", text).strip("_").lower()
    return text[:75] or "sinhala_news_video"


def create_video(
    item: dict,
    script: str,
    audio_path: Path,
    image_path: Path | None
) -> Path:
    audio = AudioFileClip(str(audio_path))

    duration = min(
        max(audio.duration + 0.6, VIDEO_SECONDS_MIN),
        VIDEO_SECONDS_MAX
    )

    frame_builder = make_frame_builder(
        item=item,
        script=script,
        duration=duration,
        image_path=image_path
    )

    clip = VideoClip(
        frame_builder,
        duration=duration
    ).set_audio(
        audio.subclip(0, min(audio.duration, duration))
    )

    filename = (
        f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_"
        f"{safe_filename(item['title'])}.mp4"
    )

    out_path = OUTPUT_DIR / filename

    clip.write_videofile(
        str(out_path),
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        preset="medium",
        bitrate="4500k",
        threads=2,
        verbose=False,
        logger=None
    )

    clip.close()
    audio.close()

    return out_path


async def main():
    prepare_folders()

    state = load_state()
    items = fetch_items()
    picked = pick_new_items(items, state, MAX_VIDEOS_PER_RUN)

    if not picked:
        print("No fresh unused news item found.")
        return

    made = []

    for item in picked:
        print("TITLE:", item["title"])
        print("LINK:", item["link"])

        script = make_original_script(item)
        print("SCRIPT:", script)

        audio_path = OUTPUT_DIR / f"voice_{item['id']}.mp3"

        await make_voice(script, audio_path)

        image_path = None

        if USE_NEWS_IMAGES and item.get("image_url"):
            image_path = download_news_image(item["image_url"], item["id"])

        video_path = create_video(
            item=item,
            script=script,
            audio_path=audio_path,
            image_path=image_path
        )

        made.append(str(video_path))
        state.setdefault("used", []).append(item["id"])

        meta = {
            "title": item["title"],
            "source": item["source"],
            "link": item["link"],
            "used_image": str(image_path) if image_path else None,
            "script": script,
            "video": str(video_path),
            "created_utc": datetime.now(timezone.utc).isoformat(),
        }

        video_path.with_suffix(".json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    save_state(state)

    print("Created videos:")

    for video in made:
        print(video)


if __name__ == "__main__":
    asyncio.run(main())
