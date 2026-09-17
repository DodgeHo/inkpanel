# -*- coding: utf-8 -*-
"""
audio_state.py —— 用 WASAPI 读网易云在不在出声（播放 / 暂停判定）

为什么不用 SMTC：
    实测网易云音乐 3.1.40.205461 **根本不向系统发布 SMTC 会话**
    （GlobalSystemMediaTransportControlsSessionManager 连续枚举 5 次都是 0 个会话；
     64 位枚举三个 cloudmusic 进程的模块，没有任何一个加载 winrt_utils.dll）。
    所以进度和状态只能自己拼。

本模块提供两个信号：
    session_state()   —— IAudioSessionManager2 里 cloudmusic.exe 那个会话的
                         AudioSessionState：0=Inactive 1=Active 2=Expired
    device_peak()     —— 默认渲染端点的 IAudioMeterInformation 峰值 0.0~1.0

实测踩过的坑（别再走回头路）：
    **会话 state 对"暂停"几乎没用。** 网易云暂停时并不关闭音频流，
    所以 state 长时间停在 1(Active)：实测暂停后 peak 立刻变 0，
    但 state 仍然是 active，于是"state==1 就算在播"会把暂停误判成播放。
    → **判播放/暂停必须以峰值电平为主**，state 只作为电平不可用时的兜底。

用法：
    audio_state.play_state()          # 'playing' / 'paused' / 'unknown'
    audio_state.PlayDetector()        # 带迟滞的判定器，轮询 1Hz 用它
"""
from __future__ import annotations

from typing import Optional

import ctypes
import ctypes.wintypes as wt

from . import winapi as w

ole32 = w.ole32

CLSCTX_ALL = 0x17

CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioSessionManager2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
IID_IAudioSessionControl2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"
IID_IAudioMeterInformation = "{C02216F6-8C67-4B5B-9D00-D008E73E0064}"

# 数据流 / 角色
eRender = 0
eConsole = 0

STATE_NAMES = {0: "inactive", 1: "active", 2: "expired", -1: "unknown"}

PEAK_PLAYING = 0.0008   # 高于此值认为在出声（留一点余量，避免静音段落误判）


def _cocreate(clsid: str, iid: str) -> int:
    if not w.com_init():
        raise RuntimeError("CoInitializeEx 失败")
    ptr = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(w.GUID(clsid)), None, CLSCTX_ALL,
                                ctypes.byref(w.GUID(iid)), ctypes.byref(ptr))
    if hr < 0:
        raise OSError("CoCreateInstance 0x%08X" % (hr & 0xFFFFFFFF))
    return ptr.value


class _Device:
    """默认渲染端点 + 会话枚举器 + 电平表，一次创建反复使用。"""

    def __init__(self) -> None:
        self.enum = _cocreate(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)
        dev = ctypes.c_void_p()
        hr = w.vt_call(self.enum, 4, ctypes.c_long,
                       ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
            self.enum, eRender, eConsole, ctypes.byref(dev))
        if hr < 0:
            raise OSError("GetDefaultAudioEndpoint 0x%08X" % (hr & 0xFFFFFFFF))
        self.device = dev.value

        mgr = ctypes.c_void_p()
        hr = w.vt_call(self.device, 3, ctypes.c_long,
                       ctypes.POINTER(w.GUID), wt.DWORD, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_void_p))(
            self.device, ctypes.byref(w.GUID(IID_IAudioSessionManager2)),
            CLSCTX_ALL, None, ctypes.byref(mgr))
        if hr < 0:
            raise OSError("Activate(IAudioSessionManager2) 0x%08X" % (hr & 0xFFFFFFFF))
        self.mgr = mgr.value

        meter = ctypes.c_void_p()
        hr = w.vt_call(self.device, 3, ctypes.c_long,
                       ctypes.POINTER(w.GUID), wt.DWORD, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_void_p))(
            self.device, ctypes.byref(w.GUID(IID_IAudioMeterInformation)),
            CLSCTX_ALL, None, ctypes.byref(meter))
        self.meter = meter.value if hr >= 0 else 0

    def close(self) -> None:
        for p in (self.meter, self.mgr, self.device, self.enum):
            w.release(p)
        self.meter = self.mgr = self.device = self.enum = 0

    # ------------------------------------------------------------ 会话
    def sessions(self) -> list:
        """返回 [{pid, state, ident}]，ident 形如 "云音乐.exe|..." 的会话标识。"""
        out = []
        se = ctypes.c_void_p()
        hr = w.vt_call(self.mgr, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
            self.mgr, ctypes.byref(se))
        if hr < 0:
            return out
        se = se.value
        try:
            cnt = ctypes.c_int(0)
            w.vt_call(se, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                se, ctypes.byref(cnt))
            for i in range(cnt.value):
                ctl = ctypes.c_void_p()
                hr = w.vt_call(se, 4, ctypes.c_long, ctypes.c_int,
                               ctypes.POINTER(ctypes.c_void_p))(
                    se, i, ctypes.byref(ctl))
                if hr < 0 or not ctl.value:
                    continue
                ctl = ctl.value
                try:
                    st = ctypes.c_int(-1)
                    w.vt_call(ctl, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                        ctl, ctypes.byref(st))

                    # QI 到 IAudioSessionControl2 拿 pid
                    c2 = ctypes.c_void_p()
                    pid = -1
                    hr2 = w.vt_call(ctl, 0, ctypes.c_long,
                                    ctypes.POINTER(w.GUID),
                                    ctypes.POINTER(ctypes.c_void_p))(
                        ctl, ctypes.byref(w.GUID(IID_IAudioSessionControl2)),
                        ctypes.byref(c2))
                    if hr2 >= 0 and c2.value:
                        c2v = c2.value
                        p = wt.DWORD(0)
                        w.vt_call(c2v, 14, ctypes.c_long, ctypes.POINTER(wt.DWORD))(
                            c2v, ctypes.byref(p))
                        pid = int(p.value)
                        # 会话标识（能拿到 exe 名）
                        ident = ""
                        try:
                            sp = ctypes.c_wchar_p()
                            hr3 = w.vt_call(c2v, 12, ctypes.c_long,
                                            ctypes.POINTER(ctypes.c_wchar_p))(
                                c2v, ctypes.byref(sp))
                            if hr3 >= 0 and sp.value:
                                ident = sp.value
                                ole32.CoTaskMemFree(ctypes.cast(sp, ctypes.c_void_p))
                        except Exception:
                            pass
                        w.release(c2v)

                    out.append({"pid": pid, "state": int(st.value), "ident": ident})
                finally:
                    w.release(ctl)
        finally:
            w.release(se)
        return out

    # ------------------------------------------------------------ 电平
    def peak(self) -> float:
        if not self.meter:
            return -1.0
        v = ctypes.c_float(0.0)
        hr = w.vt_call(self.meter, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_float))(
            self.meter, ctypes.byref(v))
        return float(v.value) if hr >= 0 else -1.0


_dev: Optional[_Device] = None


def _device() -> _Device:
    global _dev
    if _dev is None:
        _dev = _Device()
    return _dev


def reset() -> None:
    """释放设备（换线程 / 出错后重新初始化时用）。"""
    global _dev
    if _dev is not None:
        try:
            _dev.close()
        except Exception:
            pass
    _dev = None


# ============================================================== 对外接口

def session_of(exe_name: str = "cloudmusic.exe") -> Optional[dict]:
    """找到网易云那个音频会话；找不到返回 None。"""
    pids = set(w.pids_of(exe_name))
    try:
        for s in _device().sessions():
            if s["pid"] in pids:
                return s
            # 有时候 pid 拿不到，用会话标识兜底
            if not pids and exe_name.split(".")[0].lower() in (s.get("ident") or "").lower():
                return s
    except Exception:
        reset()
    return None


def session_state(exe_name: str = "cloudmusic.exe") -> int:
    """1=Active（在播） 0=Inactive（停/暂停） 2=Expired -1=没找到。"""
    s = session_of(exe_name)
    return s["state"] if s else -1


def device_peak() -> float:
    """默认扬声器端点的峰值电平 0.0~1.0，失败返回 -1。"""
    try:
        return _device().peak()
    except Exception:
        reset()
        return -1.0


def play_state(exe_name: str = "cloudmusic.exe") -> str:
    """综合结论："playing" / "paused" / "unknown"。

    以峰值电平为准（见模块 docstring 里的坑），只在电平拿不到时才看 state。
    """
    pk = device_peak()
    st = session_state(exe_name)
    if pk < 0:
        if st == 1:
            return "playing"
        if st == 0:
            return "paused"
        return "unknown"
    if pk > PEAK_PLAYING:
        return "playing"
    if st == 0:
        return "paused"
    # 有会话、电平为 0：极大概率是暂停；也可能是歌曲里的静音段
    return "silent"


class PlayDetector:
    """带去抖的播放状态判定器。

    电平瞬间变 0 可能是"暂停"，也可能是歌曲里的静音/极弱段落，
    所以：**判定为 playing 立刻生效**（电平一涨就认），
    而判定为 paused 需要连续 silence_need 次（默认 2 次）都是静音才认。
    轮询 1Hz 时约等于"静音 2 秒后才认为暂停"。
    """

    def __init__(self, exe_name: str = "cloudmusic.exe", silence_need: int = 2):
        self.exe_name = exe_name
        self.silence_need = max(1, int(silence_need))
        self._silent = 0
        self.state = "unknown"      # 对外结论
        self.raw = "unknown"
        self.peak = -1.0

    def update(self) -> str:
        self.raw = play_state(self.exe_name)
        try:
            self.peak = device_peak()
        except Exception:
            self.peak = -1.0

        if self.raw == "playing":
            self._silent = 0
            self.state = "playing"
        elif self.raw in ("paused", "silent"):
            self._silent += 1
            if self.raw == "paused" or self._silent >= self.silence_need:
                self.state = "paused"
            elif self.state == "unknown":
                self.state = "playing"      # 还没静够，先按在播算
        else:
            self._silent += 1
            if self._silent >= self.silence_need:
                self.state = "unknown"
        return self.state


def snapshot(exe_name: str = "cloudmusic.exe") -> dict:
    st = session_state(exe_name)
    info = {"state": st, "state_name": STATE_NAMES.get(st, "?"),
            "peak": round(device_peak(), 5)}
    info["verdict"] = play_state(exe_name)
    return info


def all_sessions() -> list:
    """调试用：列出全部音频会话。"""
    names = w.processes()
    rows = []
    for s in _device().sessions():
        rows.append({
            "pid": s["pid"],
            "exe": names.get(s["pid"], "?"),
            "state": STATE_NAMES.get(s["state"], s["state"]),
        })
    return rows


if __name__ == "__main__":
    import json
    import time

    print("=== 全部音频会话 ===")
    for r in all_sessions():
        print("  pid=%-7s %-20s %s" % (r["pid"], r["exe"], r["state"]))
    print()
    print("=== 网易云 3 秒采样 ===")
    for i in range(6):
        print("  ", json.dumps(snapshot(), ensure_ascii=False))
        time.sleep(0.5)
