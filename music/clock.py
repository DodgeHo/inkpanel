# -*- coding: utf-8 -*-
"""
clock.py —— 自建播放进度时钟

为什么必须自己算：
    网易云 PC 端**不发布 SMTC**（实测枚举 0 个会话），也不暴露任何 player/state 接口
    （webdb.dat 的 requestCache 里 165 个 /eapi/* 缓存响应中没有一个是 player/state）。
    所以"现在播到第几秒"只能拼出来。

算法：
    进度 = (now - t0 - paused_ms + offset_ms) 夹到 [0, duration]
      t0         当前曲目的起播墙钟（historyTracks.playtime）
      paused_ms  累计暂停时长
      offset_ms  人工校准偏移（拖了进度条之后用手指调）

对齐点（重要）：
    **切歌** 是首要对齐点：id 一变就归零重排。
    **playingCount 新行** 是第二对齐点（实测非常有用）：
        客户端每次"一首停下来"就往 playingCount 写一行，
        playDuration=这一轮实际播了多久（秒），updateTime=停下时刻（毫秒）。
        → 可以算出这一轮真正的起播时刻 = updateTime - playDuration*1000，
          比按切歌时刻估算更准；
        → 单曲循环时 historyTracks 不新增行，但 playingCount 会新增，
          于是循环也能被发现。
    **循环回绕**：进度顶到 duration 且仍在出声 → 认定循环，t0 往前滚一个 duration。
    **检索补 id**：窗口标题反查不到 id 的曲目（不在本地曲库），
        service 会拿歌名去线上检索补一个 id（`set_resolved_id`），
        于是 playingCount 校准对这类歌也生效。

其余情况（手动拖进度条）自建时钟发现不了，靠 `nudge()` / `seek_to()` 手工回正。

刷新调度：
    `next_change_ms()` 告诉调用方"再过多少毫秒画面才会变"
    （下一句歌词出现 / 进度条数字跳秒 / 歌曲结束），
    墨水屏就睡到那一刻再拉图，而不是傻傻地定时轮询。

用法：
    c = ProgressClock()
    c.update(); c.position_ms
    python -m music.clock --watch      # 终端里实时看
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

from . import audio_state, nowplaying

LOOP_WRAP_HOLD_S = 1.2      # 进度顶到末尾且持续出声多久，认定为单曲循环

# 「可见延迟」：设备从**取到图**到**屏上真的变了**要花多久。
#   请求 20~200ms + 解码 ~60ms + 局刷波形 100~300ms ≈ 0.5s，留点余量取 600。
#
# ⚠️ 这个数**不是**用来"提前唤醒"的 —— 试过，没用（见 next_change_ms 的 docstring）。
#    取图那一刻内容就定死了，早醒只会取到旧内容。它是一条**物理下限**：
#    对齐做到底，屏上也至少比歌晚这么多。所以这里只用来：
#      1) 上报给设备/调试（sync.lead_ms），让"看起来慢"有据可查；
#      2) 记录实测值，将来换更快的取图方式（比如长连接推送）时可以调小。
LEAD_MS = int(os.environ.get("H9DASH_LEAD_MS", "600"))


class ProgressClock:
    # 可见延迟（见模块级 LEAD_MS 的说明）。放成类属性，便于测试里覆盖。
    LEAD_MS = LEAD_MS

    def __init__(self, detector: Optional[audio_state.PlayDetector] = None,
                 track_reader: Callable[[], nowplaying.Track] = nowplaying.current_track,
                 use_play_events: bool = True):
        self.detector = detector or audio_state.PlayDetector()
        self.track_reader = track_reader
        self.use_play_events = use_play_events

        self.track: Optional[nowplaying.Track] = None
        self.playing = False

        self._t0 = 0.0             # 起播墙钟（秒，浮点）
        self._paused_s = 0.0       # 累计暂停（秒）
        self._offset_s = 0.0       # 人工偏移（秒）
        self._paused_at = 0.0      # 本次暂停开始时刻（0 = 未暂停）
        self._song_changed = False
        self._last_now = 0.0

        self._last_event_seq = -1  # playingCount 最近见过的自增 id
        self._at_end_since = 0.0   # 进度顶到末尾的开始时刻
        self.calibrations = 0      # 被 playingCount 校准过几次（调试用）
        self._resolved_id = ""     # 由"按歌名检索"补出来的 id（见 set_resolved_id）

    def set_resolved_id(self, song_id: str) -> None:
        """外部（service 按歌名检索）补出来的曲目 id。

        为什么不直接写 self.track.id：
            track.key 用 id 参与"换歌判定"，中途把 id 塞进去会让 key 从
            "nm:..." 变成 "id:..."，下一帧就被当成切了歌 → 进度归零。
        所以这里单独存，只用于 playingCount 校准。
        """
        sid = str(song_id or "")
        if sid == self._resolved_id:
            return
        self._resolved_id = sid
        self._last_event_seq = -1      # 换 id 了，等它的下一次 stop 事件再校准

    def _cur_rid(self) -> str:
        if self._resolved_id:
            return self._resolved_id
        return str(self.track.id) if self.track and self.track.id else ""

    # ---------------------------------------------------------------- 核心

    def update(self) -> dict:
        """采一次样，返回当前状态快照。"""
        now = time.time()
        self._last_now = now

        # 1) 曲目（切歌 = 首要强对齐点）
        t = self.track_reader()
        if t.key and (self.track is None or t.key != self.track.key):
            self.track = t
            # start_ms 有值就用它（historyTracks 的 playtime，最准）；
            # 只有窗口标题时 start_ms=0，那就把"第一次看见它"当作起播时刻。
            self._t0 = t.start_at if t.start_ms else now
            self._paused_s = 0.0
            self._offset_s = 0.0
            self._paused_at = 0.0
            self._at_end_since = 0.0
            self._song_changed = True
            self._last_event_seq = -1     # 换歌了，之前的 stop 事件不再相干
        elif self.track is None:
            self.track = t
            self._t0 = t.start_at if t.start_ms else now
            self._song_changed = True
        else:
            if t.duration_ms:
                self.track.duration_ms = t.duration_ms

        # 2) playingCount 校准（单曲循环 / 精确对齐）
        if self.use_play_events:
            self._apply_play_event(now)

        # 3) 播放 / 暂停（峰值电平为主，见 audio_state 的说明）
        state = self.detector.update()
        was = self.playing
        self.playing = (state == "playing")

        if self.playing and not was:
            if self._paused_at:
                self._paused_s += max(0.0, now - self._paused_at)
                self._paused_at = 0.0
        elif not self.playing and was:
            self._paused_at = now
        elif not self.playing and was is False and self._paused_at == 0.0:
            self._paused_at = now

        # 4) 单曲循环回绕
        self._maybe_wrap_loop(now)

        return self.snapshot()

    def _apply_play_event(self, now: float) -> None:
        """看 playingCount 有没有新行；有就拿它精确校准。"""
        ev = nowplaying.last_play_event()
        rid = self._cur_rid()
        if not ev or not self.track or not rid:
            return
        seq = ev["seq"]
        if seq == self._last_event_seq:
            return
        first_seen = (self._last_event_seq < 0)
        self._last_event_seq = seq

        if first_seen:
            return                                  # 启动时只记基线，不跳变
        if ev["resource_id"] != rid:
            return                                  # 是别的歌停下来了，不管

        # 这一轮的起播时刻 = 停下时刻 - 实际播了多久
        start_ms = ev["stop_ms"] - ev["duration_s"] * 1000
        self._t0 = start_ms / 1000.0
        self._paused_s = 0.0
        self._offset_s = 0.0
        self._paused_at = ev["stop_ms"] / 1000.0     # 写行的那一刻它停下来了
        self._at_end_since = 0.0
        self.calibrations += 1

    def _maybe_wrap_loop(self, now: float) -> None:
        """进度已经超过一轮却还在出声 → 单曲循环（或进程起晚了），把 t0 往前滚。

        直接按"超出了几个 duration"整轮推进，而不是一次一轮，
        否则进程启在歌曲中途时会把进度一直夹在末尾。
        """
        if not self.track or not self.track.duration_ms or not self.playing:
            self._at_end_since = 0.0
            return
        dur_s = self.track.duration_ms / 1000.0
        if dur_s <= 1.0:
            return
        raw = now - self._t0 - self._paused_s
        if raw > dur_s:
            loops = int(raw // dur_s)
            self._t0 += loops * dur_s
            self.calibrations += 1
            self._at_end_since = 0.0

    def snapshot(self) -> dict:
        dur = max(0, self.track.duration_ms if self.track else 0)
        pos = self.position_ms
        return {
            "id": self.track.id if self.track else "",
            "resolved_id": self._resolved_id,
            "name": self.track.name if self.track else "",
            "artist": self.track.artist if self.track else "",
            "album": self.track.album if self.track else "",
            "label": self.track.label() if self.track else "",
            "duration_ms": dur,
            "position_ms": pos,
            "playing": self.playing,
            "song_changed": self.take_song_changed(),
            "offset_ms": int(self._offset_s * 1000),
            "drifted": abs(self._offset_s) > 0.001,
            "calibrations": self.calibrations,
        }

    def take_song_changed(self) -> bool:
        v = self._song_changed
        self._song_changed = False
        return v

    # ------------------------------------------------------------ 进度计算

    @property
    def position_ms(self) -> int:
        if not self.track:
            return 0
        now = self._last_now or time.time()
        elapsed = now - self._t0
        if not self.playing and self._paused_at:
            elapsed = self._paused_at - self._t0 - self._paused_s
        else:
            elapsed = elapsed - self._paused_s
        pos = (elapsed + self._offset_s) * 1000.0
        dur = max(0, self.track.duration_ms)
        if dur:
            pos = min(pos, dur)
        else:
            pos = min(pos, 6 * 3600 * 1000.0)   # 时长未知时也别让它无限涨
        return int(max(0.0, pos))

    def nudge(self, seconds: float) -> int:
        """手工校准进度（拖了进度条之后用）。返回新的进度毫秒。"""
        self._offset_s += float(seconds)
        # 别让偏移把进度顶到区间外
        if self.track and self.track.duration_ms:
            pos = self.position_ms
            if pos <= 0 or pos >= self.track.duration_ms:
                over = (pos - self.track.duration_ms) if pos >= self.track.duration_ms else pos
                self._offset_s -= over / 1000.0
        return self.position_ms

    def realign(self) -> int:
        """重新对齐：把当前时刻当作这一句的起点，清掉累计偏移与暂停累计。"""
        self._t0 = time.time()
        self._paused_s = 0.0
        self._paused_at = 0.0
        self._offset_s = 0.0
        return self.position_ms

    def seek_to(self, position_ms: int) -> None:
        """强行把进度设到某个值（对应用户点进度条）。"""
        if not self.track:
            return
        now = time.time()
        self._paused_s = 0.0
        self._paused_at = 0.0 if self.playing else now
        self._offset_s = 0.0
        self._t0 = now - max(0, int(position_ms)) / 1000.0

    # ------------------------------------------------------- 下次变化时刻

    def next_change_ms(self, lyric_next_ms: Optional[int] = None,
                       max_wait_ms: int = 3000,
                       lyric_disabled: bool = False) -> int:
        """距离"设备该醒来取图"还有多少毫秒。

        lyric_next_ms: 下一句歌词出现的时间（曲内毫秒），没有就传 None。
        lyric_disabled: True 时忽略"下一句"这个候选（比如歌词面板没开）。

        这个值是给**设备**用的：它拿到就 sleepThenTick()，醒来后取图贴上去。
        所以返回的是"休眠时长"，不是"画面内容何时过时"。

        ══════════════════════════════════════════════════════════════
        字画同步：唯一有效的做法是「把取图时刻对齐到歌词时间戳」
        ══════════════════════════════════════════════════════════════

        先说清一件事，不然后面每个决定都会做错：
        **"早醒/提前量"并不能让屏上的字提前。**
        设备取到的那一帧，反映的是**取图那一刻**的曲内位置。
        歌词 100.9s 出现，你在 100.3s 醒来取图 —— 拿到的还是旧句子，
        屏上什么都不会变。只有取图时刻真的 ≥ 100.9s，帧里才有新句子。

        所以问题的关键是：**哪一次取图会跨过歌词？**
        常规节奏是"每秒醒一次"，唤醒序列 98.0 / 99.0 / 100.0 / 101.0：
            100.0 那帧 → pos=100.0 < 100.9 → 还是旧句子（白跑一趟）
            101.0 那帧 → 才含新句子 → 屏上 101.0 + 传输0.6 ≈ 101.6s
        对 100.9s 的歌词来说，这就是**晚了 700ms**。
        而且这 700ms 里，有 **0~1000ms 纯粹是"整秒量化"造成的浪费** ——
        歌词 100.9s 却被量化到 101.0s 才去取。

        → 正解：**把"跨过歌词的那一次唤醒"直接对齐到歌词时间戳本身。**
           不早、不晚，就在 100.9s 那一刻取图。
           实测（_align_fix.py）：最坏从 1500ms 降到 600ms，平均从 933ms 降到 600ms。

        那剩下的 600ms 是什么？
            取图之后还要走 HTTP(20~200ms)+解码(~60ms)+墨水屏波形(100~300ms)，
            实测 ≈0.6s。这 0.6s 是**"看到"的固有代价**，物理上消不掉 ——
            除非让歌也晚 0.6s 播（不可能）。所以对齐只负责把"量化浪费"清零，
            把误差压到这条物理下限。

        ⚠️ 三个踩过的坑（都写在这里，别再走一遍）：
            1. **不能对"整秒边界"做提前唤醒**。整秒的剩余时间永远 ≤1000ms，
               "提前"会让它每秒醒两次 → 墨水屏刷新率永久翻倍（实测 30 秒 60 次），
               而对"数字跳秒"来说多刷这一次毫无意义。
            2. **"歌词 - LEAD" 不能当每轮重算的目标**。它是固定值，
               提前醒来后再算还是它、而现在已经越过它 → 永远返回 50ms 空转
               （实测 4.75 秒唤醒 24 次）。所以提前只能是**一次性决定**，
               用 self._lead_used_for 记住已经为哪一句对齐过。
            3. **对齐目标要落在歌词"之后一点点"，不能落在"之前"**。
               落在之前那一帧不含新句子，等于没做（这就是第一版"提前量"
               实测 0 改善的原因）。

        实现：把候选死线里"歌词行"直接当作一个常规唤醒点，
        和整秒边界取 min。谁先到就醒谁 —— 一旦某次唤醒落在歌词行上，
        那一帧就是新句子，屏上时间 = 歌词时刻 + 0.6s（物理下限）。
        """
        now = time.time()
        pos = self.position_ms
        dur = self.track.duration_ms if self.track else 0

        # 下一句歌词的绝对时刻（曲内毫秒），没有就是 None
        lyric_at = None
        if (not lyric_disabled and lyric_next_ms is not None
                and lyric_next_ms > pos):
            lyric_at = int(lyric_next_ms)

        # ---- 候选死线 ----
        lines = []
        if not self.playing:
            lines.append(pos + 2000)              # 暂停：定期看看有没有恢复
        else:
            lines.append((pos // 1000) * 1000 + 1000)   # 下一个整秒
            if lyric_at is not None:
                # 歌词行本身就是一个唤醒点。它通常落在两个整秒之间，
                # 于是会把"跨过歌词"的那一次唤醒从下一个整秒**拉回来**到
                # 歌词时刻 —— 这正是字画同步要的那一下。
                lines.append(lyric_at)
            if dur and dur > pos:
                lines.append(int(dur))            # 歌曲结束

        lines.append(pos + max_wait_ms)           # 兜底上限
        return int(max(50, min(lines) - pos))


# ===================================================================== CLI

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="每秒打印一次")
    ap.add_argument("--seconds", type=int, default=20)
    a = ap.parse_args()

    c = ProgressClock()
    if not a.watch:
        s = c.update()
        print("%s  %d/%d ms  playing=%s" % (s["label"], s["position_ms"], s["duration_ms"], s["playing"]))
    else:
        for _ in range(a.seconds):
            s = c.update()
            print("%s pos=%6.1fs/%-6.1fs playing=%-5s 下次变化 %4dms%s" % (
                time.strftime("%H:%M:%S"), s["position_ms"] / 1000.0, s["duration_ms"] / 1000.0,
                s["playing"], c.next_change_ms(),
                "  [切歌]" if s["song_changed"] else ""))
            time.sleep(1.0)
