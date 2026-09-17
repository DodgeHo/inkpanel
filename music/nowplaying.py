# -*- coding: utf-8 -*-
"""
nowplaying.py —— 读出"现在在放什么"

⚠️ 这一条最容易踩坑，实测定论，别改：

    **窗口标题才是"当前曲目"的可靠来源；
      historyTracks 会停更，不能单独信它。**

实测（2026-09-16 20:19）：窗口标题已经是 `As The World Goes Away - Lights & Motion`，
而 historyTracks 的最新一行还停在 20:07:48 的 `鬼哭无明`（晚了整整 12 分钟）。
historyTracks 只在"从正式歌单/队列里连续播放"那一段跟着更新
（20:01~20:07 队列 148 首连播时它每首都在写），
一旦客户端切到 AI 漫游之类不受它管的播放场景，它就不写了。

所以本模块的策略是**两条腿走路**：

    1) 窗口标题 → 歌名 / 歌手（永远跟得上，实测每次切歌都变）
    2) 拿歌名去 dbTrack / historyTracks / 歌单缓存里**反查 id 与时长**
       如果 historyTracks 最新一行的歌名与标题一致 → 顺带拿到精确起播时刻
    3) 标题也读不到 → 才退回 historyTracks 的行

顺带结论：
    `webdata\\file\\playingList` 是**队列快照**，只在换队列时写，
    实测能落后十几分钟。它只适合读**队列**和**当前歌单**，不能当"当前曲目"。

用法：
    python -m music.nowplaying
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import paths, winapi as w


# ===================================================================== 曲目

@dataclass
class Track:
    id: str = ""
    name: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    start_ms: int = 0          # 起播墙钟；0 = 未知
    source: str = ""           # "webdb" / "title" / "unknown"

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def start_at(self) -> float:
        return self.start_ms / 1000.0

    @property
    def key(self) -> str:
        """用来判断"换歌了没有"的标识。

        id 有时候反查不到（只在窗口标题里见过这首歌），
        这时退回 "歌名|歌手"，否则两首歌都是空 id 会被当成同一首。
        """
        if self.id:
            return "id:" + self.id
        return "nm:%s|%s" % (self.name.strip().lower(), self.artist.strip().lower())

    def label(self) -> str:
        if self.name and self.artist:
            return "%s - %s" % (self.name, self.artist)
        return self.name or "(未知曲目)"

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "artist": self.artist,
            "album": self.album, "duration_ms": self.duration_ms,
            "start_ms": self.start_ms, "source": self.source,
            "label": self.label(),
        }


def _split_title(title: str):
    """"歌名 - 歌手" → (歌名, 歌手)。没找到分隔符就整串当歌名。"""
    if not title:
        return "", ""
    for sep in (" - ", " – ", " — ", " -"):
        if sep in title:
            a, b = title.split(sep, 1)
            return a.strip(), b.strip()
    return title.strip(), ""


# ===================================================================== 读 DB

_db_error_logged = 0.0


def _connect():
    """只读打开曲库。**绝对不要写**，这是客户端的曲库。"""
    db = str(paths.webdb()).replace("\\", "/")
    return sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=1.0)


# ================================================== 歌名 → id/时长 反查索引

class _TitleIndex:
    """把 dbTrack 里的 (歌名, 歌手) 建成索引，用来从窗口标题反查 id 与时长。

    5380 首全解析约 0.3 秒，之后缓存复用；10 分钟或查不到时重建。
    """

    TTL = 600
    _map: dict = {}
    _at: float = 0.0

    @classmethod
    def _build(cls) -> None:
        m: dict = {}
        try:
            con = _connect()
            try:
                rows = con.execute("SELECT id, jsonStr FROM dbTrack").fetchall()
            finally:
                con.close()
        except Exception:
            rows = []
        for sid, js in rows:
            try:
                j = json.loads(js) if js else {}
            except Exception:
                continue
            name = (j.get("name") or "").strip().lower()
            if not name:
                continue
            artist = "/".join(a.get("name", "") for a in (j.get("artists") or []) if a.get("name"))
            m.setdefault(name, []).append({
                "id": str(sid),
                "artist": artist,
                "album": (j.get("album") or {}).get("name") or "",
                "duration_ms": int(j.get("duration") or 0),
            })
        cls._map = m
        cls._at = time.time()

    @classmethod
    def lookup(cls, name: str, artist: str = "") -> Optional[dict]:
        if not name:
            return None
        if not cls._map or time.time() - cls._at > cls.TTL:
            cls._build()
        key = name.strip().lower()
        cands = cls._map.get(key)
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        # 同名多版本：优先歌手对得上的
        a = (artist or "").strip().lower()
        if a:
            for c in cands:
                ca = c["artist"].lower()
                if a in ca or ca in a or any(x.strip() in ca for x in a.replace("/", " ").split()):
                    return c
        return cands[0]


def _history_latest() -> Optional[dict]:
    """historyTracks 最新一行（可能过时，只当交叉验证用）。"""
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT playtime, id, jsonStr FROM historyTracks "
                "ORDER BY playtime DESC LIMIT 1").fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row:
        return None
    playtime, sid, js = row
    try:
        j = json.loads(js) if js else {}
    except Exception:
        j = {}
    return {
        "playtime": int(playtime or 0),
        "id": str(sid or ""),
        "name": j.get("name") or "",
        "artist": "/".join(a.get("name", "") for a in (j.get("artists") or []) if a.get("name")),
        "album": (j.get("album") or {}).get("name") or "",
        "duration_ms": int(j.get("duration") or 0),
    }


def current_track(allow_title_fallback: bool = True) -> Track:
    """当前曲目。

    主路径：窗口标题定"歌名/歌手"，再反查 id 与时长。
    标题读不到时才退回 historyTracks 的行。
    """
    hist = _history_latest()
    name = artist = ""
    if allow_title_fallback:
        name, artist = _split_title(w.window_title() or "")

    # ---- 标题里有信息：以标题为准，反查 id/时长
    if name:
        # 1) historyTracks 那行如果和标题对得上，那就是最准的（还带精确起播时刻）
        if hist and hist["name"] and _same_song(hist["name"], hist["artist"], name, artist):
            return Track(id=hist["id"], name=hist["name"], artist=hist["artist"] or artist,
                         album=hist["album"], duration_ms=hist["duration_ms"],
                         start_ms=hist["playtime"], source="title+webdb")
        # 2) 只靠标题 + dbTrack 反查（拿得到 id 与时长，拿不到起播时刻）
        hit = _TitleIndex.lookup(name, artist)
        if hit:
            return Track(id=hit["id"], name=name, artist=artist or hit["artist"],
                         album=hit["album"], duration_ms=hit["duration_ms"],
                         start_ms=0, source="title+dbTrack")
        # 3) 只有标题
        return Track(name=name, artist=artist, source="title")

    # ---- 标题读不到：退回 historyTracks
    if hist and hist["name"]:
        return Track(id=hist["id"], name=hist["name"], artist=hist["artist"],
                     album=hist["album"], duration_ms=hist["duration_ms"],
                     start_ms=hist["playtime"], source="webdb")
    return Track(source="unknown")


def _same_song(n1: str, a1: str, n2: str, a2: str) -> bool:
    """两个"歌名/歌手"是不是同一首（标题里的歌手常被截断，所以只要求包含）。"""
    if not n1 or not n2:
        return False
    if n1.strip().lower() != n2.strip().lower():
        return False
    a1, a2 = (a1 or "").strip().lower(), (a2 or "").strip().lower()
    if not a1 or not a2:
        return True
    return a1 in a2 or a2 in a1 or a1.split("/")[0] == a2.split("/")[0]


# ===================================================================== 读队列

def queue(limit: Optional[int] = None) -> list:
    """当前播放队列快照（来自 playingList，纯 UTF-8 JSON）。"""
    p = paths.webdata_file("playingList")
    try:
        with open(p, "r", encoding="utf-8") as f:
            j = json.load(f)
    except Exception:
        return []
    out = []
    for it in j.get("list", []):
        t = it.get("track") or {}
        out.append({
            "id": str(it.get("id") or ""),
            "name": t.get("name") or "",
            "artist": "/".join(a.get("name", "") for a in (t.get("artists") or []) if a.get("name")),
            "duration_ms": int(t.get("duration") or 0),
            "display_order": it.get("displayOrder"),
        })
    return out[:limit] if limit else out


def queue_mtime() -> float:
    try:
        return os.stat(paths.webdata_file("playingList")).st_mtime
    except OSError:
        return 0.0


def current_playlist() -> Optional[dict]:
    """当前队列来自哪个歌单：{"id":..., "name":...}；读不到返回 None。

    实测 playingList.list[0].fromInfo.sourceData = {id, name, coverImgUrl}。
    """
    p = paths.webdata_file("playingList")
    try:
        with open(p, "r", encoding="utf-8") as f:
            j = json.load(f)
        lst = j.get("list") or []
        if not lst:
            return None
        src = ((lst[0].get("fromInfo") or {}).get("sourceData")) or {}
        if src.get("id"):
            return {"id": str(src["id"]), "name": src.get("name") or ""}
    except Exception:
        pass
    return None


def playing_index() -> Optional[int]:
    """当前曲目在队列里的下标（按 displayOrder 找）；找不到返回 None。"""
    cur = current_track()
    if not cur.id:
        return None
    for i, it in enumerate(queue()):
        if it["id"] == cur.id:
            return i
    return None


# ============================================================ 播完/停下的校准信号

def last_play_event() -> Optional[dict]:
    """最近一次"这一首停下来了"的记录（playingCount 表）。

    表结构：resourceId / playDuration(秒) / updateTime(毫秒) / source / uid /
            resourceType / id(自增序号) / jsonStr

    实测语义：**一首歌停下来（放完 / 被跳 / 被暂停）的瞬间写一行**，
    playDuration 是这一轮实际播了多久（秒），updateTime 是停下的时刻。

    这给了我们两个自校准能力：
      1. 单曲循环时 historyTracks 不会新增行（同一首 id 不变），
         但 playingCount 会新增行 → 靠 id 变化就能发现"又播了一遍"。
      2. `updateTime - playDuration*1000` = 这一轮真正的起播时刻，
         精确到毫秒，比"按切歌时刻归零"更准。

    返回 {"seq", "resource_id", "duration_s", "stop_ms", "source"}，失败返回 None。
    """
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT id, resourceId, playDuration, updateTime, source "
                "FROM playingCount ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            con.close()
        if not row:
            return None
        seq, rid, dur, upd, src = row
        return {
            "seq": int(seq or 0),
            "resource_id": str(rid or ""),
            "duration_s": int(dur or 0),
            "stop_ms": int(upd or 0),
            "source": src or "",
        }
    except Exception:
        return None


def recent_tracks(limit: int = 10) -> list:
    """最近播放过的曲目（historyTracks 倒序）。"""
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT playtime, id, jsonStr FROM historyTracks "
                "ORDER BY playtime DESC LIMIT ?", (int(limit),)).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    out = []
    for pt, sid, js in rows:
        try:
            j = json.loads(js)
        except Exception:
            j = {}
        out.append({
            "id": str(sid),
            "name": j.get("name") or "",
            "artist": "/".join(a.get("name", "") for a in (j.get("artists") or []) if a.get("name")),
            "duration_ms": int(j.get("duration") or 0),
            "playtime_ms": int(pt or 0),
        })
    return out


# ===================================================================== 汇总

def snapshot() -> dict:
    cur = current_track()
    pl = current_playlist()
    return {
        "track": cur.as_dict(),
        "playlist": pl,
        "queue_len": len(queue()),
        "queue_index": playing_index(),
        "queue_mtime": queue_mtime(),
    }


if __name__ == "__main__":
    import json as _json

    s = snapshot()
    print("曲目    :", s["track"]["label"], " id=%s" % s["track"]["id"])
    print("  专辑  :", s["track"]["album"])
    print("  时长  :", "%.1f 秒" % (s["track"]["duration_ms"] / 1000.0))
    print("  起播  :", time.strftime("%m-%d %H:%M:%S", time.localtime(s["track"]["start_ms"] / 1000.0))
          if s["track"]["start_ms"] else "未知", "(来源 %s)" % s["track"]["source"])
    print("歌单    :", s["playlist"])
    print("队列    : %d 首，当前第 %s 首" % (s["queue_len"], s["queue_index"]))
    print("  队列快照 mtime:", time.strftime("%m-%d %H:%M:%S", time.localtime(s["queue_mtime"])))
    print()
    print("队列前 8 首：")
    for i, it in enumerate(queue(8)):
        print("  %2d. %-40s %s" % (i, it["name"], it["artist"]))
    print()
    print("完整 JSON：")
    print(_json.dumps(s, ensure_ascii=False, indent=2))
