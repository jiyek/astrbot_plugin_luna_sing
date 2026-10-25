"""让 Luna 唱歌：随机选一首，或者按用户点名的曲子发对应文件。

歌曲以 WAV/MP3 等音频文件放在 ``songs/`` 目录，首次发送时转码成 MP3 缓存到
``cache/``，原始文件不动。用户点名但曲库里没有时，按人格设定回一句，而不是
硬塞一首随机歌。
"""

from __future__ import annotations

import asyncio
import difflib
import os
import random
import re
import subprocess
import sys
import time
import unicodedata
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

# 点歌时要去掉的动词/语气词，长的写在前面，避免只吃掉一半
_FILLER_RE = re.compile(
    r"(?i)(?:"
    r"给我唱|唱给我听|唱一首|唱一个|唱一段|唱首|唱唱|唱|"
    r"来一首|来一个|来首|来点|来|"
    r"放一首|放首|放|点一首|点首|点歌|点|"
    r"听一首|听首|听听看|听听|听|整一首|整首|整|"
    r"我可以|可不可以|能不能|可以|能否|能|"
    r"帮我|给我|替我|为你|为你唱|"
    r"我想要|我想|我要|想听|想|"
    r"请|麻烦|拜托|"
    r"一首|一曲|一支|一个|首|曲|歌|歌曲|音乐|"
    r"这首歌|那首歌|这个|那个|这|那|的|"
    r"你的|你|露娜|luna|"
    r"好吗|好不好|行吗|行不行|吧|呗|呀|啊|嘛|哦|啦|呢|了|"
    r"一下|一遍|"
    r"日语|日文|中文|英文|国语|粤语|二次元|动漫|acg"
    r")"
)
_GENERIC_RE = re.compile(
    r"^(?:好听|随便|都行|都|任意|你决定|你来选|你选|喜欢|最爱|你的|"
    r"来|点|唱|吧|的|歌|曲|音乐)+$"
)
# 剥出来的“歌名”里带这些字，多半是在聊天而不是点歌
_CHATTY_RE = re.compile(r"(?:说|话|聊|讲|告诉|看看|睡|吃|玩|做|陪|夸|骂|叫)")
_PUNCT_RE = re.compile(r"[\s\u3000!-/:-@\[-`{-~。、，．·…〜~！？「」『』【】（）()《》〈〉「」『』—–_]+")

# 看板上的快捷入口：GET /api/plug/astrbot_plugin_luna_sing/open-songs
_OPEN_FOLDER_ROUTE = "/astrbot_plugin_luna_sing/open-songs"

# 点不到歌时的回应（{title} 是用户点的名字）
_MISS_LINES = (
    "「{title}」？曲库里没有这首。……换一首。",
    "没有。你倒是会挑，可惜我没录过。",
    "……不在曲库。想听的话，挑一首我有的。",
    "这首我没有。把歌名换成我有的，再开口。",
)

# 日语歌的中文叫法；值只要能在曲目标题里命中就行（用户还可在配置里追加）
_BUILTIN_ALIASES = {
    "尽情绽放": "咲き誇れ今",
    "如今尽情绽放": "咲き誇れ今",
    "向着看不见的尽头": "果ての見えぬ先に",
    "看不见的尽头": "果ての見えぬ先に",
    "蓝色夜曲": "青いあの日のセレナーデ",
    "蓝色的夜曲": "青いあの日のセレナーデ",
    "那日蓝色的夜曲": "青いあの日のセレナーデ",
    "纯真樱桃": "チェリブラピュア",
    "樱桃纯真": "チェリブラピュア",
    "纯真之樱": "チェリブラピュア",
    "悠久交响曲": "悠久のシンフォニー",
    "永恒交响曲": "悠久のシンフォニー",
    "悠久交响乐": "悠久のシンフォニー",
}


class SingError(RuntimeError):
    """Raised when a song cannot be prepared for delivery."""


class _AddressedFilter(filter.CustomFilter):
    """Only handle messages aimed at the bot."""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return event.is_private_chat() or event.is_at_or_wake_command


def _normalize(text: str) -> str:
    """把文本压成只含字母/假名/汉字的小写形式，便于比较。"""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+", "", folded)


def _ratio(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right).ratio()


def _partial_ratio(left: str, right: str) -> float:
    """短串在长串上滑窗取最高相似度，抵消前缀废话（“唱一首…”）的影响。"""
    if not left or not right:
        return 0.0
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    best = _ratio(short, long)
    for index in range(len(long) - len(short) + 1):
        best = max(best, _ratio(short, long[index : index + len(short)]))
    return best


def _title_of(song: Path) -> str:
    title = _TRACK_PREFIX_RE.sub("", song.stem).strip()
    return title or song.stem


def _title_score(query: str, title: str) -> float:
    """标题与查询的相似度：完全相同 > 包含 > 滑窗部分匹配。"""
    if not title:
        return 0.0
    if query == title:
        return 1.0
    if title in query:
        return 0.95
    if query in title and len(query) >= 3:
        return 0.9
    return _partial_ratio(query, title) * 0.95


def _find_by_fragment(fragment: str, songs: list[Path]) -> Path | None:
    needle = _normalize(fragment)
    if not needle:
        return None
    for song in songs:
        if needle in _normalize(_title_of(song)):
            return song
    return None


def _match_song(
    request: str,
    songs: list[Path],
    aliases: dict[str, str] | None = None,
) -> tuple[Path, float] | None:
    """在曲库里找最匹配的一首，返回 (歌曲, 置信度)；不够像就返回 None。"""
    query = _normalize(request)
    if not query:
        return None
    best_song: Path | None = None
    best_score = 0.0
    for song in songs:
        score = _title_score(query, _normalize(_title_of(song)))
        if score > best_score:
            best_song, best_score = song, score
    for alias, fragment in (aliases or {}).items():
        name = _normalize(alias)
        if not name:
            continue
        if query == name:
            score = 0.98
        elif name in query and len(name) >= 2:
            score = 0.96
        else:
            score = _partial_ratio(query, name) * 0.92
        if score <= best_score:
            continue
        target = _find_by_fragment(fragment, songs)
        if target is not None:
            best_song, best_score = target, score
    if best_song is not None and best_score >= 0.72:
        return best_song, best_score
    return None


def _requested_title(text: str) -> str:
    """从一句话里剥出用户点的歌名；只是泛泛想听歌时返回空串。"""
    stripped = _FILLER_RE.sub(" ", text)
    stripped = _PUNCT_RE.sub(" ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if len(_normalize(stripped)) < 2:
        return ""
    if _GENERIC_RE.match(_normalize(stripped)):
        return ""
    return stripped


def _strip_command(text: str) -> str:
    """去掉指令前缀（“/唱歌”“唱歌”“sing”等），只留参数。"""
    stripped = re.sub(r"^[/／!！]\s*", "", text.strip())
    for name in ("唱一首", "唱歌", "来首歌", "sing"):
        if stripped.lower().startswith(name.lower()):
            return stripped[len(name) :].strip()
    return stripped


class LunaSingPlugin(Star):
    """Let users ask Luna to sing a random song or a specific one."""

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
        self.miss_reply = str(config.get("miss_reply", "") or "").strip()
        self.aliases = self._build_aliases(config.get("song_aliases", {}))

        root = Path(__file__).resolve().parent
        configured_dir = str(config.get("songs_dir", "") or "").strip()
        if configured_dir:
            songs_dir = Path(configured_dir).expanduser()
            if not songs_dir.is_absolute():
                songs_dir = root / songs_dir
        else:
            songs_dir = root / "songs"
        self._songs_dir = songs_dir
        self._cache_dir = root / "cache"
        self._songs_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_song: dict[str, str] = {}
        self._last_time: dict[str, float] = {}
        self._convert_lock = asyncio.Lock()
        self._ffmpeg = self._find_ffmpeg()
        if not self._ffmpeg:
            logger.warning("[LunaSing] 未找到 ffmpeg，无法把 WAV 转成 MP3")
        self._register_open_folder_api()

    def _register_open_folder_api(self) -> None:
        """注册看板接口：在服务器所在机器上打开曲库目录。"""
        try:
            self.context.register_web_api(
                _OPEN_FOLDER_ROUTE,
                self._open_songs_folder,
                ["GET"],
                "打开 Luna 唱歌的曲库文件夹",
            )
        except Exception as exc:  # noqa: BLE001 - 老版本 AstrBot 没有这个接口
            logger.warning("[LunaSing] 注册「打开曲库目录」接口失败: %s", exc)

    async def _open_songs_folder(self, **_: object) -> dict:
        """在资源管理器里打开曲库目录（看板配置页的快捷入口）。"""
        path = self._songs_dir
        try:
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                # 直接起 explorer 比 ShellExecute 更宽松：实例以低完整性运行，
                # os.startfile 会被拒绝（WinError 5），CreateProcess 一般没问题。
                subprocess.Popen(["explorer.exe", str(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001 - 返回错误给看板
            logger.warning("[LunaSing] 打开曲库目录失败: %s", exc)
            return {
                "status": "error",
                "message": f"打开失败：{exc}",
                "data": {"path": str(path)},
            }
        return {
            "status": "ok",
            "message": f"已打开：{path}",
            "data": {"path": str(path)},
        }

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

    def _pick_song(self, session: str, songs: list[Path]) -> Path:
        if not songs:
            raise SingError("歌曲目录里没有可用的音频文件")
        last = self._last_song.get(session)
        if self.avoid_repeat and len(songs) > 1:
            candidates = [song for song in songs if song.name != last] or songs
        else:
            candidates = songs
        return random.choice(candidates)

    def _available_titles(self, songs: list[Path]) -> str:
        return "、".join(_title_of(song) for song in songs)

    @staticmethod
    def _build_aliases(configured) -> dict[str, str]:
        """内置中文叫法 + 配置里的追加项；配置项优先。"""
        aliases = dict(_BUILTIN_ALIASES)
        if isinstance(configured, dict):
            aliases.update(
                {str(key): str(value) for key, value in configured.items() if key and value}
            )
        return aliases

    def _miss_message(self, request: str, songs: list[Path]) -> str:
        title = request or "这首"
        if self.miss_reply:
            catalog = self._available_titles(songs)
            return self.miss_reply.replace("{title}", title).replace("{songs}", catalog)
        return random.choice(_MISS_LINES).replace("{title}", title)

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

    async def _serve(
        self,
        event: AstrMessageEvent,
        text: str = "",
    ) -> AsyncIterator[MessageEventResult]:
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
            songs = self._songs()
            requested = _requested_title(text) if text else ""
            if requested:
                matched = _match_song(requested, songs, self.aliases) or _match_song(
                    text, songs, self.aliases
                )
                if matched is None:
                    yield event.plain_result(self._miss_message(requested, songs))
                    return
                song, score = matched
                logger.debug(
                    "[LunaSing] 点歌 %r -> %s (%.2f)", requested, song.name, score
                )
            else:
                song = self._pick_song(session, songs)
            title = _title_of(song)
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
            (f"🎤 就这首：{title}\n" if requested else "🎤 随机唱一首\n")
            + f"🎵 {title}\n"
            + f"📦 MP3 · {self.bitrate} · {size_mb:.1f} MB"
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
        """唱歌；带上歌名就是点歌，例如“/唱歌 Desire”。"""

        text = _strip_command(event.get_message_str())
        async for result in self._serve(event, text):
            yield result

    @filter.custom_filter(_AddressedFilter, priority=100)
    async def sing_natural_language(self, event: AstrMessageEvent):
        """自然语言点歌，例如“唱首 Desire”“来首好听的”。"""

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

        # 要么是“想听歌”这类泛请求，要么直接报了一首曲库里的歌名
        requested = _requested_title(text)
        try:
            matched = (
                _match_song(requested, self._songs(), self.aliases)
                if requested
                else None
            )
        except SingError:
            return
        if requested and matched is None and _CHATTY_RE.search(requested):
            # 例如“想听你说说话”：像聊天，不在插件里接，交给主线回答
            return
        if not _SING_RE.search(text) and matched is None:
            return

        # Yield the result before stopping the event, so the respond stage can
        # still deliver the audio/file component.
        try:
            async for result in self._serve(event, text):
                yield result
        finally:
            event.stop_event()
