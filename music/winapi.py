# -*- coding: utf-8 -*-
"""
winapi.py —— 本项目用到的 Windows 原生调用（纯 ctypes，零第三方依赖）

刻意**不依赖 winsdk / pywin32**：
  · winsdk 最后更新 2023-08，只有 cp312 wheel，且实测 SMTC 拿不到网易云的数据；
  · 纯 ctypes 不受 Python 位数限制，32/64 位都能跑。

包含三块：
  1. COM 基础设施（CoInitializeEx + vtable 调用助手）
  2. SendInput 键盘注入（用来触发网易云自己的全局快捷键）
  3. 进程 / 窗口标题枚举
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)

# ===================================================================== 1. COM

COINIT_APARTMENTTHREADED = 0x2
S_OK = 0
S_FALSE = 1
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

_tls = threading.local()


def com_init() -> bool:
    """在当前线程初始化 COM（幂等）。返回 True 表示可用。"""
    if getattr(_tls, "com_ok", False):
        return True
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    # S_OK / S_FALSE 都算成功；RPC_E_CHANGED_MODE 说明已被别的模式初始化过，也能用
    ok = hr in (S_OK, S_FALSE) or hr == RPC_E_CHANGED_MODE
    _tls.com_ok = ok
    return ok


def com_uninit() -> None:
    if getattr(_tls, "com_ok", False):
        ole32.CoUninitialize()
        _tls.com_ok = False


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s: str):
        super().__init__()
        hr = ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(self))
        if hr < 0:
            raise ValueError("bad GUID %r" % s)


def vt_call(ptr: int, index: int, restype, *argtypes):
    """取出 COM 接口指针第 index 个虚函数，返回可直接调用的 WINFUNCTYPE 包装。"""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])


def release(ptr: int) -> None:
    if ptr:
        try:
            vt_call(ptr, 2, ctypes.c_long)(ptr)
        except Exception:
            pass


# ============================================================== 2. SendInput

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

VK = {
    "CTRL": 0x11, "ALT": 0x12, "SHIFT": 0x10, "WIN": 0x5B,
    "SPACE": 0x20, "ENTER": 0x0D, "ESC": 0x1B, "TAB": 0x09, "BACK": 0x08,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK[_c] = ord(_c)

# 需要带 EXTENDEDKEY 标志的键（否则会被当成小键盘键）
_EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22,
             0x5B, 0x5C, 0x5D, 0x6F, 0x0D}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ()
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wt.UINT


def _kb(vk: int, up: bool) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = 0
    inp.u.ki.dwFlags = flags
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = None
    return inp


def send_keys(vks, hold_ms: int = 30, gap_ms: int = 12) -> int:
    """按下并释放一组虚拟键（按顺序按下，逆序释放）。

    返回实际注入的事件数（应为 2 * len(vks)）。
    """
    import time

    mods = [v for v in vks[:-1]]
    main = vks[-1]

    seq = []
    for v in mods:
        seq.append(_kb(v, False))
        time.sleep(gap_ms / 1000.0)
    seq.append(_kb(main, False))
    time.sleep(hold_ms / 1000.0)
    seq.append(_kb(main, True))
    for v in reversed(mods):
        time.sleep(gap_ms / 1000.0)
        seq.append(_kb(v, True))

    arr = (INPUT * len(seq))(*seq)
    return int(user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT)))


def combo(*names: str) -> int:
    """把 'CTRL','ALT','P' 这样的名字转成 vk 列表并注入。"""
    vks = []
    for n in names:
        n = n.upper()
        if n not in VK:
            raise KeyError("unknown key %r" % n)
        vks.append(VK[n])
    return send_keys(vks)


# ========================================================= 3. 进程 / 窗口标题

TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
                ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * MAX_PATH)]


def processes() -> dict:
    """返回 {pid: "exe名小写"}。"""
    out = {}
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return out
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = e.szExeFile.lower()
            ok = kernel32.Process32NextW(snap, ctypes.byref(e))
    finally:
        kernel32.CloseHandle(snap)
    return out


def pids_of(exe_name: str) -> list:
    want = exe_name.lower()
    return [p for p, n in processes().items() if n == want]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def windows_of(pids) -> list:
    """返回 [(hwnd, title, pid)]，只保留标题非空的顶层窗口。"""
    pidset = set(int(p) for p in pids)
    found = []

    def cb(hwnd, _lparam):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) in pidset:
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.strip():
                    found.append((int(hwnd), buf.value, int(pid.value)))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


# 已知的非内容窗口标题（客户端同时开着好几个窗口）
_JUNK_TITLE = (
    "default ime", "msctfime ui", "gdi+ window", "桌面歌词", "迷你播放器",
    "网易云音乐", "cloudmusic", "microsoft", "program manager",
)


def window_title(exe_name: str = "cloudmusic.exe"):
    """取网易云主窗口的标题（形如 "歌名 - 歌手"）。

    客户端会同时开着好几个窗口（主窗、桌面歌词、迷你播放器、GDI+、IME 影子窗），
    所以策略是：先剔掉已知的垃圾标题，再优先挑含 " - " 的，最后才退回最长的那个。
    """
    wins = windows_of(pids_of(exe_name))
    if not wins:
        return None

    def junk(t: str) -> bool:
        low = t.lower()
        return any(j.lower() in low for j in _JUNK_TITLE)

    good = [t for _h, t, _p in wins if not junk(t)]
    titled = [t for t in good if " - " in t]
    if titled:
        titled.sort(key=len, reverse=True)
        return titled[0]
    if good:
        good.sort(key=len, reverse=True)
        return good[0]
    return None


if __name__ == "__main__":
    print("processes:", len(processes()))
    print("cloudmusic pids:", pids_of("cloudmusic.exe"))
    for h, t, p in windows_of(pids_of("cloudmusic.exe")):
        print("  hwnd=%s pid=%s title=%r" % (h, p, t))
    print("title ->", window_title())
