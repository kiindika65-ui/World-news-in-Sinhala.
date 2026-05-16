import json
import os
import sys
from pathlib import Path

import requests


OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PAGE_NAME = os.getenv("PAGE_NAME", "World news in Sinhala")


def fail(message: str):
    print(f"ERROR: {message}")
    sys.exit(1)


def find_latest_video() -> Path:
    videos = sorted(
        OUTPUT_DIR.glob("*.mp4"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

    if not videos:
        fail("No MP4 video found in output folder.")

    return videos[0]


def read_meta(video_path: Path) -> dict:
    meta_path = video_path.with_suffix(".json")

    if not meta_path.exists():
        return {}

    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def make_caption(meta: dict) -> str:
    title = meta.get("title", "නවතම පුවත් යාවත්කාලීනයක්")

    caption = (
        f"📰 {title}\n\n"
        f"{PAGE_NAME}\n"
        f"නවතම පුවත් • පැහැදිලි විස්තර"
    )

    return caption[:1000]


def send_video(video_path: Path, caption: str):
    if not BOT_TOKEN:
        fail("Missing TELEGRAM_BOT_TOKEN secret.")

    if not CHAT_ID:
        fail("Missing TELEGRAM_CHAT_ID secret.")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"

    with video_path.open("rb") as video_file:
        files = {"video": video_file}
        data = {
            "chat_id": CHAT_ID,
            "caption": caption,
            "supports_streaming": "true"
        }

        response = requests.post(url, data=data, files=files, timeout=180)

    try:
        result = response.json()
    except Exception:
        result = {"ok": False, "description": response.text}

    if not response.ok or not result.get("ok"):
        print(result)
        fail("Telegram video upload failed.")

    print("Telegram upload successful.")


def main():
    video_path = find_latest_video()
    meta = read_meta(video_path)
    caption = make_caption(meta)

    print(f"Uploading to Telegram: {video_path}")
    send_video(video_path, caption)


if __name__ == "__main__":
    main()
