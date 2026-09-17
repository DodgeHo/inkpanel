# -*- coding: utf-8 -*-
"""
music —— 把网易云音乐 PC 端变成墨水屏能看、能遥控的"歌词屏"

设计原则（都是实测换来的，别随手改）：

1. **不侵入客户端**：不装插件、不上 CDP、不改配置、不写 webdb.dat。
2. **控制靠"注入客户端自己的全局快捷键"**：网易云默认注册了
   Ctrl+Alt+P / ←→ / ↑↓ / L，我们用 SendInput 注入这些组合键即可。
   注入 ≠ 注册，所以不受 ERROR_HOTKEY_ALREADY_REGISTERED(1409) 影响。
3. **状态只能自己拼**（客户端不发布 SMTC）：
   · 当前曲目   ← webdb.dat 的 historyTracks 表，按 playtime DESC 取首行
   · 起播时刻   ← 上面那行的 playtime
   · 播放/暂停  ← WASAPI 会话 state + 端点峰值电平
   · 进度       ← 自建时钟（起播时刻 + 总时长），切歌时强对齐
4. **歌单切换在 Windows 上做不到**：`orpheus://playlist/{id}` 实测无效
   （handler 进程确实被拉起又 0.35s 退出，但客户端毫无反应；
     cloudmusic.dll 里的 orpheus:// 全是内部页面 native/start.html、native/lrc.html）。
   所以歌单只读不切。

对外最常用的入口是 :mod:`music.nowplaying` 与 :mod:`music.service`。
"""
from . import paths  # noqa: F401

__version__ = "1.0.0"
__all__ = ["paths"]
