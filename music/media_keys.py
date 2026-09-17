# -*- coding: utf-8 -*-
"""
media_keys.py —— 通过"注入网易云自己的全局快捷键"来控制播放

原理（实测确认）：
    网易云音乐默认开启了全局快捷键并在系统里注册占用了
        Ctrl+Alt+P   播放/暂停
        Ctrl+Alt+←/→ 上一首 / 下一首
        Ctrl+Alt+↑/↓ 音量 +/-
        Ctrl+Alt+L   喜欢
    （用 RegisterHotKey 探测时这些组合返回 1409 ERROR_HOTKEY_ALREADY_REGISTERED，
      而 Ctrl+Alt+B / SPACE / 0 / 1 / V 返回成功 = 说明确实是网易云占的。）

    我们**注入**同样的按键（SendInput）。注入 ≠ 注册，所以完全不受 1409 影响，
    客户端收到后照常触发它自己的全局热键处理。

    这是 `winkidney/NetEaseMusicController`(2015) 的同款思路，零侵入：
    不装插件、不改配置、不碰进程内存。

前提：客户端"设置 → 快捷键 → 全局快捷键"保持勾选（默认就是勾的）。

用法：
    python -m music.media_keys play_pause
    python -m music.media_keys next
    python -m music.media_keys vol_up 3        # 连按 3 次
"""
from __future__ import annotations

import time

from . import winapi as w

# 标准多媒体键（VK_MEDIA_*）—— 实测**网易云对 VK_MEDIA_PLAY_PAUSE 有响应**，
# 比 Ctrl+Alt+P 可靠（后者在好多机器上被别的软件占用，实测按下去毫无反应）。
MEDIA_PLAY_PAUSE = 0xB3
MEDIA_STOP = 0xB2
MEDIA_NEXT = 0xB0
MEDIA_PREV = 0xB1

# 动作名 → 按键序列。**默认键位表**，可被 music/config.json 里的 "keys" 覆盖。
KEYS = {
    "play_pause": (MEDIA_PLAY_PAUSE,),
    "toggle": (MEDIA_PLAY_PAUSE,),
    "next": ("CTRL", "ALT", "RIGHT"),
    "prev": ("CTRL", "ALT", "LEFT"),
    "vol_up": ("CTRL", "ALT", "UP"),
    "vol_down": ("CTRL", "ALT", "DOWN"),
    "like": ("CTRL", "ALT", "L"),
}

# 备选键位（探测用；某个动作不灵时换这组试）
ALT_KEYS = {
    "play_pause": ("CTRL", "ALT", "P"),
    "next": (MEDIA_NEXT,),
    "prev": (MEDIA_PREV,),
}

# 连按时的间隔（太密客户端会吃掉中间几次）
REPEAT_GAP = 0.28


def actions() -> list:
    return sorted(KEYS)


def _to_vk(k) -> int:
    if isinstance(k, int):
        return k
    n = str(k).upper()
    if n not in w.VK:
        raise KeyError("未知按键 %r" % k)
    return w.VK[n]


def press(action: str, times: int = 1, gap: float = REPEAT_GAP, keys=None) -> int:
    """注入一次（或 times 次）快捷键。返回成功注入的按键事件总数。

    keys 可传入自定义按键序列（用于换键位重试）。
    抛 KeyError：动作名 / 按键名不认识。
    抛 OSError ：SendInput 一个事件都没吃进去（通常是被 UIPI 拦了）。
    """
    seq = keys
    if seq is None:
        if action not in KEYS:
            raise KeyError("未知动作 %r，可用：%s" % (action, ", ".join(actions())))
        seq = KEYS[action]
    vks = [_to_vk(k) for k in seq]

    total = 0
    for i in range(max(1, int(times))):
        n = w.send_keys(vks)
        if n == 0:
            raise OSError("SendInput 失败，GetLastError=%s" % w.ctypes.get_last_error())
        total += n
        if i + 1 < times:
            time.sleep(gap)
    return total


def press_raw(*keys) -> int:
    """注入任意组合键，例如 press_raw('CTRL', 'ALT', 'P') 或 press_raw(0xB3)。"""
    n = w.send_keys([_to_vk(k) for k in keys])
    if n == 0:
        raise OSError("SendInput 失败，GetLastError=%s" % w.ctypes.get_last_error())
    return n


def probe_hotkeys() -> dict:
    """探测一组候选组合键是不是被别人占着（返回 {名字: True占用/False空闲}）。

    注意：这会真的**注册**热键，用完要立刻注销，别和客户端的抢。
    """
    user32 = w.user32
    MOD_ALT, MOD_CONTROL = 0x0001, 0x0002
    result = {}
    cands = {
        "Ctrl+Alt+P": (MOD_CONTROL | MOD_ALT, 0x50),
        "Ctrl+Alt+Left": (MOD_CONTROL | MOD_ALT, 0x25),
        "Ctrl+Alt+Right": (MOD_CONTROL | MOD_ALT, 0x27),
        "Ctrl+Alt+Up": (MOD_CONTROL | MOD_ALT, 0x26),
        "Ctrl+Alt+Down": (MOD_CONTROL | MOD_ALT, 0x28),
        "Ctrl+Alt+L": (MOD_CONTROL | MOD_ALT, 0x4C),
        "Ctrl+Alt+M": (MOD_CONTROL | MOD_ALT, 0x4D),
        "Ctrl+Alt+V": (MOD_CONTROL | MOD_ALT, 0x56),
        "Ctrl+Alt+B": (MOD_CONTROL | MOD_ALT, 0x42),
    }
    for i, (name, (mods, vk)) in enumerate(cands.items()):
        hid = 0xB000 + i          # 自留一段 id，别撞
        ok = bool(user32.RegisterHotKey(None, hid, mods, vk))
        err = w.ctypes.get_last_error()
        if ok:
            user32.UnregisterHotKey(None, hid)
        result[name] = {"occupied": (not ok) and err == 1409, "error": None if ok else err}
    return result


if __name__ == "__main__":
    import argparse

    def show(seq):
        out = []
        for k in seq:
            out.append("VK_0x%02X" % k if isinstance(k, int) else str(k))
        return " + ".join(out)

    ap = argparse.ArgumentParser(description="注入网易云播放快捷键")
    ap.add_argument("action", nargs="?", default="list",
                    help="动作名，或 list / probe")
    ap.add_argument("n", nargs="?", type=int, default=1, help="连按次数")
    ap.add_argument("--alt", action="store_true", help="用备选键位表 ALT_KEYS")
    a = ap.parse_args()

    if a.action == "list":
        print("可用动作（默认键位）：")
        for k in actions():
            print("  %-12s %s" % (k, show(KEYS[k])))
        print("\n备选键位（--alt）：")
        for k, v in ALT_KEYS.items():
            print("  %-12s %s" % (k, show(v)))
    elif a.action == "probe":
        print("热键占用探测（occupied=已被占用，通常是客户端注册的）：")
        for name, r in probe_hotkeys().items():
            print("  %-16s %s" % (name, "已占用" if r["occupied"] else "空闲 err=%s" % r["error"]))
    else:
        keys = ALT_KEYS.get(a.action) if a.alt else None
        n = press(a.action, a.n, keys=keys)
        print("[OK] 注入 %s x%d，共 %d 个按键事件" % (a.action, a.n, n))
