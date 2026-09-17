# -*- coding: utf-8 -*-
"""
render_music.py —— 把"正在放什么"画成墨水屏用的 16 阶灰 PNG

设计要点（都是为墨水屏服务的）：
  · **只输出变化的矩形**。整屏贴新位图 = 每次都走全屏波形 = 每 3 秒闪一次。
    渲染器把画面切成若干"块"，逐块和上一帧比像素，只报告真正变了的块。
  · **歌词区按行切块**，所以唱一句只刷一行。
  · **帧号**：每渲染一帧 frame_id +1。设备带着自己手里的帧号来问，
    服务端就能判断"你能不能只收脏块"，避免拼在一张过期的底图上。
  · 无动画、无渐变、无透明度（老安卓 + 墨水屏）。
  · 同时给出**触摸分区表**（服务端画按钮、设备按坐标命中）。

用法：
    python -m music.render_music -o out/music.png
    python -m music.render_music --landscape
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",    # 微软雅黑 Bold
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

_BAYER4 = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]


def font(size: int, bold: bool = False):
    order = FONT_CANDIDATES if not bold else [FONT_CANDIDATES[1], FONT_CANDIDATES[0]] + FONT_CANDIDATES[2:]
    for p in order:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def to_16gray(img: Image.Image) -> Image.Image:
    """量化到 16 阶灰 + 4x4 Bayer 有序抖动（和 dashboard/render.py 同一套做法）。"""
    import numpy as np

    a = np.asarray(img.convert("L")).astype(np.int32)
    h, w = a.shape
    b = np.array(_BAYER4, dtype=np.int32)
    tile = np.tile(b, ((h + 3) // 4, (w + 3) // 4))[:h, :w]
    q = np.clip((a * 15 + tile) // 255, 0, 15)
    return Image.fromarray((q * 17).astype("uint8"), "L").convert("RGB")


# ===================================================================== 数据结构

@dataclass
class Zone:
    """一个可点区域。"""
    name: str                 # 人类可读名字
    action: str               # 发给服务端的指令
    x0: int
    y0: int
    x1: int
    y1: int

    def as_dict(self) -> dict:
        return {"name": self.name, "action": self.action,
                "x": self.x0, "y": self.y0, "w": self.x1 - self.x0, "h": self.y1 - self.y0}

    def hit(self, x: int, y: int) -> bool:
        return self.x0 <= x < self.x1 and self.y0 <= y < self.y1


@dataclass
class RenderResult:
    png: bytes = b""
    blocks: dict = field(default_factory=dict)        # 名字 → (x0,y0,x1,y1)
    changed: List[str] = field(default_factory=list)  # 变化了的块名
    full: bool = False                                # 是否需要整屏刷
    rects: List[tuple] = field(default_factory=list)  # changed 对应的矩形
    zones: List[Zone] = field(default_factory=list)
    next_change_ms: int = 1000
    size: tuple = (0, 0)
    image: object = None                              # PIL Image（16 阶灰 RGB），裁图用
    frame_id: int = 0                                 # 单调递增帧号（设备靠它判断能否只收脏块）
    lyric_next_ms: Optional[int] = None               # 下一句歌词的曲内时间（给设备本地换行用）
    lyric_lead_ms: int = 0                            # 本帧用的歌词提前量（设备/GUI 回显用）
    sync: dict = field(default_factory=dict)          # 同步辅助信息（进度/提前量/是否在播）

    def crop_png(self, x: int, y: int, w: int, h: int) -> bytes:
        """只把某一块重新编码成 PNG —— 设备端局刷时只要这一小块。"""
        if self.image is None:
            return self.png
        box = (max(0, int(x)), max(0, int(y)),
               min(self.size[0], int(x) + int(w)), min(self.size[1], int(y) + int(h)))
        if box[2] <= box[0] or box[3] <= box[1]:
            return b""
        buf = io.BytesIO()
        self.image.crop(box).save(buf, "PNG", optimize=True)
        return buf.getvalue()

    def meta(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "size": {"w": self.size[0], "h": self.size[1]},
            "blocks": {k: {"x": v[0], "y": v[1], "w": v[2] - v[0], "h": v[3] - v[1]}
                       for k, v in self.blocks.items()},
            "changed": self.changed,
            "rects": [{"x": r[0], "y": r[1], "w": r[2] - r[0], "h": r[3] - r[1]} for r in self.rects],
            "full": self.full,
            "zones": [z.as_dict() for z in self.zones],
            "next_change_ms": self.next_change_ms,
            "lyric_next_ms": self.lyric_next_ms,
            "lyric_lead_ms": self.lyric_lead_ms,
            "sync": self.sync,
        }


# ===================================================================== 版式

class Layout:
    """竖版 825x1200 的坐标系；横版按宽度缩放沿用同一套比例。"""

    def __init__(self, w: int, h: int, lyric_lines: int = 7, translation: bool = True):
        self.w, self.h = w, h
        self.lyric_lines = max(3, int(lyric_lines))
        self.translation = translation

        s = w / 825.0                      # 相对竖版的缩放
        self.s = s
        self.pad = int(34 * s)
        self.header_h = int(196 * s)
        self.bar_h = int(96 * s)
        self.btn_h = int(300 * s)
        self.lyric_y0 = self.header_h + int(16 * s)
        self.lyric_y1 = h - self.bar_h - self.btn_h - int(16 * s)
        self.slot_h = max(int(46 * s), (self.lyric_y1 - self.lyric_y0) // self.lyric_lines)

    def px(self, v: float) -> int:
        return int(v * self.s)


# ===================================================================== 渲染器

class MusicRenderer:
    """有状态渲染器：记住上一帧，才能算脏矩形。"""

    def __init__(self, width: int = 825, height: int = 1200,
                 lyric_lines: int = 7, translation: bool = True,
                 lyric_lead_ms: int = 2000):
        self.width = width
        self.height = height
        self.lyric_lines = lyric_lines
        self.translation = translation
        # 歌词提前量（毫秒）：让歌词比歌**早这么多**切到下一句。
        # 默认 2000 = 「在这一句结束前 2 秒就滚到下一句」。
        #   墨水屏有 ~0.6s 的固有可见延迟，再加上整秒量化，
        #   屏上的歌词天生慢于耳朵；提前正好把这段补回来。
        #   歌词早出现无害（人读一句要好几秒），晚出现才难受。
        # 0 = 关闭，回到"歌播到哪句就显示哪句"。
        # 改这个字段即时生效，下一帧就按新值取窗口（不用 reset，
        # 因为窗口内容变了自然会产生脏块）。
        self.lyric_lead_ms = max(0, int(lyric_lead_ms))
        self._prev: Optional[Image.Image] = None      # 上一帧（L 模式）
        self.frame_id = 0                             # 单调递增；设备靠它决定能否只收脏块
        self._lyric_anchor = None                     # 上一帧"当前句"的 t_ms，用于判断窗口是否滚动

    # ------------------------------------------------------------ 对外

    def render(self, state: dict, lyrics_obj=None, status: str = "") -> RenderResult:
        """state 来自 clock.snapshot()；lyrics_obj 是 lyrics.Lyrics 或 None。"""
        self.frame_id += 1
        lay = Layout(self.width, self.height, self.lyric_lines, self.translation)
        img = Image.new("L", (self.width, self.height), 255)
        d = ImageDraw.Draw(img)
        zones: List[Zone] = []
        blocks: dict = {}

        # ---------- 0. 进度量化（省刷新的关键，改动前先读这段） ----------
        # 进度条横跨整个宽度，只要它变了，脏矩形就是一条 825px 的全宽条。
        # 如果拿毫秒级的 position_ms 去画，**每秒会重画 30~60 次**，
        # 于是哪怕歌词一句没换，设备也会一直收到脏块、墨水屏一直动 ——
        # 实测 12 秒内刷新 40 次、累计脏面积是整屏的 4.6 倍（见 _lat_repro.py）。
        #
        # 墨水屏上"进度条走一秒"和"走两秒"肉眼没差别，所以把它对齐到整秒：
        # 秒数没跳，画出来的进度条就是逐像素一致的 → _diff 直接判为"没变"。
        # 歌词区同理：只有"当前句真的换了"才需要重画那几行。
        pos = int(state.get("position_ms") or 0)
        qpos = (pos // 1000) * 1000          # 量化到整秒
        state = dict(state)
        state["position_ms"] = qpos
        state["_pos_ms_raw"] = pos           # 需要精确值的地方（比如调试）还能拿到

        # ---------- 1. 顶部：歌名 / 歌手 · 专辑 ----------
        blocks["header"] = (0, 0, self.width, lay.header_h)
        self._draw_header(d, lay, state, status)

        # ---------- 2. 歌词区（按行切块） ----------
        # ★ 歌词提前量（lyric_lead_ms）：让歌词比歌**早 N 毫秒**切到下一句。
        #
        #   为什么要这个东西：
        #     墨水屏从"取图"到"屏上真的变了"有 ~0.6s 的固有延迟
        #     （HTTP + 解码 + 局刷波形，见 clock.LEAD_MS），
        #     再加上"跨过歌词的那次取图只能对齐到整秒"这个量化损失，
        #     结果就是**屏上的歌词总比耳朵听到的慢半拍**。
        #
        #   为什么"提前"不影响阅读：
        #     歌词是给人看的，早 1~2 秒出现完全无害 —— 人眼读一句要好几秒，
        #     而且下一句提前印出来反而像"预告"。反过来，晚了就是"唱完了才出来"，
        #     那才是真的难受。所以这个方向上提前是**净收益**。
        #
        #   为什么不动 position_ms 本身：
        #     进度条、剩余时间、clock.next_change_ms 全都基于 position_ms。
        #     像 lyric_earlier 那样整体 nudge 会让进度条也一起偏 5 秒。
        #     这里只在**取歌词窗口**这一步加偏移 —— 进度条照旧走真实位置。
        #
        #   注意：这只影响"哪句被选为当前句"。歌词行本身的时间戳不动，
        #   所以不会把翻译行、前后文行弄错位（窗口是按下标切的）。
        pos = qpos
        lyric_pos = pos + max(0, int(self.lyric_lead_ms or 0))
        window = []
        if lyrics_obj:
            window = lyrics_obj.window(lyric_pos, before=2, after=self.lyric_lines - 3,
                                       with_translation=self.translation)
        cur_idx = None
        for i, wline in enumerate(window):
            if wline["current"]:
                cur_idx = i
                break
        if cur_idx is None:
            cur_idx = min(2, max(0, len(window) - 1)) if window else 0

        first = cur_idx - 2
        if first < 0:
            first = 0
        shown = window[first:first + self.lyric_lines]

        # 窗口是不是又滚动了一行？滚动了才需要重画整个歌词区。
        # 用"当前句下标"当锚点：没换句就说明窗口内容没动。
        cur_anchor = None
        if shown:
            for wline in shown:
                if wline["current"]:
                    cur_anchor = wline["t_ms"]
                    break
        self._lyric_anchor = cur_anchor

        y = lay.lyric_y0
        for i in range(self.lyric_lines):
            rect = (0, y, self.width, y + lay.slot_h)
            blocks["lyric_%d" % i] = rect
            if i < len(shown):
                self._draw_lyric_line(d, lay, shown[i], y)
            y += lay.slot_h

        if not shown:
            blocks["lyric_status"] = (0, lay.lyric_y0, self.width, lay.lyric_y1)
            self._draw_lyric_placeholder(d, lay, lyrics_obj, state)

        # ---------- 3. 进度条 ----------
        yb = self.height - lay.bar_h - lay.btn_h
        blocks["progress"] = (0, yb, self.width, yb + lay.bar_h)
        self._draw_progress(d, lay, state, yb)

        # ---------- 4. 按钮区（固定，基本不变） ----------
        zones = self._draw_buttons(d, lay, state)
        blocks["buttons"] = (0, self.height - lay.btn_h, self.width, self.height)

        # ---------- 输出 ----------
        out = to_16gray(img)
        buf = io.BytesIO()
        out.save(buf, "PNG", optimize=True)

        gray = img if img.mode == "L" else img.convert("L")
        changed, rects, full = self._diff(gray, blocks)
        self._prev = gray

        return RenderResult(png=buf.getvalue(), blocks=blocks, changed=changed,
                            rects=rects, full=full, zones=zones,
                            next_change_ms=1000, size=(self.width, self.height),
                            image=out, frame_id=self.frame_id,
                            lyric_lead_ms=self.lyric_lead_ms)

    def reset(self) -> None:
        """下一帧强制整屏刷新。"""
        self._prev = None
        self._lyric_anchor = None

    def _diff(self, cur: Image.Image, blocks: dict):
        if self._prev is None or self._prev.size != cur.size:
            return list(blocks.keys()), [blocks[k] for k in blocks], True
        changed, rects = [], []
        for name, r in blocks.items():
            try:
                a = self._prev.crop(r)
                b = cur.crop(r)
                if ImageChops_equal(a, b):
                    continue
            except Exception:
                pass
            changed.append(name)
            rects.append(r)
        return changed, rects, False

    # ------------------------------------------------------------ 各块绘制

    def _draw_header(self, d, lay: Layout, state: dict, status: str) -> None:
        s = lay.s
        pad = lay.pad
        d.rectangle([0, 0, self.width, int(6 * s)], fill=0)

        name = state.get("name") or "(未知曲目)"
        artist = state.get("artist") or ""
        album = state.get("album") or ""

        f_name = font(int(50 * s), bold=True)
        f_sub = font(int(28 * s))
        f_tiny = font(int(22 * s))

        # 歌名太长就缩小字号
        while d.textlength(name, font=f_name) > self.width - 2 * pad and f_name.size > 22:
            f_name = font(f_name.size - 2, bold=True)

        playing = state.get("playing")
        # 播放/暂停指示用画的，不用字符（雅黑没有 ▶ / ❚ 这两个字形，会变成豆腐块）
        gx, gy = pad, int(46 * s)
        gr = int(15 * s)
        if playing:
            d.polygon([(gx, gy - gr), (gx, gy + gr), (gx + int(gr * 1.7), gy)], fill=0)
        else:
            gb = int(9 * s)
            d.rectangle([gx, gy - gr, gx + gb, gy + gr], fill=0)
            d.rectangle([gx + gb + int(8 * s), gy - gr, gx + 2 * gb + int(8 * s), gy + gr], fill=0)

        d.text((pad + int(46 * s), int(26 * s)), name, font=f_name, fill=0)

        sub = " · ".join([x for x in (artist, album) if x])
        if d.textlength(sub, font=f_sub) > self.width - 2 * pad:
            while sub and d.textlength(sub + "…", font=f_sub) > self.width - 2 * pad:
                sub = sub[:-1]
            sub += "…"
        d.text((pad + int(46 * s), int(26 * s) + f_name.size + int(10 * s)), sub, font=f_sub, fill=70)

        if status:
            d.text((self.width - pad - d.textlength(status, font=f_tiny), int(14 * s)),
                   status, font=f_tiny, fill=120)

        y = lay.header_h - int(8 * s)
        d.line([pad, y, self.width - pad, y], fill=0, width=max(2, int(2 * s)))

    def _draw_lyric_line(self, d, lay: Layout, ln: dict, y: int) -> None:
        s = lay.s
        pad = lay.pad
        cur = ln.get("current")
        text = ln.get("text") or ""
        trans = ln.get("trans") or ""

        base = int(40 * s)
        text_x = pad + int(24 * s)
        avail = self.width - 2 * text_x

        # **当前行不许截断**：放不下就逐档缩字号（这句是整屏的主角）
        if cur:
            f_main = font(base, bold=True)
            while f_main.size > max(20, base - 14) and d.textlength(text, font=f_main) > avail:
                f_main = font(f_main.size - 2, bold=True)
        else:
            f_main = font(base - int(2 * s))

        f_tr = font(int(26 * s))

        # 当前行：左侧竖条 + 加粗 + 纯黑；其它行灰一点
        fill_main = 0 if cur else 110

        total_h = f_main.size + (f_tr.size + int(6 * s) if trans else 0)
        ty = y + max(0, (lay.slot_h - total_h) // 2)

        if cur:
            d.rectangle([pad, ty - int(4 * s), pad + int(8 * s), ty + total_h + int(4 * s)], fill=0)

        if d.textlength(text, font=f_main) <= avail:
            tx = text_x + (avail - d.textlength(text, font=f_main)) // 2
        else:
            while text and d.textlength(text + "…", font=f_main) > avail:
                text = text[:-1]
            text += "…"
            tx = text_x
        d.text((tx, ty), text, font=f_main, fill=fill_main)

        if trans:
            tt = trans
            if d.textlength(tt, font=f_tr) > avail:
                while tt and d.textlength(tt + "…", font=f_tr) > avail:
                    tt = tt[:-1]
                tt += "…"
            d.text((text_x + (avail - d.textlength(tt, font=f_tr)) // 2,
                    ty + f_main.size + int(6 * s)), tt, font=f_tr, fill=140)

    def _draw_lyric_placeholder(self, d, lay: Layout, lyrics_obj, state: dict) -> None:
        s = lay.s
        if lyrics_obj is not None and not lyrics_obj and lyrics_obj.error:
            msg = lyrics_obj.error
            if "no lyric" in msg or "纯音乐" in msg:
                msg = "纯音乐，请欣赏"
            elif "no song id" in msg:
                msg = "正在识别这首歌…"
        elif not state.get("id"):
            msg = "正在识别这首歌…"
        else:
            msg = "歌词加载中…"
        f = font(int(30 * s))
        cy = (lay.lyric_y0 + lay.lyric_y1) // 2
        d.text(((self.width - d.textlength(msg, font=f)) // 2, cy), msg, font=f, fill=120)

    def _draw_progress(self, d, lay: Layout, state: dict, y: int) -> None:
        s = lay.s
        pad = lay.pad
        pos = int(state.get("position_ms") or 0)
        dur = int(state.get("duration_ms") or 0)

        bh = max(10, int(16 * s))
        by = y + int(26 * s)
        x0, x1 = pad, self.width - pad

        d.rectangle([x0, by, x1, by + bh], outline=0, width=max(2, int(2 * s)))
        if dur > 0:
            ratio = min(1.0, max(0.0, pos / float(dur)))
            fw = int((x1 - x0 - 4) * ratio)
            if fw > 0:
                d.rectangle([x0 + 2, by + 2, x0 + 2 + fw, by + bh - 2], fill=0)

        f_t = font(int(30 * s))
        f_s = font(int(24 * s))
        left = "%s / %s" % (fmt_ms(pos), fmt_ms(dur) if dur else "--:--")
        d.text((x0, by + bh + int(10 * s)), left, font=f_t, fill=0)

        # 时长未知：进度条画不满，明说一句免得用户以为卡了
        right = ""
        if state.get("drifted"):
            right = "已校准 %+ds" % int(round((state.get("offset_ms") or 0) / 1000.0))
        elif dur <= 0:
            right = "进度未知"
        if right:
            d.text((x1 - d.textlength(right, font=f_s), by + bh + int(14 * s)),
                   right, font=f_s, fill=140)

    def _draw_buttons(self, d, lay: Layout, state: dict):
        """画按钮并返回 (zones)。按钮分两排：上面是播放控制，下面是低频功能。"""
        s = lay.s
        pad = lay.pad
        top = self.height - lay.btn_h
        zones: List[Zone] = []

        # ---- 第一排：上一首 / 播放暂停 / 下一首 / 音量- / 音量+
        row1_y0 = top + int(10 * s)
        row1_h = int(132 * s)
        gap = int(12 * s)
        n1 = 5
        bw = (self.width - 2 * pad - gap * (n1 - 1)) // n1
        labels1 = [
            ("prev", "prev", "prev"),
            ("pp", "play_pause", "pp"),
            ("next", "next", "next"),
            ("vol-", "vol_down", "vol_down"),
            ("vol+", "vol_up", "vol_up"),
        ]
        for i, (nm, act, kind) in enumerate(labels1):
            x0 = pad + i * (bw + gap)
            rect = (x0, row1_y0, x0 + bw, row1_y0 + row1_h)
            self._button_box(d, rect, s, primary=(kind == "play_pause"))
            self._button_glyph(d, rect, kind, state, s)
            zones.append(Zone(nm, act, *rect))

        # ---- 第二排：-5s / +5s / 翻译 / 歌词行数 / 歌词提前
        row2_y0 = row1_y0 + row1_h + int(14 * s)
        row2_h = int(84 * s)
        # 提前量按钮的标签带当前值 —— 不显示值的话，按了之后屏上没有任何反馈，
        # 人会以为没生效然后一直按（实测这个键会被连按好几次）。
        _lead = int(getattr(self, "lyric_lead_ms", 0) or 0)
        lead_label = "提前关" if _lead <= 0 else "提前%.1fs" % (_lead / 1000.0)
        labels2 = [
            ("对早5s", "lyric_earlier", "对早5s"),
            ("对晚5s", "lyric_later", "对晚5s"),
            ("翻译", "toggle_trans", "翻译"),
            ("行数", "cycle_lines", "行数"),
            ("提前量", "cycle_lyric_lead", lead_label),
        ]
        n2 = len(labels2)
        bw2 = (self.width - 2 * pad - gap * (n2 - 1)) // n2
        for i, (nm, act, label) in enumerate(labels2):
            x0 = pad + i * (bw2 + gap)
            rect = (x0, row2_y0, x0 + bw2, row2_y0 + row2_h)
            self._button_box(d, rect, s, small=True)
            f = font(int(30 * s))
            d.text((rect[0] + (rect[2] - rect[0] - d.textlength(label, font=f)) // 2,
                    rect[1] + (rect[3] - rect[1] - f.size) // 2), label, font=f, fill=0)
            zones.append(Zone(nm, act, *rect))

        # ---- 底部一行：当前歌单名（只展示，不可点） ----
        y = row2_y0 + row2_h + int(10 * s)
        if y + int(30 * s) < self.height:
            pl = state.get("playlist_name") or "—"
            f = font(int(24 * s))
            txt = "歌单：" + pl
            if d.textlength(txt, font=f) > self.width - 2 * pad:
                while pl and d.textlength("歌单：" + pl + "…", font=f) > self.width - 2 * pad:
                    pl = pl[:-1]
                txt = "歌单：" + pl + "…"
            d.text((pad, y), txt, font=f, fill=110)
        return zones

    def _button_box(self, d, rect, s, primary=False, small=False):
        d.rectangle(rect, outline=0, width=max(2, int(3 * s)))
        if primary:
            d.rectangle([rect[0] + 3, rect[1] + 3, rect[2] - 3, rect[3] - 3],
                        outline=0, width=max(1, int(2 * s)))

    def _button_glyph(self, d, rect, kind, state, s) -> None:
        x0, y0, x1, y1 = rect
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        r = int(24 * s)

        if kind == "pp":
            if state.get("playing"):
                bw = int(10 * s)
                d.rectangle([cx - bw - int(4 * s), cy - r, cx - int(4 * s), cy + r], fill=0)
                d.rectangle([cx + int(4 * s), cy - r, cx + bw + int(4 * s), cy + r], fill=0)
            else:
                d.polygon([(cx - int(16 * s), cy - r), (cx - int(16 * s), cy + r),
                           (cx + int(20 * s), cy)], fill=0)
        elif kind in ("prev", "next"):
            # 三角形 + 一条竖杠；next 朝右，prev 朝左
            a = int(17 * s)
            if kind == "next":
                pts = [(cx - a, cy - r), (cx - a, cy + r), (cx + a, cy)]
                bar_x = cx + a + int(5 * s)
            else:
                pts = [(cx + a, cy - r), (cx + a, cy + r), (cx - a, cy)]
                bar_x = cx - a - int(5 * s)
            d.polygon(pts, fill=0)
            d.rectangle([bar_x - int(3 * s), cy - r, bar_x + int(3 * s), cy + r], fill=0)
        elif kind in ("vol_down", "vol_up"):
            # 小喇叭（方块 + 梯形口）+ 加/减号
            bx = cx - int(26 * s)
            d.rectangle([bx, cy - int(7 * s), bx + int(9 * s), cy + int(7 * s)], fill=0)
            d.polygon([(bx + int(9 * s), cy - int(7 * s)), (bx + int(20 * s), cy - int(18 * s)),
                       (bx + int(20 * s), cy + int(18 * s)), (bx + int(9 * s), cy + int(7 * s))],
                      fill=0)
            d.rectangle([bx + int(26 * s), cy - int(3 * s), bx + int(44 * s), cy + int(3 * s)], fill=0)
            if kind == "vol_up":
                d.rectangle([bx + int(32 * s), cy - int(9 * s), bx + int(38 * s), cy + int(9 * s)], fill=0)
        elif kind == "-5s" or kind == "+5s":
            pass


def ImageChops_equal(a: Image.Image, b: Image.Image) -> bool:
    from PIL import ImageChops
    return ImageChops.difference(a, b).getbbox() is None


def fmt_ms(ms: int) -> str:
    ms = max(0, int(ms))
    return "%d:%02d" % (ms // 60000, (ms // 1000) % 60)


# ===================================================================== 样例数据

def _demo_state(song_id=None):
    """造一个假的播放状态（尽可能挑一首真有歌词的歌），用来预览版式。

    挑歌的优先级（每一步都可能落空，所以必须逐级兜底）：
      1. 指定了 song_id → 就用它（本地找不到就造个占位 Track，版式照样能预览）
      2. 没指定 → 遍历**本机所有歌单**，找歌词行数最多、且带翻译的那首
      3. 一条都没有 → 造一首全假数据的歌（纯占位，不碰任何本地数据）

    ⚠️ 以前这里写死了一个歌单 id。那是**个人数据**（等于把作者的歌单 ID
    提交进公开仓库），而且别人本机没有那个歌单时整条预览路径会直接抛异常。
    现在改成"遍历本机歌单"，既去掉了个人信息，也让 --demo 在任意机器上都能跑。

    ⚠️ 另一处顺手修掉的坑：`_lyrics` / `_np` 这两个名字原先**只**在文件末尾的
    `if __name__ == "__main__":` 块里被绑成模块全局。也就是说这个函数
    以前**只有**用 `python -m music.render_music` 跑才工作 —— 一旦被 import
    （测试、或被别的模块复用）就 NameError: name '_lyrics' is not defined。
    所以这里就地 import，两种情况都能跑。
    """
    from . import lyrics as _lyrics, nowplaying as _np, playlist as _pl

    t = None
    ly = None

    if song_id:
        # 指定了 id 就**必须**渲这首歌 —— 找不到也只能退化成占位曲目，
        # 绝不能悄悄换成别的歌（那样 --demo 12345 出来的图跟 12345 无关）。
        ly = _lyrics.load(song_id)
        try:
            for pl in _pl.playlists():
                if not pl.get("has_tracks"):
                    continue
                for x in _pl.playlist_tracks(pl["id"], limit=400):
                    if x.id == str(song_id):
                        t = x
                        break
                if t:
                    break
        except Exception:
            t = None
    else:
        # 没指定 —— 遍历本机歌单，挑"歌词长 + 带翻译"的那首当样例。
        # 别的机器上可能一个歌单都没有，所以每一步都要能落空。
        best = None
        try:
            for n, pl in enumerate(_pl.playlists()):
                if n >= 20:            # 歌单特别多时别把 --demo 拖成几十秒
                    break
                if not pl.get("has_tracks"):
                    continue
                for x in _pl.playlist_tracks(pl["id"], limit=60):
                    cand = _lyrics.load(x.id)
                    if not cand or len(cand.lines) <= 20:
                        continue
                    score = len(cand.lines) + (100 if cand.has_translation else 0)
                    if best is None or score > best[0]:
                        best = (score, x, cand)
                if best is not None and best[0] >= 100:
                    break          # 已经拿到带翻译的了，不用再翻别的歌单
        except Exception:
            best = None
        if best is not None:
            _s, t, ly = best
        else:
            # 没有歌单可挑 —— 退而用"正在播放的那首"
            try:
                t = _np.current_track()
                if t is not None:
                    ly = _lyrics.load(t.id)
            except Exception:
                t, ly = None, None

    if t is None:
        # 最后兜底：全假数据。不读歌单、不读播放状态，只为了让版式能预览。
        from .nowplaying import Track

        sid = str(song_id or "0")
        t = Track(id=sid,
                  name=("歌曲 %s" % sid) if song_id else "示例歌曲",
                  artist="", album="示例专辑")
        if ly is None:
            try:
                ly = _lyrics.load(sid)
            except Exception:
                ly = None

    # 挑一个歌词中间的位置，让"当前行"上下都有内容
    mid = ly.lines[len(ly.lines) // 2].t_ms if ly and ly.lines else 45000
    return {
        "id": t.id, "name": t.name, "artist": t.artist, "album": t.album or "示例专辑",
        "label": t.label(), "duration_ms": t.duration_ms or (mid + 90000),
        "position_ms": mid, "playing": True, "song_changed": False,
        "offset_ms": 0, "drifted": False, "calibrations": 0,
        "playlist_name": "示例歌单",
    }, ly


# ===================================================================== CLI

if __name__ == "__main__":
    import argparse
    import json as _json

    from . import clock as _clock, lyrics as _lyrics, nowplaying as _np

    ap = argparse.ArgumentParser(description="渲染音乐面板 PNG")
    ap.add_argument("--landscape", action="store_true", help="横版 1200x825")
    ap.add_argument("-o", "--out", default=str(Path(__file__).resolve().parent / "out" / "music.png"))
    ap.add_argument("--lines", type=int, default=7)
    ap.add_argument("--no-trans", action="store_true")
    ap.add_argument("--twice", action="store_true", help="连渲两帧，演示脏矩形")
    ap.add_argument("--demo", metavar="SONG_ID", nargs="?", const="auto",
                    help="用指定歌曲（或自动挑一首有歌词的）渲一张样例图，不读真实播放状态")
    a = ap.parse_args()

    w, h = (1200, 825) if a.landscape else (825, 1200)

    if a.demo:
        st, ly = _demo_state(a.demo if a.demo != "auto" else None)
    else:
        c = _clock.ProgressClock()
        st = c.update()
        st["playlist_name"] = (_np.current_playlist() or {}).get("name", "")
        ly = _lyrics.load(st["id"]) if st["id"] else None

    r = MusicRenderer(w, h, lyric_lines=a.lines, translation=not a.no_trans)
    res = r.render(st, ly)
    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(res.png)
    print("[OK] %s  %dx%d  %d bytes" % (p, w, h, len(res.png)))
    print("     曲目 : %s" % st["label"])
    print("     歌词 : %s" % (
        "%d 行，翻译=%s" % (len(ly.lines), ly.has_translation) if ly else "无"))
    print("     块   : %d 个，变了 %d 个 %s" % (len(res.blocks), len(res.changed), res.changed[:8]))
    print("     分区 : %s" % ", ".join(z.name for z in res.zones))

    if a.twice:
        time.sleep(1.2)
        st = c.update()
        st["playlist_name"] = (_np.current_playlist() or {}).get("name", "")
        res2 = r.render(st, ly)
        print("     第二帧变了 : %s" % (res2.changed or "（没有变化）"))
        print("     脏矩形     : %s" % res2.rects)
