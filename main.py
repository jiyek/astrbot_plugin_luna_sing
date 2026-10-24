"""Randomly send one of Luna's bundled songs.

Songs are stored as WAV files inside ``songs/``.  On first use a song is
transcoded to MP3 for delivery; WAV originals stay untouched.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path

import astrbot.api.message_components as Comp
import imageio_ffmpeg
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star

_SUPPORTED_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}
_TRACK_PREFIX_RE = re.compile(r"^\d+[\s._-]*")
_SING_RE = re.compile(
    r"(?is)(?:"
    r"(?=.*(?:唱|来首|来一首|来点|放首|放一首|点歌|想听|整首|整一首))"
    r"(?=.*(?:歌|曲|音乐|music|song|听听|听))"
    r"|"
    r"(?=.*(?:唱一首|唱一个|唱首|唱两句|唱几句|唱一段|唱一下|"
    r"来一首|来首|放一首|整一首))"
    r")"
)
_NEGATIVE_RE = re.compile(r"(?:别|不要|不用|停止|闭嘴).{0,4}(?:唱|歌)")


class SingError(RuntimeError):
    """Raised when a song cannot be prepared for delivery."""


class _AddressedFilter(filter.CustomFilter):
    """Only handle messages aimed at the bot."""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return event.is_private_chat() or event.is_at_or_wake_command


class LunaSingPlugin(Star):
    """Let users ask Luna to sing a random bundled song."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.enabled = self._as_bool(config.get("enabled", True))
        self.send_mode = str(config.get("send_mode", "file") or "file").strip()
        if self.send_mode not in {"file", "record"}:
            self.send_mode = "file"
        self.bitrate = str(config.get("mp3_bitrate", "192k") or "192k").strip()
        if self.bitrate not in {"128k", "192k", "320k"}:
            self.bitrate = "192k"
        self.avoid_repeat = self._as_bool(config.get("avoid_repeat", True))
        self.cooldown_seconds = self._as_float(
            config.get("cooldown_seconds", 0),
            0.0,
            0.0,
            120.0,
        )

        root = Path(__file__).resolve().parent
        self._songs_dir = root / "songs"
        self._cache_dir = root / "cache"
        self._songs_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_song: dict[str, str] = {}
        self._last_time: dict[str, float] = {}
        self._convert_lock = asyncio.Lock()
        self._ffmpeg = self._find_ffmpeg()
        if not self._ffmpeg:
            logger.warning("[LunaSing] 未找到 ffmpeg，无法把 WAV 转成 MP3")

    @staticmethod
    def _as_bool(value) -> bool:
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "off"}
        return bool(value)

    @staticmethod
    def _as_float(value, default: float, minimum: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        return min(maximum, max(minimum, number))

    @staticmethod
    def _find_ffmpeg() -> str | None:
        try:
            path = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # noqa: BLE001 - fall back to the WAV file
            return None
        return path if Path(path).is_file() else None

    def _songs(self) -> list[Path]:
        try:
            songs = [
                path
                for path in self._songs_dir.iterdir()
                if path.is_file() and path.suffix.lower() in _SUPPORTED_SUFFIXES
            ]
        except OSError as exc:
            raise SingError(f"读取歌曲目录失败：{exc}") from exc
        return sorted(songs, key=lambda item: item.name.casefold())

    def _pick_song(self, session: str) -> Path:
        songs = self._songs()
        if not songs:
            raise SingError("歌曲目录里没有可用的音频文件")
        last = self._last_song.get(session)
        if self.avoid_repeat and len(songs) > 1:
            songs = [song for song in songs if song.name != last] or songs
        return random.choice(songs)

    @staticmethod
    def _title(song: Path) -> str:
        title = _TRACK_PREFIX_RE.sub("", song.stem).strip()
        return title or song.stem

    async def _safe_send(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(event.plain_result(text))
        except Exception as exc:  # noqa: BLE001 - progress messages are best effort
            logger.debug("[LunaSing] 发送进度消息失败: %s", exc)

    async def _to_mp3(self, song: Path) -> Path:
        """Convert a song to a cached MP3, returning the original MP3 if any."""

        if song.suffix.lower() == ".mp3":
            return song
        if not self._ffmpeg:
            raise SingError(
                "没有找到 ffmpeg，无法把 WAV 转成可发送的 MP3；"
                "请重载插件以安装 imageio-ffmpeg"
            )

        safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", song.stem)
        output = self._cache_dir / f"{safe_stem}_{self.bitrate}.mp3"
        try:
            if output.is_file() and output.stat().st_mtime >= song.stat().st_mtime:
                return output
        except OSError:
            pass

        async with self._convert_lock:
            try:
                if output.is_file() and output.stat().st_mtime >= song.stat().st_mtime:
                    return output
            except OSError:
                pass

            part = output.with_name(f"{output.name}.part")
            log_path = output.with_name(f"{output.name}.ffmpeg.log")
            command = [
                self._ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(song),
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                self.bitrate,
                "-map_metadata",
                "-1",
                "-id3v2_version",
                "3",
                "-f",
                "mp3",
                str(part),
            ]
            try:
                log_file = log_path.open("wb")
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=log_file,
                )
            except OSError as exc:
                try:
                    log_file.close()
                except (NameError, OSError):
                    pass
                part.unlink(missing_ok=True)
                log_path.unlink(missing_ok=True)
                raise SingError(f"启动 ffmpeg 失败：{exc}") from exc

            try:
                await asyncio.wait_for(process.wait(), timeout=180)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                part.unlink(missing_ok=True)
                log_path.unlink(missing_ok=True)
                raise SingError("歌曲转码超时") from None
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                part.unlink(missing_ok=True)
                log_path.unlink(missing_ok=True)
                raise
            finally:
                log_file.close()

            if process.returncode != 0:
                try:
                    detail = log_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).strip()
                except OSError:
                    detail = ""
                part.unlink(missing_ok=True)
                log_path.unlink(missing_ok=True)
                raise SingError(f"ffmpeg 转码失败：{detail[-300:] or '未知错误'}")
            log_path.unlink(missing_ok=True)
            if not part.is_file() or part.stat().st_size <= 0:
                part.unlink(missing_ok=True)
                raise SingError("ffmpeg 没有生成有效的 MP3")
            part.replace(output)
            return output

    async def _serve(self, event: AstrMessageEvent) -> AsyncIterator[MessageEventResult]:
        if not self.enabled:
            yield event.plain_result("唱歌功能暂时关闭了。")
            return

        session = event.unified_msg_origin
        now = time.monotonic()
        if self.cooldown_seconds > 0:
            last_time = self._last_time.get(session, 0.0)
            if now - last_time < self.cooldown_seconds:
                wait = self.cooldown_seconds - (now - last_time)
                yield event.plain_result(f"刚唱过啦，{wait:.0f} 秒后再来吧。")
                return

        try:
            song = self._pick_song(session)
            title = self._title(song)
            await self._safe_send(event, f"🎤 正在准备：{title}…")
            audio = await self._to_mp3(song)
        except SingError as exc:
            logger.warning("[LunaSing] 准备歌曲失败: %s", exc)
            yield event.plain_result(f"❌ {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - keep the bot alive
            logger.error("[LunaSing] 准备歌曲异常: %s", exc, exc_info=True)
            yield event.plain_result(f"❌ 唱歌失败：{exc}")
            return

        self._last_song[session] = song.name
        self._last_time[session] = now
        size_mb = audio.stat().st_size / 1024 / 1024
        summary = (
            "🎤 随机唱一首\n"
            f"🎵 {title}\n"
            f"📦 MP3 · {self.bitrate} · {size_mb:.1f} MB"
        )
        if self.send_mode == "record":
            component = Comp.Record.fromFileSystem(path=str(audio))
        else:
            component = Comp.File(name=f"{title}.mp3", file=str(audio))

        result = event.chain_result([Comp.Plain(summary), component])
        # Do not let AstrBot's T2I decorator replace the whole chain with one
        # image, which would drop the audio attachment.
        result.use_t2i(False)
        yield result

    @filter.command("唱歌", alias={"唱一首", "sing", "来首歌"})
    async def sing_command(self, event: AstrMessageEvent):
        """随机唱一首歌。"""

        async for result in self._serve(event):
            yield result

    @filter.regex(_SING_RE, priority=100)
    @filter.custom_filter(_AddressedFilter, priority=100)
    async def sing_natural_language(self, event: AstrMessageEvent):
        """让用户用自然语言点歌，例如“唱首歌”“来首好听的”。"""

        if not self.enabled:
            return
        text = event.get_message_str().strip()
        if not text or text.startswith(("/", "／")):
            return
        if _NEGATIVE_RE.search(text):
            return
        if event.is_stopped():
            return
        if not (event.is_private_chat() or event.is_at_or_wake_command):
            return

        # Yield the result before stopping the event, so the respond stage can
        # still deliver the audio/file component.
        try:
            async for result in self._serve(event):
                yield result
        finally:
            event.stop_event()
