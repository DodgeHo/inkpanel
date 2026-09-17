# -*- coding: utf-8 -*-
"""
lyrics.py —— 取歌词（原文 + 翻译）并解析成时间轴

接口（实测免登录、免 cookie，HTTP 200，code=200，lrc 与 tlyric 都有）：
    https://music.163.com/api/song/lyric?os=pc&id={songId}&lv=-1&tv=-1

⚠️ 别再走"读客户端本地歌词缓存"那条路：
   网易云 3.1.40 上 **webdata\\lyric\\ 目录根本不存在**，那条路不通。

本地缓存：music/cache/lyric/{songId}.json（已在 .gitignore 里），
   避免重复请求、也避免被限流。没有翻译时也会缓存，只是 translation=[]。

用法：
    ly = lyrics.load("28793566")
    ly.index_at(42000)          # 当前该显示第几行
    ly.next_ms(42000)           # 下一行出现的时间（给设备安排刷新）
    python -m music.lyrics 28793566
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import paths

API = "https://music.163.com/api/song/lyric"
SEARCH_API = "https://music.163.com/api/search/get/web"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 8
CACHE_TTL = 30 * 24 * 3600      # 30 天
SEARCH_TTL = 180 * 24 * 3600    # 曲目 id 很稳定，缓存久一点

_TIME_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META_RE = re.compile(r"^\[(ti|ar|al|by|offset|length):(.*)\]$")
_PUNCT_RE = re.compile(r"[\s\-_/\\（）()【】\[\]{}·、,，.。!！?？:：;；'\"“”‘’|~]+")


def _norm(s: str) -> str:
    """归一化：去空格/标点、转小写，用于歌名歌手的模糊比对。"""
    return _PUNCT_RE.sub("", (s or "").lower())


# ===================================================================== 解析

def parse_lrc(text: str) -> List[tuple]:
    """LRC 文本 → [(t_ms, 文本)]，按时间排序。

    支持一行多个时间戳（[00:01.00][00:05.00]xxx），
    也兼容 [mm:ss]、[mm:ss.x]、[mm:ss.xx]、[mm:ss.xxx]。
    """
    if not text:
        return []
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        stamps = list(_TIME_RE.finditer(line))
        if not stamps:
            continue
        # 时间戳后面的才是歌词正文
        body = line[stamps[-1].end():].strip()
        for m in stamps:
            mm = int(m.group(1))
            ss = int(m.group(2))
            frac = m.group(3) or "0"
            # .5 → 500ms, .05 → 50ms, .005 → 5ms
            ms = int((frac + "000")[:3])
            out.append((mm * 60000 + ss * 1000 + ms, body))
    out.sort(key=lambda x: x[0])
    return out


@dataclass
class Line:
    t_ms: int
    text: str
    trans: str = ""

    @property
    def is_meta(self) -> bool:
        return not self.text and not self.trans


@dataclass
class Lyrics:
    song_id: str = ""
    lines: List[Line] = field(default_factory=list)
    has_translation: bool = False
    fetched_at: float = 0.0
    error: str = ""

    def __bool__(self) -> bool:
        return bool(self.lines)

    # ---------------------------------------------------------------- 查询

    def index_at(self, position_ms: int) -> int:
        """返回 position_ms 时刻应该在播的那一行下标；还没唱到第一句时返回 -1。"""
        lo, hi, ans = 0, len(self.lines) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.lines[mid].t_ms <= position_ms:
                ans = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return ans

    def window(self, position_ms: int, before: int = 2, after: int = 4,
               with_translation: bool = True) -> list:
        """取"当前行 + 上下文"若干行，供渲染用。

        返回 [{"t_ms","text","trans","current":bool}]，长度最多 before+1+after。
        """
        i = self.index_at(position_ms)
        if i < 0:
            i = -1
        start = max(0, i - before)
        end = min(len(self.lines), i + after + 1)
        out = []
        for k in range(start, end):
            ln = self.lines[k]
            if ln.is_meta:
                continue
            out.append({
                "t_ms": ln.t_ms,
                "text": ln.text,
                "trans": ln.trans if with_translation else "",
                "current": (k == i),
                "past": (k < i),
            })
        return out

    def next_ms(self, position_ms: int) -> Optional[int]:
        """下一行歌词出现的时间；没有下一行返回 None。"""
        i = self.index_at(position_ms)
        n = i + 1
        while n < len(self.lines):
            if not self.lines[n].is_meta:
                return self.lines[n].t_ms
            n += 1
        return None

    def as_dict(self, with_translation: bool = True) -> dict:
        return {
            "song_id": self.song_id,
            "has_translation": self.has_translation,
            "lines": [{"t_ms": l.t_ms, "text": l.text,
                       "trans": l.trans if with_translation else ""} for l in self.lines],
            "error": self.error,
        }


# ===================================================================== 网络

def _cache_path(song_id: str):
    return paths.cache_dir("lyric") / ("%s.json" % song_id)


def _read_cache(song_id: str) -> Optional[dict]:
    p = _cache_path(song_id)
    try:
        if not p.exists():
            return None
        if time.time() - p.stat().st_mtime > CACHE_TTL:
            return None
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(song_id: str, obj: dict) -> None:
    try:
        p = _cache_path(song_id)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception:
        pass


def fetch_raw(song_id: str, force: bool = False) -> dict:
    """调接口拿原始 JSON（带缓存）。失败返回 {}。"""
    if not force:
        c = _read_cache(song_id)
        if c is not None:
            return c

    import urllib.request
    import urllib.parse

    q = urllib.parse.urlencode({"os": "pc", "id": song_id, "lv": -1, "tv": -1, "rv": -1})
    req = urllib.request.Request(API + "?" + q, headers={"User-Agent": UA, "Referer": "https://music.163.com/"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"_error": repr(e)}

    if isinstance(data, dict) and data.get("code") == 200:
        _write_cache(song_id, data)
    return data


# ====================================================== 按名检索（反查 id 兜底）

def _search_cache_path(keyword: str):
    h = hashlib.md5(keyword.encode("utf-8")).hexdigest()[:16]
    return paths.cache_dir("search") / ("%s.json" % h)


def _read_search_cache(keyword: str) -> Optional[list]:
    p = _search_cache_path(keyword)
    try:
        if p.exists() and time.time() - p.stat().st_mtime <= SEARCH_TTL:
            with open(p, "r", encoding="utf-8") as f:
                v = json.load(f)
            # ★ 必须校验类型再交出去：调用方（search_songs）是**直接 return 缓存**的，
            #   一旦磁盘上是个 str/None，它会原样把非 list 塞回给 search_song，
            #   后面 `for c in cands` 就开始逐字符遍历 —— 报出来的错离现场很远。
            #   缓存是本模块自己写的，但历史版本/手工编辑都可能留下异类，
            #   这里 cheap 一次判型，把"脏缓存"变成"缓存未命中"，自愈。
            return v if isinstance(v, list) else None
    except Exception:
        pass
    return None


def _write_search_cache(keyword: str, obj: list) -> None:
    try:
        with open(_search_cache_path(keyword), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception:
        pass


def search_songs(keyword: str, limit: int = 10, force: bool = False) -> list:
    """按关键词检索歌曲，返回 [{id,name,artist,album,duration_ms}]。

    实测（2026-09-16）：GET https://music.163.com/api/search/get/web?s=..&type=1
    免登录、免 cookie，code=200，result.songs[] 带 id/name/artists/duration/album。
    失败一律返回 []（不抛异常），避免拖垮主循环。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    if not force:
        c = _read_search_cache(keyword)
        if c is not None:
            return c

    import urllib.request
    import urllib.parse

    q = urllib.parse.urlencode({"s": keyword, "type": 1, "offset": 0,
                                "limit": int(limit), "total": "true"})
    req = urllib.request.Request(
        SEARCH_API + "?" + q,
        headers={"User-Agent": UA, "Referer": "https://music.163.com/"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return []

    if not isinstance(data, dict) or data.get("code") != 200:
        return []
    # ⚠️ (data.get("result") or {}) 这种写法**挡不住非空非 dict**。
    #    实测（2026-09-17）：网易云在限流/风控时会把 result 变成**字符串**
    #    （如 "rate limited"），偶尔也会是 list。此时 (x or {}) 原样返回那个
    #    非 dict 值，接着 .get("songs") 就抛
    #        AttributeError: 'str' object has no attribute 'get'
    #    控制台只会看到一行 "search: AttributeError(...)"，看着像无关紧要的
    #    warning，其实是**检索兜底整条路都断了**（UI 漫游/电台的歌永远认不出 id）。
    #    所以必须显式判类型，而不是靠 or {}。
    r = data.get("result")
    if not isinstance(r, dict):
        return []
    songs = r.get("songs") or []
    if not isinstance(songs, list):
        return []
    out = []
    for s in songs:
        if not isinstance(s, dict):
            continue                    # 线上结构偶尔会混进非 dict，跳过而不是炸掉整条路
        out.append({
            "id": str(s.get("id") or ""),
            "name": s.get("name") or "",
            "artist": "/".join(a.get("name", "") for a in (s.get("artists") or []) if a.get("name")),
            "album": (s.get("album") or {}).get("name") or "",
            "duration_ms": int(s.get("duration") or 0),
        })
    _write_search_cache(keyword, out)
    return out


def search_song(name: str, artist: str = "") -> Optional[dict]:
    """按"歌名 + 歌手"找最匹配的一首；**不敢确定就返回 None**。

    宁可认不出（显示"歌词未找到"），也不能认错——认错会挂着别人的歌词，比没有更糟。

    打分（满分 10）：
        +6  歌名归一化后完全相等          ← 硬门槛
        +3  歌名一方包含另一方（短的一方 >= 2 字）
        +4  歌手归一化后完全相等
        +3  歌手任一片段（>= 2 字）出现在候选歌手串里
    必须 >= 6 分才认。
    """
    name = (name or "").strip()
    if not name:
        return None

    cands = search_songs(("%s %s" % (name, artist)).strip())
    if not cands:
        cands = search_songs(name)
    if not cands:
        return None
    # ★ 出口断言：候选集必须是 [{...}]。search_songs 现在已保证返回 list，
    #   但这里再挡一次 —— 因为**调用方（service._resolve_id）会直接对返回值
    #   调 .get()**，一旦这里漏出去一个 str，报错会出现在几百行之外，
    #   表现为"search: AttributeError('str' object has no attribute 'get')"，
    #   看着像无关紧要的 warning，实则是检索兜底整条路断了。
    cands = [c for c in cands if isinstance(c, dict)]
    if not cands:
        return None

    qn, qa = _norm(name), _norm(artist)
    parts = [_norm(p) for p in (artist or "").replace("/", " ").split()]
    parts = [p for p in parts if len(p) >= 2]

    best, best_score = None, 0.0
    for c in cands:
        cn, ca = _norm(c["name"]), _norm(c["artist"])
        score = 0.0
        if qn and cn == qn:
            score += 6
        elif qn and cn and (qn in cn or cn in qn) and min(len(qn), len(cn)) >= 2:
            score += 3
        if qa and ca:
            if qa == ca:
                score += 4
            elif any(p in ca for p in parts):
                score += 3
        if score > best_score:
            best, best_score = c, score
    if best is None or best_score < 6:
        return None
    res = dict(best)
    res["score"] = best_score
    return res


def _build(song_id: str, raw: dict) -> Lyrics:
    ly = Lyrics(song_id=str(song_id), fetched_at=time.time())
    if not raw:
        ly.error = "empty response"
        return ly
    if "_error" in raw:
        ly.error = raw["_error"]
        return ly
    if raw.get("code") != 200:
        ly.error = "code=%s" % raw.get("code")
        return ly

    body = (raw.get("lrc") or {}).get("lyric") or ""
    trans = (raw.get("tlyric") or {}).get("lyric") or ""
    if not body.strip():
        ly.error = "no lyric（纯音乐 / 无版权 / 需登录）"
        return ly

    base = parse_lrc(body)
    tr = dict(parse_lrc(trans))
    ly.has_translation = bool(tr)

    for t, text in base:
        if _META_RE.match("[%s]" % text):
            continue
        ln = Line(t_ms=t, text=text)
        if tr:
            # 翻译时间戳可能与原文差几毫秒，容差 500ms 内就近匹配
            ln.trans = tr.get(t, "")
            if not ln.trans:
                best = None
                for tt, tx in tr.items():
                    if abs(tt - t) <= 500 and (best is None or abs(tt - t) < abs(best[0] - t)):
                        best = (tt, tx)
                if best:
                    ln.trans = best[1]
        ly.lines.append(ln)

    # 去掉空行（但保留结构）
    ly.lines = [l for l in ly.lines if l.text.strip()]
    if not ly.lines:
        ly.error = "parsed 0 lines"
    return ly


def load(song_id: str, force: bool = False) -> Lyrics:
    """取一首歌的歌词（带缓存）。任何失败都返回带 .error 的空对象，不抛异常。"""
    if not song_id:
        return Lyrics(error="no song id")
    return _build(song_id, fetch_raw(str(song_id), force=force))


# ===================================================================== CLI

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="取歌词并解析")
    ap.add_argument("song_id", nargs="?", help="不传就取当前播放的歌")
    ap.add_argument("--force", action="store_true", help="忽略缓存重新请求")
    ap.add_argument("--at", type=float, default=None, help="模拟进度（秒），打印该时刻的歌词窗口")
    a = ap.parse_args()

    sid = a.song_id
    if not sid:
        from . import nowplaying
        sid = nowplaying.current_track().id
        print("当前曲目 id =", sid)

    t0 = time.time()
    ly = load(sid, force=a.force)
    print("耗时 %.2fs  has_translation=%s  lines=%d  error=%r" % (
        time.time() - t0, ly.has_translation, len(ly.lines), ly.error))
    if not ly:
        raise SystemExit(1)

    if a.at is not None:
        pos = int(a.at * 1000)
        print("\n=== %ds 处的窗口 ===" % a.at)
        for w in ly.window(pos):
            print("  %s%s%6.1fs  %s" % (">>" if w["current"] else "  ",
                                        "" if not w["past"] else " ", w["t_ms"] / 1000.0, w["text"]))
            if w["trans"]:
                print("            %6s  %s" % ("", w["trans"]))
        print("\n下一行在", ly.next_ms(pos), "ms")
    else:
        print("\n前 25 行：")
        for l in ly.lines[:25]:
            print("  %7.2fs  %-40s %s" % (l.t_ms / 1000.0, l.text, l.trans))
