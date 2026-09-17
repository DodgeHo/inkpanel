# -*- coding: utf-8 -*-
"""
paths.py —— 定位网易云音乐 PC 端在本机留下的各种数据位置

只做"找路"，不做任何解析，也不写客户端目录（webdb.dat 永远只读）。

实测环境：网易云音乐 3.1.40.205461，装于 D:\\Program Files\\NetEase\\CloudMusic
数据根目录：%LOCALAPPDATA%\\Netease\\CloudMusic
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------- 客户端数据

_ENV_OVERRIDE = "H9DASH_NCM_HOME"


def ncm_home() -> Path:
    """客户端数据根目录（可用环境变量 H9DASH_NCM_HOME 覆盖，便于测试）。"""
    ov = os.environ.get(_ENV_OVERRIDE)
    if ov:
        return Path(ov)
    la = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(la) / "Netease" / "CloudMusic"


def install_dir() -> Path | None:
    """客户端安装目录（从 webcmd 协议注册项里读，找不到就返回 None）。"""
    try:
        import winreg

        for root, path in (
            (winreg.HKEY_CLASSES_ROOT, r"orpheus\shell\open\command"),
            (winreg.HKEY_CLASSES_ROOT, r"cloudmusic.mp3\shell\open\command"),
        ):
            try:
                k = winreg.OpenKey(root, path)
                v = str(winreg.QueryValue(k, None))
                winreg.CloseKey(k)
            except OSError:
                continue
            # 形如  "D:\...\cloudmusic.exe"--webcmd="%1"
            i = v.find(".exe")
            if i > 0:
                p = v[: i + 4].strip().strip('"')
                d = Path(p)
                if d.exists():
                    return d.parent
    except Exception:
        pass
    for guess in (r"D:\Program Files\NetEase\CloudMusic",
                  r"C:\Program Files\NetEase\CloudMusic",
                  r"C:\Program Files (x86)\NetEase\CloudMusic"):
        if Path(guess).exists():
            return Path(guess)
    return None


# ---------------------------------------------------------------- 关键文件

def webdata_file(name: str) -> Path:
    """webdata\\file\\<name>，例如 playingList / lastTimePlayingList / fmPlay。"""
    return ncm_home() / "webdata" / "file" / name


def webdb() -> Path:
    """曲库 SQLite（约 600 MB）。永远以只读方式打开。"""
    return ncm_home() / "Library" / "webdb.dat"


def elog() -> Path:
    return ncm_home() / "cloudmusic.elog"


def exe() -> Path | None:
    d = install_dir()
    return (d / "cloudmusic.exe") if d else None


# ---------------------------------------------------------------- 本项目自己的目录
#
# 打包成 exe 后 __file__ 在随机临时目录（每次都换名），缓存/日志改去
# %APPDATA%\H9Dash\music —— 与 dashboard 的重定向同一套规则
if getattr(sys, "frozen", False):
    PROJECT = Path(os.environ.get("APPDATA") or str(Path.home())) / "H9Dash" / "music"
    PROJECT.mkdir(parents=True, exist_ok=True)
else:
    PROJECT = Path(__file__).resolve().parent


def cache_dir(*parts: str) -> Path:
    """歌词等缓存目录（已在 .gitignore 中排除）。"""
    p = PROJECT / "cache"
    for x in parts:
        p = p / x
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_file(name: str = "music.log") -> Path:
    p = PROJECT.parent / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p / name


def out_dir() -> Path:
    p = PROJECT / "out"
    p.mkdir(parents=True, exist_ok=True)
    return p


if __name__ == "__main__":  # 快速自检：python -m music.paths
    print("ncm_home   :", ncm_home(), ncm_home().is_dir())
    print("install_dir:", install_dir())
    print("webdb      :", webdb(), webdb().exists(),
          ("%d MB" % (webdb().stat().st_size // 1048576)) if webdb().exists() else "")
    for n in ("playingList", "lastTimePlayingList", "fmPlay", "recentListen"):
        f = webdata_file(n)
        print("  %-22s %s" % (n, ("%d bytes" % f.stat().st_size) if f.exists() else "MISSING"))
    print("cache_dir  :", cache_dir("lyric"))
    print("log_file   :", log_file())
