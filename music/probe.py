# -*- coding: utf-8 -*-
"""
probe.py —— 一键体检：把音乐面板依赖的每一条通道都验一遍

**只读**，不注入按键、不切歌单、不改任何东西（想测按键请用 media_keys）。

用法：
    python -m music.probe              # 一次性体检
    python -m music.probe --watch 30   # 连续观察 30 秒（每 1 秒一行）
    python -m music.probe --keys       # 额外做热键占用探测（会短暂注册热键）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import audio_state, lyrics, nowplaying, paths, playlist, clock, winapi as w

# 这个模块满屏 ✓ ✗ ─ 和中文，GBK 控制台下会直接 UnicodeEncodeError 打挂体检。
try:
    from dashboard.render import fix_console_encoding
    fix_console_encoding()
except Exception:
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass


def _c(code, text):
    """超简单的着色，管道输出时自动退化。"""
    if not sys.stdout.isatty():
        return text
    return "\033[%dm%s\033[0m" % (code, text)


OK = lambda s: _c(32, s)      # noqa: E731
BAD = lambda s: _c(31, s)     # noqa: E731
WARN = lambda s: _c(33, s)    # noqa: E731
DIM = lambda s: _c(90, s)     # noqa: E731


# ===================================================================== 各项检查

def check_paths() -> bool:
    print("─" * 68)
    print("1. 数据位置")
    home = paths.ncm_home()
    ok = home.is_dir()
    print("   客户端数据目录 : %s  %s" % (home, OK("✓") if ok else BAD("✗ 不存在")))
    print("   安装目录       : %s" % (paths.install_dir() or BAD("未找到")))
    db = paths.webdb()
    if db.exists():
        print("   曲库 webdb.dat : %s  %s (%.0f MB)" % (db, OK("✓"), db.stat().st_size / 1048576))
    else:
        print("   曲库 webdb.dat : %s" % BAD("✗ 缺失"))
        ok = False
    for n in ("playingList", "lastTimePlayingList"):
        f = paths.webdata_file(n)
        print("   %-15s: %s" % (n, OK("✓ %d bytes" % f.stat().st_size) if f.exists() else WARN("缺失")))
    # 曾经踩过的坑
    lyr_dir = home / "webdata" / "lyric"
    print("   webdata\\lyric : %s" % (DIM("不存在（这个版本就是这样，歌词必须走接口）")
                                       if not lyr_dir.exists() else DIM("存在")))
    return ok


def check_process() -> bool:
    print("─" * 68)
    print("2. 客户端进程与窗口")
    pids = w.pids_of("cloudmusic.exe")
    if not pids:
        print("   %s 没在跑" % BAD("✗ 网易云音乐"))
        return False
    print("   cloudmusic.exe : %s  (pid %s)" % (OK("✓ 运行中"), ", ".join(map(str, pids))))
    wins = w.windows_of(pids)
    print("   顶层窗口 %d 个：" % len(wins))
    for h, t, p in wins:
        print("       %-8s %s" % (h, t))
    title = w.window_title()
    print("   解析出的标题   : %s" % (OK(repr(title)) if title else WARN("(无)")))
    return True


def check_audio() -> bool:
    print("─" * 68)
    print("3. 音频状态（WASAPI）")
    try:
        rows = audio_state.all_sessions()
    except Exception as e:
        print("   %s %r" % (BAD("✗ 枚举失败"), e))
        return False
    for r in rows:
        mark = OK("◀ 网易云") if r["exe"] == "cloudmusic.exe" else ""
        print("   pid=%-7s %-26s %-9s %s" % (r["pid"], r["exe"], r["state"], mark))
    det = audio_state.PlayDetector()
    for _ in range(3):
        det.update()
        time.sleep(0.3)
    print("   峰值电平       : %s   → 判定 %s" % (
        det.peak, (OK("在播") if det.state == "playing" else WARN("暂停 / 无声"))))
    print("   %s" % DIM("注意：客户端暂停时不关闭音频流，会话 state 仍是 active，"))
    print("   %s" % DIM("      所以判暂停只能靠峰值电平，别用 state。"))
    return True


def check_nowplaying() -> dict:
    print("─" * 68)
    print("4. 当前曲目 / 队列 / 歌单")
    s = nowplaying.snapshot()
    t = s["track"]
    if t["name"]:
        print("   曲目           : %s  %s" % (
            OK(t["label"]), OK("[id %s]" % t["id"]) if t["id"] else
            WARN("[没有 id]")))
        print("   专辑           : %s" % (t["album"] or "-"))
        print("   时长           : %s" % (
            "%.1f 秒" % (t["duration_ms"] / 1000.0) if t["duration_ms"] else WARN("未知")))
        print("   起播时刻       : %s" % (
            time.strftime("%m-%d %H:%M:%S", time.localtime(t["start_ms"] / 1000.0))
            if t["start_ms"] else WARN("未知（只从标题认出来，没有 DB 行）")))
        print("   来源           : %s" % t["source"])
        if not t["id"]:
            print("   %s" % WARN("▲ 这首歌不在本地曲库里（AI 漫游等场景），"))
            print("   %s" % WARN("  拿不到 id → 歌词和精确进度都用不了，只能显示歌名/歌手。"))
    else:
        print("   曲目           : %s" % WARN("读不到（客户端窗口标题为空？）"))
    print("   当前歌单       : %s" % (s["playlist"] or WARN("(无)")))
    print("   队列           : %d 首，当前第 %s 首" % (s["queue_len"], s["queue_index"]))
    print("   队列快照 mtime : %s   %s" % (
        time.strftime("%m-%d %H:%M:%S", time.localtime(s["queue_mtime"])),
        DIM("(只在换队列时更新，不能当'当前曲目'用)")))
    return s


def check_play_events() -> dict:
    print("─" * 68)
    print("5. playingCount（停下事件 → 精确校准 / 发现单曲循环）")
    ev = nowplaying.last_play_event()
    if not ev:
        print("   %s" % WARN("读不到"))
        return {}
    print("   最近一次停下   : %s  播了 %d 秒，停在 %s  (来源 %s, seq %d)" % (
        OK(ev["resource_id"]), ev["duration_s"],
        time.strftime("%m-%d %H:%M:%S", time.localtime(ev["stop_ms"] / 1000.0)),
        ev["source"], ev["seq"]))
    print("   推算起播       : %s" % time.strftime(
        "%m-%d %H:%M:%S", time.localtime((ev["stop_ms"] - ev["duration_s"] * 1000) / 1000.0)))
    return ev


def check_clock() -> dict:
    print("─" * 68)
    print("6. 自建进度时钟")
    c = clock.ProgressClock()
    c.update()
    s = c.snapshot()
    print("   进度           : %s / %s  (%.1f%%)" % (
        fmt_ms(s["position_ms"]), fmt_ms(s["duration_ms"]),
        100.0 * s["position_ms"] / s["duration_ms"] if s["duration_ms"] else 0))
    print("   状态           : %s" % (OK("播放中") if s["playing"] else WARN("暂停")))
    print("   校准次数       : %d  %s" % (s["calibrations"],
                                          DIM("(切歌 / playingCount 触发)")))
    print("   下次该刷新     : %d ms 后" % c.next_change_ms())
    return s


def check_lyrics(track_id: str) -> bool:
    print("─" * 68)
    print("7. 歌词接口")
    if not track_id:
        print("   %s 没有曲目 id，跳过" % WARN("!"))
        return False
    t0 = time.time()
    ly = lyrics.load(track_id)
    print("   请求耗时       : %.2fs  (缓存目录 %s)" % (time.time() - t0, paths.cache_dir("lyric")))
    if not ly:
        print("   %s %s" % (WARN("✗"), ly.error))
        return False
    print("   行数           : %d   翻译: %s" % (
        len(ly.lines), OK("有") if ly.has_translation else DIM("无")))
    print("   前 5 行        :")
    for l in ly.lines[:5]:
        print("       %7.2fs  %s" % (l.t_ms / 1000.0, l.text))
        if l.trans:
            print("                %s" % DIM(l.trans))
    return True


def check_playlists() -> bool:
    print("─" * 68)
    print("8. 离线歌单")
    pls = playlist.playlists()
    print("   本地歌单       : %d 个" % len(pls))
    for p in pls[:6]:
        print("       %-14s %-30s %5d 首  曲目表=%s" % (
            p["id"], p["name"][:28], p["track_count"], "有" if p["has_tracks"] else "无"))
    if len(pls) > 6:
        print("       %s" % DIM("... 还有 %d 个" % (len(pls) - 6)))
    print("   %s" % DIM("只读不切：Windows 版客户端不支持 orpheus://playlist/{id}（已实测）"))
    return bool(pls)


def check_keys(do_probe: bool) -> None:
    print("─" * 68)
    print("9. 控制通道（快捷键注入）")
    from . import media_keys as mk
    print("   默认键位表：")
    for k in mk.actions():
        seq = mk.KEYS[k]
        print("       %-12s %s" % (k, " + ".join("VK_0x%02X" % s if isinstance(s, int) else s
                                                 for s in seq)))
    print("   %s" % DIM("实测：VK_MEDIA_PLAY_PAUSE 播放/暂停 ✓  Ctrl+Alt+←/→ 切歌 ✓ "
                       "Ctrl+Alt+↑/↓ 音量 ✓  Ctrl+Alt+P 无效 ✗"))
    if do_probe:
        print("   热键占用探测（1409 = 已被占用）：")
        for name, r in mk.probe_hotkeys().items():
            print("       %-16s %s" % (name, WARN("占用") if r["occupied"] else OK("空闲")))


def check_refresh() -> None:
    print("─" * 68)
    print("10. 墨水屏刷新能力")
    import glob
    try:
        with open("/proc/version") as f:
            pass
    except Exception:
        pass
    print("   %s" % DIM("局刷只在设备侧探测（APK 里 Class.forName 试 Onyx SDK）。"))
    print("   %s" % DIM("PC 侧能做的：只输出变化的矩形，设备只 invalidate 那一块。"))


# ===================================================================== 工具

def fmt_ms(ms: int) -> str:
    ms = max(0, int(ms))
    return "%d:%02d" % (ms // 60000, (ms // 1000) % 60)


def watch(seconds: int) -> None:
    print("─" * 68)
    print("连续观察 %d 秒（每秒一行；title/DB/停下事件 任一变化都会标出来）" % seconds)
    print("%-8s %-6s %-9s %-22s %-14s %-12s %s" % (
        "时间", "峰值", "判定", "窗口标题", "historyTracks", "playEvent", "进度"))
    c = clock.ProgressClock()
    last_title = last_hist = last_ev = None
    for _ in range(seconds):
        c.update()
        s = c.snapshot()
        title = w.window_title() or ""
        hist = nowplaying.current_track()
        ev = nowplaying.last_play_event() or {}
        marks = ""
        if last_title is not None and title != last_title:
            marks += " [标题变]"
        if last_hist is not None and hist.id != last_hist:
            marks += " [DB切歌]"
        if last_ev is not None and ev.get("seq") != last_ev:
            marks += " [停下: %s %ss]" % (ev.get("resource_id"), ev.get("duration_s"))
        last_title, last_hist, last_ev = title, hist.id, ev.get("seq")
        print("%-8s %-6.3f %-9s %-22s %-14s %-12s %s/%s%s" % (
            time.strftime("%H:%M:%S"), c.detector.peak,
            "在播" if s["playing"] else "静音",
            title[:20], hist.id or "-",
            "%s:%s" % (ev.get("resource_id", "-"), ev.get("seq", "-")),
            fmt_ms(s["position_ms"]), fmt_ms(s["duration_ms"]), marks))
        time.sleep(1.0)


# ===================================================================== main

def main() -> int:
    ap = argparse.ArgumentParser(description="音乐面板通道体检（只读）")
    ap.add_argument("--watch", type=int, default=0, metavar="秒", help="连续观察模式")
    ap.add_argument("--keys", action="store_true", help="额外做热键占用探测")
    a = ap.parse_args()

    print("=" * 68)
    print("  H9 音乐面板 · 通道体检（只读，不会改变任何播放状态）")
    print("  Python %s  %d 位" % (sys.version.split()[0], 64 if sys.maxsize > 2**32 else 32))
    print("=" * 68)

    check_paths()
    if not check_process():
        print("\n%s 客户端没在跑，后面几项没意义，先打开网易云音乐再试。" % BAD("!"))
        return 1
    check_audio()
    s = check_nowplaying()
    check_play_events()
    check_clock()
    check_lyrics(s["track"]["id"])
    check_playlists()
    check_keys(a.keys)
    check_refresh()

    if a.watch:
        print()
        watch(a.watch)

    print("─" * 68)
    print("体检结束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
