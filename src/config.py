from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"
VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


@dataclass(frozen=True)
class Settings:
    token: str
    dev_guild_id: int | None
    ffmpeg_path: str


def load_settings() -> Settings:
    load_dotenv(ROOT_DIR / ".env")

    token = os.getenv("DISCORD_TOKEN", "").strip()
    dev_guild_raw = os.getenv("DEV_GUILD_ID", "").strip()
    ffmpeg_path = os.getenv("FFMPEG_PATH", "ffmpeg").strip() or "ffmpeg"

    dev_guild_id = int(dev_guild_raw) if dev_guild_raw.isdigit() else None

    return Settings(token=token, dev_guild_id=dev_guild_id, ffmpeg_path=ffmpeg_path)
