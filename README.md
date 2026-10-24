# Luna 唱歌

从插件内置的 Luna 歌曲库里随机选一首发送。

## ⚠️ 仓库里不含歌曲文件

12 首原始 WAV 合计约 **467MB**，超过 GitHub 的仓库/单文件限制，因此**没有放进 Git**。
clone 下来后 `songs/` 是空的，需要自己把音频放进去，否则 `/唱歌` 会报“歌曲目录里没有可用的音频文件”。

放置方式：

```text
astrbot_plugin_luna_sing/
└── songs/
    ├── 01 Progress Light.wav
    ├── 02 Spring Day's Cherish.wav
    └── ...
```

- 支持格式：`.wav` `.mp3` `.flac` `.m4a` `.ogg`，按文件名排序。
- 文件名**随意**，`01 ` `02-` 这类数字前缀会被自动去掉当歌名显示。
- 放几首都可以，至少 1 首；`songs/` 目录不存在时会自动创建。

## 歌曲

原始 WAV 的清单如下（仅供对照，文件本身不入库）：

```text
01 Progress Light.wav
02 Spring Day's Cherish.wav
03 咲き誇れ今 〜果ての見えぬ先に〜.wav
04 Timeless Hope.wav
05 Sing.wav
06 Destiny Sky.wav
07 青いあの日のセレナーデ.wav
08 チェリブラピュア.wav
09 Absolute Moonlight.wav
10 悠久のシンフォニー.wav
11 Desire.wav
12 Shiny Moon.wav
```

歌曲约 3–5 分钟，原始 WAV 体积较大。插件首次播放某首歌时会转成 `192k` MP3 并缓存到 `cache/`，之后直接复用，不会修改原始 WAV。

## 触发方式

指令：

```text
/唱歌
/唱一首
/sing
/来首歌
```

自然语言（私聊或 @bot）：

```text
唱首歌
来首好听的
给我唱一首
来点音乐
唱一个
```

每次随机选一首；默认避免同一会话连续重复上一首。

## 配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `send_mode` | `file` | `file` 发送 MP3 文件；`record` 发送语音消息 |
| `mp3_bitrate` | `192k` | MP3 码率：`128k` / `192k` / `320k` |
| `avoid_repeat` | `true` | 同一会话避免连续重复 |
| `cooldown_seconds` | `0` | 点歌冷却，`0` 表示不限制 |

## 为什么默认发文件而不是语音

歌曲完整长度约 3–5 分钟。QQ 语音消息对长音频限制很严，直接发 `Record` 很容易失败或只播放一小段。因此默认把 MP3 作为文件发送；如果只想尝试语音消息，可以把 `send_mode` 改成 `record`。

## 依赖

`requirements.txt` 中的 `imageio-ffmpeg` 会在插件加载时由 AstrBot 安装，用于 WAV → MP3 转码。插件代码在导入阶段就依赖它，所以缺少依赖时会触发 AstrBot 的插件依赖安装流程。

## 说明

- 发送前会先发一条“正在准备”的进度消息。
- 最终消息关闭了文本转图片，避免摘要被渲染成图片时把音频附件丢掉。
- 自然语言触发只处理私聊或 @bot 的群消息，群聊中普通提到“唱歌”不会抢话。
- 新增歌曲后重载插件即可识别。
