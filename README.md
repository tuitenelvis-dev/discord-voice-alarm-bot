# Discord Voice Alarm Bot

Bot Discord Python dùng `discord.py` v2 slash commands, phát nhạc bằng `yt-dlp` + FFmpeg, treo voice 24/7 và báo thức theo giờ Việt Nam.

## Tính năng

- `/play query`: phát nhạc từ YouTube URL, từ khóa tìm kiếm, hoặc direct audio URL.
- `/play playlist_url`: dán link YouTube playlist để thêm tối đa 50 bài vào queue.
- `/queue`: xem hàng chờ.
- `/skip`: bỏ qua bài hiện tại.
- `/pause` và `/resume`: tạm dừng/phát tiếp.
- `/stop`: dừng nhạc và xóa queue nhưng vẫn ở voice room.
- `/join247`: đặt kênh voice hiện tại làm stay channel 24/7.
- `/leave247`: tắt chế độ treo room cho server.
- `/setalarm HH:MM nội_dung`: đặt báo thức theo múi giờ Việt Nam.
- Khi báo thức tới giờ, bot dừng nhạc đang phát và phát âm báo kiểu Messenger trong voice room.
- `/stopalarm`: bất kỳ ai cũng có thể tắt âm báo đang kêu.
- `/alarms`: xem báo thức của bạn.
- `/cancelalarm alarm_id`: xóa báo thức.

## Cài đặt

```bash
python -m pip install -r requirements.txt
```

Cài FFmpeg nếu máy chưa có:

```bash
install_ffmpeg.bat
```

Sau khi cài FFmpeg, restart VS Code hoặc terminal.

## Cấu hình

Copy `.env.example` thành `.env`, rồi điền token:

```env
DISCORD_TOKEN=your_token_here
DEV_GUILD_ID=
FFMPEG_PATH=ffmpeg
```

Nếu đang dev, nên điền `DEV_GUILD_ID` để slash command sync vào một server nhanh hơn.

## Chạy bot

```bash
python -m src.bot
```

Hoặc double-click `run_bot.bat`.

## Lưu ý Spotify

Phiên bản miễn phí này dùng `yt-dlp` + FFmpeg, nên YouTube/direct URL là ổn định nhất. Spotify URL được xử lý theo kiểu best-effort: bot thử đọc metadata rồi tìm bài tương ứng trên YouTube. Nếu muốn Spotify playlist/album đầy đủ và ổn định hơn, nên nâng cấp sang Lavalink + plugin Spotify.

## Nguồn tham khảo

- `discord.py` latest stable trên PyPI là v2.7.1.
- Slash commands dùng `discord.app_commands`.
- Alarm loop chạy theo `Asia/Ho_Chi_Minh` bằng `zoneinfo`.
