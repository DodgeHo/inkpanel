# -*- coding: utf-8 -*-
"""
playlist.py —— 完全离线地读歌单

只读，不调接口、不登录、不写客户端任何文件。

数据来源（`Library\\webdb.dat`，SQLite，只读打开）：
    historyPlaylists    playtime / id / jsonStr     → 歌单列表（**含歌单名**）
    playlistTrackIds    id / jsonStr                → 歌单内歌曲 id 列表
    dbTrack             id / jsonStr                → 歌曲元数据（名字/时长/歌手）
    historyTracks       playtime / id / jsonStr     → 最近播放

⚠️ 实测：**Windows 版客户端不支持 `orpheus://playlist/{id}` 切歌单**
   （协议确实注册了，handler 进程也确实被拉起又 0.35s 退出，
     但客户端毫无反应；cloudmusic.dll 里 orpheus:// 只用于内部页面
     native/start.html、native/lrc.html、cache/）。
   所以本模块**只负责读**，不负责切。面板上歌单是"看"的，不是"点"的。

用法：
    python -m music.playlist              # 列出所有歌单（含各自的 id）
    python -m music.playlist <歌单id>      # 列出某个歌单的曲目
"""
from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

from . import paths
from .nowplaying import Track, _connect


def _rows(sql: str, args=()) -> list:
    try:
        con = _connect()
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()
    except Exception:
        return []


def _track_from_json(sid: str, js: str) -> Track:
    try:
        j = json.loads(js) if js else {}
    except Exception:
        j = {}
    return Track(
        id=str(sid),
        name=j.get("name") or "",
        artist="/".join(a.get("name", "") for a in (j.get("artists") or []) if a.get("name")),
        album=(j.get("album") or {}).get("name") or "",
        duration_ms=int(j.get("duration") or 0),
        source="webdb",
    )


# ===================================================================== 歌单

def playlists() -> List[dict]:
    """所有本地有记录的歌单，按最近访问倒序。

    返回 [{"id","name","cover","track_count","playtime_ms","has_tracks"}]。
    只有 playlistTrackIds 里有记录的才带得动曲目列表。
    """
    have = set()
    for (pid,) in _rows("SELECT id FROM playlistTrackIds"):
        have.add(str(pid))

    out = []
    for pt, pid, js in _rows("SELECT playtime, id, jsonStr FROM historyPlaylists ORDER BY playtime DESC"):
        try:
            j = json.loads(js) if js else {}
        except Exception:
            j = {}
        out.append({
            "id": str(pid),
            "name": j.get("name") or j.get("title") or "(未命名歌单)",
            "cover": j.get("coverImgUrl") or "",
            "track_count": int(j.get("trackCount") or 0),
            "playtime_ms": int(pt or 0),
            "has_tracks": str(pid) in have,
        })
    return out


def playlist_tracks(playlist_id: str, limit: Optional[int] = None) -> List[Track]:
    """某个歌单的曲目（用 playlistTrackIds 的顺序 + dbTrack 的元数据）。"""
    row = _rows("SELECT jsonStr FROM playlistTrackIds WHERE id=?", (str(playlist_id),))
    if not row:
        return []
    try:
        ids = [str(t["id"]) for t in json.loads(row[0][0]).get("trackIds", [])]
    except Exception:
        return []
    if limit:
        ids = ids[:limit]
    if not ids:
        return []

    meta = {}
    # 一次最多 900 个占位符，SQLite 默认上限 999，分批查
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        for sid, js in _rows("SELECT id, jsonStr FROM dbTrack WHERE id IN (%s)" % ph, chunk):
            meta[str(sid)] = js

    out = []
    for sid in ids:
        if sid in meta:
            out.append(_track_from_json(sid, meta[sid]))
        else:
            out.append(Track(id=sid, name="(本地无元数据)", source="webdb"))
    return out


def playlist_track_count(playlist_id: str) -> int:
    return len(playlist_tracks(playlist_id))


# ===================================================================== 队列/历史

def queue() -> List[Track]:
    """当前播放队列（转成 Track 列表）。"""
    from . import nowplaying

    return [Track(id=it["id"], name=it["name"], artist=it["artist"],
                  duration_ms=it["duration_ms"], source="queue")
            for it in nowplaying.queue()]


def history(limit: int = 20) -> List[Track]:
    """最近播放。"""
    from . import nowplaying

    return [Track(id=r["id"], name=r["name"], artist=r["artist"],
                  duration_ms=r["duration_ms"], start_ms=r["playtime_ms"], source="history")
            for r in nowplaying.recent_tracks(limit)]


def find_by_name(keyword: str, limit: int = 10) -> List[Track]:
    """在当前队列 + 所有歌单里，按名字/歌手模糊找歌（给面板上的点歌用）。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    seen, out = set(), []
    for t in queue() + history(30):
        if kw in t.name.lower() or kw in t.artist.lower():
            if t.id not in seen:
                seen.add(t.id)
                out.append(t)
                if len(out) >= limit:
                    return out
    for pl in playlists():
        if not pl["has_tracks"]:
            continue
        for t in playlist_tracks(pl["id"], limit=400):
            if kw in t.name.lower() or kw in t.artist.lower():
                if t.id not in seen:
                    seen.add(t.id)
                    out.append(t)
                    if len(out) >= limit:
                        return out
    return out


# ===================================================================== CLI

if __name__ == "__main__":
    import argparse
    import time

    ap = argparse.ArgumentParser(description="离线读歌单")
    ap.add_argument("playlist_id", nargs="?")
    ap.add_argument("-n", "--limit", type=int, default=30)
    a = ap.parse_args()

    if not a.playlist_id:
        pls = playlists()
        print("本地共 %d 个歌单：" % len(pls))
        for p in pls:
            print("  %-14s %-34s %5d 首  当地曲目表=%s  最近 %s" % (
                p["id"], p["name"][:32], p["track_count"],
                "有" if p["has_tracks"] else "无",
                time.strftime("%m-%d %H:%M", time.localtime(p["playtime_ms"] / 1000.0))
                if p["playtime_ms"] else "-"))
    else:
        ts = playlist_tracks(a.playlist_id, a.limit)
        print("歌单 %s 的前 %d 首：" % (a.playlist_id, len(ts)))
        for i, t in enumerate(ts):
            print("  %3d. %-44s %-16s %5.1fs" % (i + 1, t.name[:42], t.artist[:14],
                                                 t.duration_ms / 1000.0))
