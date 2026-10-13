"""Telegram group music bot - GitHub Actions edition.

القيم الثابتة (الايديات ونص ستارت) تعدّلها في قسم CONFIGURATION.
التوكن والجلسة تأتي من GitHub Secrets:
    BOT_TOKEN       توكن البوت
    SESSION_STRING  جلسة الحساب المساعد (Telethon StringSession)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import re
import signal
import time
from html import escape as html_escape
from pathlib import Path
from typing import Any

from pytgcalls import PyTgCalls
from pytgcalls.types import AudioQuality, MediaStream
from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.sessions import StringSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("music-bot")


# ============================== CONFIGURATION ==============================
API_ID = 38720187
API_HASH = "a5c27bc42b391f32db86befcabc68094"

# ايدي كروب الصوت (مثل -1001234567890) وايدي كروب اليوتيوب المساعد.
TARGET_CHAT_ID = -1003928945885
YOUTUBE_CHAT_ID = -1003928945885

# اختياري: رابط الكروب العام. إذا تركته فارغًا يحاول البوت استخراجه.
TARGET_CHAT_LINK = ""
DEVELOPER_URL = ""

# كليشة ستارت الثابتة.
START_TEXT = (
    "أهلًا بك في بوت ميوزك\n"
    "رد على فيديو أو ملف صوتي بكلمة شغل للتشغيل."
)
# أزرار ستارت الثابتة: ("الاسم", "الرابط")
START_BUTTONS: list[tuple[str, str]] = [
    # ("قناة البوت", "https://t.me/example"),
]

MAX_MEDIA_MB = 200
# عدد الاتصالات المتوازية للتحميل.
PARALLEL_DOWNLOAD_WORKERS = 4
# مدة التشغيل قبل الإغلاق الذاتي (ثواني) ليعيد الـworkflow تشغيله.
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "20400"))
# ============================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()

DATA_DIR = Path("data")
DOWNLOAD_DIR = Path("downloads")
CACHE_DIR = DOWNLOAD_DIR / "cache"
for _dir in (DATA_DIR, DOWNLOAD_DIR, CACHE_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

DOWNLOAD_CHUNK_SIZE = 1024 * 1024

PLAY_WORDS = {"شغل", "تشغيل", "play", "/شغل", "/تشغيل", "/play"}
STOP_WORDS = {"ايقاف", "إيقاف", "وقف", "stop", "/ايقاف", "/إيقاف", "/وقف", "/stop"}
SKIP_WORDS = {"تخطي", "التالي", "skip", "/تخطي", "/التالي", "/skip"}
WAIT_WORDS = {
    "الانتظار", "الأنتظار", "الانتضار", "الأنتضار", "انتظار", "انتضار",
    "/الانتظار", "/الأنتظار", "/الانتضار", "/الأنتضار", "/انتظار", "/انتضار",
    "حالة", "حاله", "الحالة", "الحاله",
    "/حالة", "/حاله", "/الحالة", "/الحاله", "status", "/status",
}
CLEAR_QUEUE_WORDS = {
    "مسح حالة", "مسح حاله", "مسح الحالة", "مسح الحاله",
    "/مسح حالة", "/مسح حاله", "/مسح الحالة", "/مسح الحاله",
}
START_WORDS = {"/start", "start"}

MEDIA_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma",
    ".mp4", ".m4v", ".webm", ".mkv", ".avi", ".mov",
}


def clean_command(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    return parts[0].lower() if parts else ""


def normalized_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, remaining = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remaining:02d}"
    return f"{minutes:02d}:{remaining:02d}"


def media_from_message(message: Any) -> Any | None:
    """Return audio/video media, including documents with a generic MIME."""
    for attr in ("video", "audio", "voice"):
        value = getattr(message, attr, None)
        if value:
            return value

    document = getattr(message, "document", None)
    if not document:
        return None

    mime = (getattr(document, "mime_type", "") or "").lower()
    file_obj = getattr(message, "file", None)
    name = (getattr(file_obj, "name", "") or "").lower()
    extension = (getattr(file_obj, "ext", "") or "").lower()
    if (
        mime.startswith("video/")
        or mime.startswith("audio/")
        or Path(name).suffix in MEDIA_EXTENSIONS
        or extension in MEDIA_EXTENSIONS
    ):
        return document
    return None


def media_filename(message: Any) -> str:
    message_file = getattr(message, "file", None)
    name = getattr(message_file, "name", None) or f"media_{message.id}.bin"
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", name)[:120]


def media_size(media: Any) -> int:
    return int(getattr(media, "size", 0) or getattr(media, "file_size", 0) or 0)


def control_buttons() -> list[list[Any]]:
    return [
        [Button.inline("الانتظار", b"music_status")],
        [
            Button.inline("إيقاف", b"music_stop"),
            Button.inline("تخطي", b"music_skip"),
        ],
    ]


def start_buttons() -> list[list[Any]] | None:
    rows: list[list[Any]] = []
    row: list[Any] = []
    for label, url in START_BUTTONS:
        row.append(Button.url(label[:64], url))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if DEVELOPER_URL:
        rows.append([Button.url("مطور البوت", DEVELOPER_URL)])
    return rows or None


def progress_bar(fraction: float, width: int = 12) -> str:
    filled = int(max(0.0, min(1.0, fraction)) * width)
    return "▰" * filled + "▱" * (width - filled)


async def report_progress(
    progress: Any, state: dict[str, Any], total: int, label: str
) -> None:
    """Edit the progress message every ~2s: percent, size, speed, time left."""
    started = time.monotonic()
    last_text = ""
    while True:
        await asyncio.sleep(2)
        done = state["done"]
        speed = done / max(time.monotonic() - started, 0.001)
        done_mb = done / 1048576
        if total:
            fraction = done / total
            remaining = (total - done) / speed if speed > 0 else 0
            text = (
                f"{label}\n"
                f"{progress_bar(fraction)} {fraction * 100:.0f}%\n"
                f"الحجم : {done_mb:.1f} / {total / 1048576:.1f} MB\n"
                f"السرعة : {speed / 1048576:.1f} MB/s\n"
                f"المتبقي : {format_duration(remaining)}"
            )
        else:
            text = (
                f"{label}\n"
                f"تم تحميل : {done_mb:.1f} MB\n"
                f"السرعة : {speed / 1048576:.1f} MB/s"
            )
        if text != last_text:
            with contextlib.suppress(Exception):
                await progress.edit(text)
            last_text = text


@contextlib.asynccontextmanager
async def download_progress(
    progress: Any, total: int, label: str = "جار التحميل"
):
    """Yield a counter dict; while open, the progress message is updated."""
    state: dict[str, Any] = {"done": 0}
    task = (
        asyncio.create_task(report_progress(progress, state, total, label))
        if progress is not None
        else None
    )
    try:
        yield state
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def parallel_download(
    client: TelegramClient,
    message: Any,
    destination: Path,
    size: int,
    state: dict[str, Any] | None = None,
) -> None:
    """Download a message's media over several connections at once."""
    destination.unlink(missing_ok=True)

    if size < DOWNLOAD_CHUNK_SIZE * PARALLEL_DOWNLOAD_WORKERS:
        # Small or unknown size: one stream is just as fast.
        with destination.open("wb") as output:
            async for chunk in client.iter_download(
                message.media, request_size=DOWNLOAD_CHUNK_SIZE
            ):
                output.write(chunk)
                if state is not None:
                    state["done"] += len(chunk)
        return

    aligned = max(
        (size // PARALLEL_DOWNLOAD_WORKERS // DOWNLOAD_CHUNK_SIZE)
        * DOWNLOAD_CHUNK_SIZE,
        DOWNLOAD_CHUNK_SIZE,
    )
    parts: list[tuple[int, int]] = []
    for index in range(PARALLEL_DOWNLOAD_WORKERS):
        offset = index * aligned
        if offset >= size:
            break
        length = (
            aligned if index < PARALLEL_DOWNLOAD_WORKERS - 1 else size - offset
        )
        parts.append((offset, length))

    with destination.open("wb") as preallocate:
        preallocate.truncate(size)

    async def download_part(offset: int, length: int) -> None:
        written = 0
        with destination.open("r+b") as output:
            output.seek(offset)
            # Telethon's limit counts chunks, not bytes.
            async for chunk in client.iter_download(
                message.media,
                offset=offset,
                limit=math.ceil(length / DOWNLOAD_CHUNK_SIZE),
                request_size=DOWNLOAD_CHUNK_SIZE,
            ):
                data = chunk[: length - written]
                output.write(data)
                written += len(data)
                if state is not None:
                    state["done"] += len(data)
                if written >= length:
                    break
        if written != length:
            raise RuntimeError(f"تحميل ناقص ({written}/{length} بايت).")

    await asyncio.gather(*(download_part(o, l) for o, l in parts))


class GroupMusicBot:
    def __init__(self) -> None:
        self.bot = TelegramClient(StringSession(), API_ID, API_HASH)
        self.user = TelegramClient(
            StringSession(SESSION_STRING),
            API_ID,
            API_HASH,
            flood_sleep_threshold=60,
            connection_retries=10,
            retry_delay=5,
        )
        self.calls: PyTgCalls | None = None
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.player_task: asyncio.Task[None] | None = None
        self.current: dict[str, Any] | None = None
        self.advance_event = asyncio.Event()
        self.leave_when_empty = False
        self.download_locks: dict[str, asyncio.Lock] = {}
        self.youtube_cache: dict[str, tuple[Path, str]] = {}
        self.target_chat_link: str | None = None
        self.target_chat_photo_path = DATA_DIR / "target_chat_photo.jpg"
        self.stop_event = asyncio.Event()

    # ------------------------------------------------------------ helpers
    async def get_target_chat_link(self) -> str | None:
        if TARGET_CHAT_LINK.strip():
            return TARGET_CHAT_LINK.strip()
        if self.target_chat_link is not None:
            return self.target_chat_link or None

        self.target_chat_link = ""
        try:
            entity = await self.bot.get_entity(TARGET_CHAT_ID)
            username = str(getattr(entity, "username", "") or "").strip()
            if username:
                self.target_chat_link = f"https://t.me/{username}"
            else:
                raw_id = str(abs(TARGET_CHAT_ID))
                if raw_id.startswith("100"):
                    self.target_chat_link = f"https://t.me/c/{raw_id[3:]}/1"
        except Exception:
            log.exception("Could not resolve the voice group link")
        return self.target_chat_link or None

    async def get_target_chat_photo(self) -> Path | None:
        path = self.target_chat_photo_path
        if path.exists() and path.stat().st_size > 0:
            return path
        try:
            downloaded = await self.bot.download_profile_photo(
                TARGET_CHAT_ID, file=str(path)
            )
            candidate = Path(downloaded) if downloaded else path
            if candidate.exists() and candidate.stat().st_size > 0:
                if candidate != path:
                    candidate.replace(path)
                return path
        except Exception:
            log.exception("Could not download the voice group photo")
        return None

    async def requester_info(self, event: Any) -> tuple[int | None, str]:
        name = "المشرف"
        try:
            sender = await event.get_sender()
            parts = [
                str(getattr(sender, "first_name", "") or "").strip(),
                str(getattr(sender, "last_name", "") or "").strip(),
            ]
            name = " ".join(p for p in parts if p) or str(
                getattr(sender, "username", "") or ""
            ).strip()
        except Exception:
            log.exception("Could not resolve the playback requester")
        return getattr(event, "sender_id", None), name or "المشرف"

    async def playback_text(
        self, track: dict[str, Any], state: str, duration: float | None = None
    ) -> str:
        group_link = await self.get_target_chat_link()
        title = html_escape(str(track.get("title") or "ملف صوتي"))
        duration_value = html_escape(
            format_duration(duration) if duration is not None else "جارٍ الحساب..."
        )
        if group_link:
            safe_link = html_escape(group_link, quote=True)
            title = f'<a href="{safe_link}">{title}</a>'
            duration_value = f'<a href="{safe_link}">{duration_value}</a>'
        requester_name = html_escape(str(track.get("requester_name") or "المشرف"))
        requester_id = track.get("requester_id")
        requester_value = (
            f'<a href="tg://user?id={int(requester_id)}">{requester_name}</a>'
            if requester_id
            else requester_name
        )
        return (
            "<blockquote>▷ ꧁مَنَارُ الحَقّ꧂ </blockquote>\n"
            f"{html_escape(state)}\n"
            f"العنوان : {title}\n"
            f"المدة : {duration_value}\n"
            f"بواسطة : {requester_value}"
        )

    async def is_admin(self, user_id: int | None) -> bool:
        if not user_id:
            return False
        try:
            permissions = await self.bot.get_permissions(TARGET_CHAT_ID, user_id)
            return bool(permissions.is_admin or permissions.is_creator)
        except Exception:
            log.exception("Could not check group admin status")
            return False

    # ----------------------------------------------------------- download
    async def download_media(self, message: Any, progress: Any = None) -> Path:
        media = media_from_message(message)
        if media is None:
            raise ValueError("الرسالة لا تحتوي على ملف صوتي أو فيديو.")

        size = media_size(media)
        if size > MAX_MEDIA_MB * 1024 * 1024:
            raise ValueError(f"حجم الملف أكبر من {MAX_MEDIA_MB} ميغابايت.")

        cache_key = str(getattr(media, "id", None) or message.id)
        suffix = Path(media_filename(message)).suffix or ".media"
        cached_path = CACHE_DIR / f"{cache_key}{suffix}"
        lock = self.download_locks.setdefault(cache_key, asyncio.Lock())

        async with lock:
            # Same file requested again during this run: play from cache.
            if cached_path.exists() and cached_path.stat().st_size > 0:
                log.info("Using cached media %s", cached_path)
                return cached_path

            started = time.monotonic()
            temporary = DOWNLOAD_DIR / f".{cache_key}.part"
            try:
                async with download_progress(progress, size) as state:
                    await parallel_download(
                        self.bot, message, temporary, size, state
                    )
                temporary.replace(cached_path)
                log.info(
                    "Downloaded %s (%.1f MB) in %.2fs",
                    cache_key,
                    size / 1024 / 1024,
                    time.monotonic() - started,
                )
                return cached_path
            finally:
                temporary.unlink(missing_ok=True)

    async def media_duration(self, path: Path) -> float:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await process.communicate()
        try:
            return max(1.0, float(output.decode().strip()))
        except (ValueError, UnicodeDecodeError):
            return 3600.0

    async def download_youtube_audio(
        self, response: Any, query: str, progress: Any = None
    ) -> tuple[Path, str] | None:
        media = media_from_message(response)
        if media is None:
            return None

        cache_key = str(getattr(media, "id", None) or response.id)
        original_name = media_filename(response)
        suffix = Path(original_name).suffix or ".media"
        cached_path = CACHE_DIR / f"{cache_key}{suffix}"
        lock = self.download_locks.setdefault(cache_key, asyncio.Lock())

        async with lock:
            if cached_path.exists() and cached_path.stat().st_size > 0:
                return cached_path, original_name

            temporary = DOWNLOAD_DIR / f".youtube_{cache_key}.part"
            try:
                total = media_size(media)
                async with download_progress(progress, total) as state:
                    await parallel_download(
                        self.user, response, temporary, total, state
                    )
                if temporary.stat().st_size == 0:
                    return None
                temporary.replace(cached_path)
                return cached_path, original_name or query
            finally:
                temporary.unlink(missing_ok=True)

    async def wait_for_youtube_audio(self, request: Any) -> Any | None:
        # The helper group is dedicated to this exchange, so the first new
        # media message after our request is the result.
        for _ in range(90):
            async for response in self.user.iter_messages(
                YOUTUBE_CHAT_ID, min_id=request.id, limit=50
            ):
                if response.id > request.id and media_from_message(response):
                    return response
            await asyncio.sleep(1)
        return None

    async def request_youtube_audio(
        self, query: str, progress: Any = None
    ) -> tuple[Path, str] | None:
        if not YOUTUBE_CHAT_ID:
            log.error("YouTube helper group is not configured")
            return None

        key = normalized_text(query)
        cached = self.youtube_cache.get(key)
        if cached and cached[0].exists() and cached[0].stat().st_size > 0:
            log.info("YouTube query served from cache: %s", key)
            return cached

        try:
            request = await self.user.send_message(YOUTUBE_CHAT_ID, f"يوت {query}")
            response = await self.wait_for_youtube_audio(request)
            if response is None:
                return None
            result = await self.download_youtube_audio(response, query, progress)
            if result:
                self.youtube_cache[key] = result
            return result
        except Exception:
            log.exception("YouTube helper request failed")
            return None

    # ------------------------------------------------------ status message
    async def send_playback_status(
        self, chat_id: int, track: dict[str, Any], state: str
    ) -> Any:
        text = await self.playback_text(track, state)
        photo = await self.get_target_chat_photo()
        if photo:
            with contextlib.suppress(Exception):
                return await self.bot.send_file(
                    chat_id, photo, caption=text,
                    parse_mode="html", buttons=control_buttons(),
                )
        return await self.bot.send_message(
            chat_id, text, parse_mode="html",
            buttons=control_buttons(), link_preview=False,
        )

    async def edit_status(self, status_message: Any, text: str) -> Any:
        try:
            await self.bot.edit_message(
                status_message.chat_id, status_message.id, text,
                parse_mode="html", buttons=control_buttons(), link_preview=False,
            )
            return status_message
        except MessageNotModifiedError:
            return status_message
        except Exception as exc:
            log.warning("Could not edit playback message (%s); resending", exc)
            with contextlib.suppress(Exception):
                return await self.bot.send_message(
                    status_message.chat_id, text, parse_mode="html",
                    buttons=control_buttons(), link_preview=False,
                )
            return status_message

    # -------------------------------------------------------------- player
    async def player_loop(self) -> None:
        while True:
            track = await self.queue.get()
            self.current = track
            status_message = track["status_message"]
            try:
                if self.calls is None:
                    raise RuntimeError("Assistant account is not connected.")
                await self.calls.play(
                    TARGET_CHAT_ID,
                    # No conversion: the file is played as it is. Video files
                    # keep their video, audio files play as audio.
                    MediaStream(
                        str(track["path"]),
                        audio_parameters=AudioQuality.STUDIO,
                        video_flags=MediaStream.Flags.AUTO_DETECT,
                    ),
                )
                duration = await self.media_duration(track["path"])
                track["status_message"] = await self.edit_status(
                    status_message,
                    await self.playback_text(track, "بدأ التشغيل", duration),
                )
                self.advance_event.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self.advance_event.wait(), timeout=duration + 1
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not play %s", track["path"])
                track["status_message"] = await self.edit_status(
                    status_message,
                    "تعذر تشغيل الصوت. تأكد من إضافة الحساب المساعد وفتح المحادثة الصوتية.",
                )
            finally:
                self.current = None
                self.queue.task_done()
                if self.leave_when_empty and self.queue.empty():
                    self.leave_when_empty = False
                    if self.calls is not None:
                        with contextlib.suppress(Exception):
                            await self.calls.leave_call(TARGET_CHAT_ID)

    async def ensure_player(self) -> None:
        if self.player_task is None or self.player_task.done():
            self.player_task = asyncio.create_task(self.player_loop())

    async def advance(self) -> bool:
        if self.current is None:
            return False
        self.leave_when_empty = self.queue.empty()
        self.advance_event.set()
        return True

    async def status_text(self) -> str:
        waiting = self.queue.qsize()
        if self.current:
            return (
                f"التشغيل الآن:\n{self.current['title']}\n"
                f"قائمة الانتظار: {waiting} ملف"
            )
        return f"لا يوجد تشغيل\nقائمة الانتظار: {waiting} ملف"

    async def clear_waiting(self) -> int:
        removed = 0
        while True:
            try:
                track = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.queue.task_done()
            removed += 1
            with contextlib.suppress(Exception):
                await self.edit_status(
                    track["status_message"], "تم مسح الملف من قائمة الانتظار."
                )
        return removed

    async def enqueue(
        self, event: Any, progress: Any, path: Path, title: str
    ) -> None:
        waiting_before = self.queue.qsize() + (1 if self.current else 0)
        requester_id, requester_name = await self.requester_info(event)
        track: dict[str, Any] = {
            "path": path,
            "status_message": None,
            "title": title,
            "requester_id": requester_id,
            "requester_name": requester_name,
        }
        with contextlib.suppress(Exception):
            await progress.delete()
        state = "جار التشغيل" if waiting_before == 0 else "في قائمة الانتظار"
        track["status_message"] = await self.send_playback_status(
            event.chat_id, track, state
        )
        await self.queue.put(track)
        await self.ensure_player()
        if waiting_before:
            track["status_message"] = await self.edit_status(
                track["status_message"],
                (await self.playback_text(track, "في قائمة الانتظار"))
                + "\n\nتمت الإضافة إلى قائمة التشغيل.\n"
                f"عدد الأصوات في الانتظار: {self.queue.qsize()}",
            )

    # ------------------------------------------------------------ handlers
    async def send_start(self, event: Any) -> None:
        await event.respond(
            START_TEXT or "مرحبًا.", buttons=start_buttons(), link_preview=False
        )

    async def handle_group_message(self, event: Any) -> None:
        text = (event.raw_text or "").strip()
        command = clean_command(text)
        full_command = normalized_text(text)

        known = (
            full_command in CLEAR_QUEUE_WORDS
            or full_command in WAIT_WORDS
            or command in STOP_WORDS
            or command in SKIP_WORDS
            or command in START_WORDS
            or command in PLAY_WORDS
        )
        if not known:
            return
        if not await self.is_admin(event.sender_id):
            return

        if full_command in CLEAR_QUEUE_WORDS:
            removed = await self.clear_waiting()
            await event.reply(
                f"تم مسح قائمة الانتظار.\nعدد الأصوات التي تم مسحها: {removed}"
            )
            return

        if command in STOP_WORDS or command in SKIP_WORDS:
            advanced = await self.advance()
            await event.reply(
                "تم الانتقال للملف التالي."
                if advanced and not self.leave_when_empty
                else "تم إيقاف التشغيل ومغادرة الاستيج."
                if advanced
                else "لا يوجد ملف يعمل حاليًا."
            )
            return

        if full_command in WAIT_WORDS:
            await event.reply(await self.status_text(), buttons=control_buttons())
            return

        if command in START_WORDS:
            await self.send_start(event)
            return

        parts = text.split(maxsplit=1)
        youtube_query = parts[1].strip() if len(parts) > 1 else ""
        if youtube_query:
            progress = await event.reply("جار البحث عن المقطع وتحميله ...")
            result = await self.request_youtube_audio(youtube_query, progress)
            if result is None:
                await progress.edit("تعذر الحصول على البصمة.")
                return
            path, title = result
            await self.enqueue(event, progress, path, title)
            return

        reply = await event.message.get_reply_message()
        if not reply or not media_from_message(reply):
            await event.reply("يجب أن ترد بكلمة شغل على فيديو أو ملف صوتي.")
            return

        progress = await event.reply("جار التحميل ...")
        try:
            path = await self.download_media(reply, progress)
            await self.enqueue(event, progress, path, media_filename(reply))
        except Exception:
            log.exception("Download failed")
            await progress.edit("فشل تحميل الملف. تأكد من الصلاحيات وحجم الفيديو.")

    async def _callback_handler(self, event: Any) -> None:
        if event.chat_id != TARGET_CHAT_ID:
            return
        if not await self.is_admin(event.sender_id):
            await event.answer("هذا الزر للمشرفين فقط.", alert=True)
            return

        action = event.data
        if action in {b"music_stop", b"music_skip"}:
            advanced = await self.advance()
            await event.answer(
                "تم الانتقال للملف التالي."
                if advanced and not self.leave_when_empty
                else "تم إيقاف التشغيل ومغادرة الاستيج."
                if advanced
                else "لا يوجد ملف يعمل حاليًا.",
                alert=False,
            )
        elif action == b"music_status":
            await event.answer(await self.status_text(), alert=True)

    async def callback_handler(self, event: Any) -> None:
        try:
            await self._callback_handler(event)
        except Exception:
            log.exception("Unhandled callback error for action=%r", event.data)
            with contextlib.suppress(Exception):
                await event.answer("تعذر تنفيذ العملية.", alert=True)

    async def new_message_handler(self, event: Any) -> None:
        try:
            if event.is_private:
                if clean_command(event.raw_text or "") in START_WORDS:
                    await self.send_start(event)
            elif event.chat_id == TARGET_CHAT_ID:
                await self.handle_group_message(event)
        except Exception:
            log.exception(
                "Unhandled message error chat=%s sender=%s",
                event.chat_id, event.sender_id,
            )

    # ------------------------------------------------------------ lifecycle
    async def shutdown(self) -> None:
        """Fast, quiet cleanup. GitHub gives only a few seconds on cancel."""
        log.info("Shutting down...")

        async def quiet(coro: Any, timeout: float = 3.0) -> None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(coro, timeout)

        if self.player_task and not self.player_task.done():
            self.player_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self.player_task, 2)
        if self.calls:
            await quiet(self.calls.leave_call(TARGET_CHAT_ID))
            await quiet(self.calls.stop())
        await quiet(self.bot.disconnect())
        await quiet(self.user.disconnect())
        log.info("Shutdown complete.")

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop_event.set)

        self.bot.add_event_handler(
            self.new_message_handler, events.NewMessage(incoming=True)
        )
        self.bot.add_event_handler(self.callback_handler, events.CallbackQuery())

        try:
            await self.user.connect()
            if not await self.user.is_user_authorized():
                raise RuntimeError("SESSION_STRING غير صالحة أو منتهية.")
            self.calls = PyTgCalls(self.user)
            await self.calls.start()
            await self.bot.start(bot_token=BOT_TOKEN)
            log.info(
                "Music bot is running; group=%s youtube=%s; auto-stop in %ss",
                TARGET_CHAT_ID, YOUTUBE_CHAT_ID, MAX_RUNTIME_SECONDS,
            )
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=MAX_RUNTIME_SECONDS
                )
                log.info("Stop signal received.")
            except asyncio.TimeoutError:
                log.info("Runtime limit reached; restarting via workflow.")
        finally:
            await self.shutdown()


def validate_configuration() -> None:
    if API_ID <= 0 or not API_HASH:
        raise RuntimeError("ضع API_ID و API_HASH في قسم CONFIGURATION.")
    if not BOT_TOKEN:
        raise RuntimeError("المتغير BOT_TOKEN غير موجود (GitHub Secret).")
    if not SESSION_STRING:
        raise RuntimeError("المتغير SESSION_STRING غير موجود (GitHub Secret).")
    if not TARGET_CHAT_ID:
        raise RuntimeError("ضع TARGET_CHAT_ID في قسم CONFIGURATION.")
    if not YOUTUBE_CHAT_ID:
        log.warning("YOUTUBE_CHAT_ID فارغ: أمر (شغل + اسم) لن يعمل.")


if __name__ == "__main__":
    validate_configuration()
    asyncio.run(GroupMusicBot().run())
