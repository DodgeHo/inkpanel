# -*- coding: utf-8 -*-
"""
service.py —— 音乐面板的状态机：串起"读状态 → 取歌词 → 渲染 → 处理指令"

给 HTTP 层（dashboard/server.py）和 GUI 用同一个入口，避免两边逻辑各写一份。

线程安全：所有对内部状态的访问都在一把锁里，HTTP 是多线程的。

用法：
    svc = MusicService(width=825, height=1200)
    r = svc.frame()                 # 拿一帧（带脏矩形）
    svc.command("next")             # 发指令
    print(svc.status())
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

from . import clock as clock_mod
from . import lyrics as lyrics_mod
from . import media_keys, nowplaying, playlist, render_music

# 一次渲染后多久内直接复用同一张图（墨水屏不需要 60fps）
MIN_FRAME_INTERVAL = 0.6

# 可以下发的指令 → 说明
COMMANDS = {
    "play_pause": "播放 / 暂停",
    "prev": "上一首",
    "next": "下一首",
    "vol_up": "音量 +",
    "vol_down": "音量 -",
    "like": "喜欢（收藏）",
    "lyric_earlier": "歌词对早 5 秒（只动显示，不动客户端播放）",
    "lyric_later": "歌词对晚 5 秒（只动显示，不动客户端播放）",
    "realign": "重新对齐进度",
    "toggle_trans": "开 / 关翻译歌词",
    "cycle_lines": "切换歌词行数",
    "set_lyric_lead": "设置歌词提前量（毫秒）",
    "cycle_lyric_lead": "循环切换歌词提前量",
    "refresh": "强制重画",
}

LINE_CHOICES = (5, 7, 9)

# 歌词提前量的档位（毫秒）。默认 2000 = 「这一句结束前 2 秒就滚到下一句」。
# 为什么默认提前：墨水屏取图→屏上可见有 ~0.6s 固有延迟，再叠加整秒量化，
# 屏上的歌词天生比耳朵慢；提前把它补回来，而且歌词早出现不影响阅读
# （人读一句要好几秒），晚出现才难受。
LYRIC_LEAD_DEFAULT_MS = 2000
LYRIC_LEAD_CHOICES = (0, 1000, 2000, 3000, 5000)


class MusicService:
    def __init__(self, width: int = 825, height: int = 1200,
                 lyric_lines: int = 7, translation: bool = True,
                 allow_control: bool = True,
                 lyric_lead_ms: int = LYRIC_LEAD_DEFAULT_MS):
        self.width = int(width)
        self.height = int(height)
        self.lyric_lines = int(lyric_lines)
        self.translation = bool(translation)
        self.allow_control = bool(allow_control)

        self._lock = threading.RLock()
        self.clock = clock_mod.ProgressClock()
        self.renderer = render_music.MusicRenderer(
            self.width, self.height, self.lyric_lines, self.translation,
            lyric_lead_ms=int(lyric_lead_ms))

        self._last: Optional[render_music.RenderResult] = None
        self._last_at = 0.0
        self._lyric_for = ""
        self._lyric: Optional[lyrics_mod.Lyrics] = None
        self._lyric_err = ""
        self._resolve_cache: dict = {}      # "歌名|歌手" → 检索结果 or None
        self._resolved_id = ""
        self._last_cmd = ""
        self._last_cmd_at = 0.0
        self._status = ""
        self._status_at = 0.0
        self._errors: list = []

    # ------------------------------------------------------------ 状态

    def _note(self, msg: str, keep_s: float = 6.0) -> None:
        self._status = msg
        self._status_at = time.time() + keep_s

    def _error(self, msg: str) -> None:
        self._errors.append("%s %s" % (time.strftime("%H:%M:%S"), msg))
        del self._errors[:-20]

    def _ensure_lyrics(self, song_id: str) -> None:
        if not song_id:
            if self._lyric_for:
                self._lyric_for = ""
                self._lyric = None
                self._lyric_err = "这首歌拿不到 id（本地曲库没有、线上也没搜到）"
            return
        if song_id == self._lyric_for:
            return
        self._lyric_for = song_id
        try:
            self._lyric = lyrics_mod.load(song_id)
            self._lyric_err = self._lyric.error or ""
        except Exception as e:
            self._lyric = None
            self._lyric_err = repr(e)

    # -------------------------------------------------- 标题-only 曲目补 id

    def _resolve_id(self, st: dict) -> str:
        """窗口标题有歌名、但没有曲目 id 时，拿歌名去线上检索补一个。

        AI 漫游 / 电台 / 云盘里不在本地曲库的歌都走这条路。
        结果按 "歌名|歌手" 缓存在内存里，**搜不到也缓存 None**，
        否则每帧都发一次请求。

        只补 id 与时长，**绝不改 clock.track.id**
        （那会让 key 从 "nm:…" 变 "id:…" 被当成切歌，进度归零）。
        """
        if st.get("id"):
            return ""
        name = (st.get("name") or "").strip()
        if not name:
            return ""

        key = "nm:%s|%s" % (name.lower(), (st.get("artist") or "").strip().lower())
        if key not in self._resolve_cache:
            try:
                hit = lyrics_mod.search_song(name, st.get("artist") or "")
            except Exception as e:
                self._error("search: %r" % (e,))
                hit = None
            self._resolve_cache[key] = hit
            if len(self._resolve_cache) > 300:
                for k in list(self._resolve_cache)[:150]:
                    self._resolve_cache.pop(k, None)

        hit = self._resolve_cache.get(key)
        if not hit:
            self._resolved_id = ""
            return ""

        # 时长补进时钟对象（这个字段是"粘"的：update() 只在 t.duration_ms 非 0 时覆盖它）
        tk = self.clock.track
        if tk is not None and not tk.duration_ms and hit.get("duration_ms"):
            tk.duration_ms = int(hit["duration_ms"])
        if not st.get("duration_ms"):
            st["duration_ms"] = int(hit.get("duration_ms") or 0)
        if not st.get("album") and hit.get("album"):
            st["album"] = hit["album"]
        if not st.get("artist") and hit.get("artist"):
            st["artist"] = hit["artist"]

        self._resolved_id = hit["id"]
        self.clock.set_resolved_id(hit["id"])
        return hit["id"]

    # ------------------------------------------------------------ 出图

    def frame(self, force: bool = False) -> render_music.RenderResult:
        with self._lock:
            now = time.time()

            # 这里原本有个"0.6 秒内复用上一帧"的节流，现在是**有害的**：
            # 字画同步靠的是"把跨过歌词的那次取图对齐到歌词时间戳"，
            # 也就是设备会在歌词那一刻（而不是整秒）来取图。那一帧被节流挡回去，
            # 拿到的还是旧句子 → 得再等一整轮，对齐直接崩掉
            # （而且会表现为"有时候准、有时候晚一句"，最难查的那种）。
            # 渲染本身只要 ~130ms，节流本来就没省下什么，索性只在"极密集重复请求"
            # 这个明显异常的情况下才兜一下。见 clock.next_change_ms 的 docstring。
            if (not force and self._last is not None
                    and now - self._last_at < MIN_FRAME_INTERVAL):
                return self._last

            st = self.clock.update()
            pl = nowplaying.current_playlist()
            st["playlist_name"] = (pl or {}).get("name", "")

            if st.get("song_changed"):
                self.renderer.reset()          # 切歌 → 整屏重画
                self._resolved_id = ""

            sid = st.get("id") or self._resolve_id(st) or ""
            st["id"] = sid                      # 让渲染器能按 id 画/记状态
            st["resolved"] = sid
            self._ensure_lyrics(sid)

            status = self._status if time.time() < self._status_at else ""
            try:
                res = self.renderer.render(st, self._lyric, status)
            except Exception as e:
                self._error("render: %r" % (e,))
                raise

            # 下一句的归属：告诉设备"下一句在曲内几点开始、歌词行下标是几"，
            # 它就能在本地提前换行，不必等服务端算完再传 —— 少一次往返的滞后。
            #
            # ★ 歌词提前量必须在这里一起扣掉，否则就自相矛盾了：
            #   渲染时用 (pos + lead) 选句 → 画面在"歌词时刻 - lead"就变了；
            #   而唤醒点若还报"歌词时刻"，设备会**等过了那次变化**才醒 ——
            #   白跑一趟，屏上反而更晚。所以两边必须用同一个基准。
            lead = int(getattr(self.renderer, "lyric_lead_ms", 0) or 0)
            nxt_ms = self._lyric.next_ms(st["position_ms"]) if self._lyric else None
            if nxt_ms is not None and lead > 0:
                # 提前到"该滚到这一句"的时刻。
                # ⚠️ 别在越界时传 None —— next_change_ms 会把 None 当成"没有歌词行"
                #    而只靠整秒边界唤醒，**下一轮要等到整秒才醒**，反而更慢。
                #    越界（已经开始提前了）就意味着"现在就该滚"，给一个刚过 pos 的值，
                #    next_change_ms 里 (lyric_at > pos) 成立 → 立刻唤醒。
                nxt_ms = int(nxt_ms) - lead
                if nxt_ms <= st["position_ms"]:
                    nxt_ms = int(st["position_ms"]) + 1
            nxt = self.clock.next_change_ms(nxt_ms, lyric_disabled=not self._lyric)
            res.next_change_ms = nxt
            res.lyric_next_ms = nxt_ms
            res.sync = {
                "pos_ms": st["position_ms"],
                "lead_ms": self.clock.LEAD_MS,
                "playing": bool(st.get("playing")),
            }

            self._last = res
            self._last_at = now
            return res

    # ------------------------------------------------------------ 指令

    def command(self, action: str, **kw) -> dict:
        action = (action or "").strip()
        with self._lock:
            self._last_cmd, self._last_cmd_at = action, time.time()
            try:
                return self._dispatch(action, **kw)
            except Exception as e:
                self._error("cmd %s: %r" % (action, e))
                return {"ok": False, "action": action, "error": str(e)}

    def _dispatch(self, action: str, **kw) -> dict:
        if action in ("play_pause", "prev", "next", "vol_up", "vol_down", "like"):
            if not self.allow_control:
                return {"ok": False, "action": action, "error": "控制已禁用"}
            media_keys.press(action)
            label = COMMANDS.get(action, action)
            self._note(label)
            return {"ok": True, "action": action, "did": label}

        if action == "lyric_earlier":
            ms = self.clock.nudge(-5.0)
            self._note("歌词对早 5s")
            self.renderer.reset()
            return {"ok": True, "action": action, "position_ms": ms, "offset_ms": int(self.clock._offset_s * 1000)}

        if action == "lyric_later":
            ms = self.clock.nudge(+5.0)
            self._note("歌词对晚 5s")
            self.renderer.reset()
            return {"ok": True, "action": action, "position_ms": ms, "offset_ms": int(self.clock._offset_s * 1000)}

        if action == "realign":
            self.clock.realign()
            self.renderer.reset()
            self._note("已重新对齐")
            return {"ok": True, "action": action, "position_ms": self.clock.position_ms}

        if action == "toggle_trans":
            self.translation = not self.translation
            self.renderer.translation = self.translation
            self.renderer.reset()
            self._note("翻译 %s" % ("开" if self.translation else "关"))
            return {"ok": True, "action": action, "translation": self.translation}

        if action == "cycle_lines":
            i = LINE_CHOICES.index(self.lyric_lines) if self.lyric_lines in LINE_CHOICES else 1
            self.lyric_lines = LINE_CHOICES[(i + 1) % len(LINE_CHOICES)]
            self.renderer.lyric_lines = self.lyric_lines
            self.renderer.reset()
            self._note("歌词 %d 行" % self.lyric_lines)
            return {"ok": True, "action": action, "lyric_lines": self.lyric_lines}

        if action == "set_lyric_lead":
            # 歌词提前量（毫秒）。0 = 关闭（歌播到哪句显示哪句）。
            # 上限 10 秒：再大就会出现"上一句还没唱，屏上已经跳到下下句"，
            # 看着像歌词乱了 —— 提前是净收益，但只在 1~3 秒这个量级成立。
            try:
                ms = int(kw.get("ms", kw.get("value", 0)) or 0)
            except (TypeError, ValueError):
                return {"ok": False, "action": action, "error": "ms 不是整数"}
            ms = max(0, min(10000, ms))
            self.renderer.lyric_lead_ms = ms
            # 只改了"选句基准"，不 reset：窗口内容一变自然会出脏块，
            # reset 反而会白刷一次整屏（墨水屏整屏闪一下很显眼）。
            self._note("歌词提前 %s" % ("关闭" if ms == 0 else "%.1fs" % (ms / 1000.0)))
            return {"ok": True, "action": action, "lyric_lead_ms": ms}

        if action == "cycle_lyric_lead":
            # 给设备按键用：在常用档位间循环
            i = (LYRIC_LEAD_CHOICES.index(self.renderer.lyric_lead_ms)
                 if self.renderer.lyric_lead_ms in LYRIC_LEAD_CHOICES else -1)
            ms = LYRIC_LEAD_CHOICES[(i + 1) % len(LYRIC_LEAD_CHOICES)]
            self.renderer.lyric_lead_ms = ms
            self._note("歌词提前 %s" % ("关闭" if ms == 0 else "%.1fs" % (ms / 1000.0)))
            return {"ok": True, "action": action, "lyric_lead_ms": ms}

        if action == "refresh":
            self.renderer.reset()
            self._note("已强制重画")
            return {"ok": True, "action": action}

        if action == "key":
            # 设备端上报的"未知按键"，写日志以便之后把映射写死
            code = kw.get("code")
            try:
                from . import paths as _paths
                with open(_paths.log_file("keys.log"), "a", encoding="utf-8") as f:
                    f.write("%s keycode=%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), code))
            except Exception:
                pass
            return {"ok": True, "action": action, "code": code}

        return {"ok": False, "action": action, "error": "未知指令（可用：%s）" % ", ".join(sorted(COMMANDS))}

    # ------------------------------------------------------------ 诊断

    def status(self) -> dict:
        with self._lock:
            st = self.clock.snapshot()
            pl = nowplaying.current_playlist()
            return {
                "track": st,
                "playlist": pl,
                "lyric": {
                    "song_id": self._lyric_for,
                    "resolved_id": self._resolved_id,
                    "lines": len(self._lyric.lines) if self._lyric else 0,
                    "has_translation": bool(self._lyric and self._lyric.has_translation),
                    "error": self._lyric_err,
                },
                "view": {"lyric_lines": self.lyric_lines, "translation": self.translation,
                         "width": self.width, "height": self.height},
                "last_command": {"action": self._last_cmd, "at": self._last_cmd_at},
                "allow_control": self.allow_control,
                "errors": self._errors[-5:],
                "commands": COMMANDS,
            }

    def queue(self, limit: int = 30) -> list:
        return nowplaying.queue(limit)

    def playlists(self) -> list:
        return playlist.playlists()
