# -*- coding: utf-8 -*-
"""
render.py —— 把看板内容渲染成墨水屏用的 16 阶灰 PNG

设计要点：
- 画布默认竖版 825x1200（海尔 topsir H9 是 1200x825，横版用 --landscape）
- 输出强制量化到 16 阶灰 + 4x4 Bayer 有序抖动，避免大面积渐变出现色阶断层
- 天气 / 新闻 / 热搜由 data_sources 联网取（不在这台老安卓上做），
  本模块**只读缓存、绝不联网** —— 渲染必须能在断网时也把图完整画出来

版面（竖版 825x1200）：
    0-210    日期 + 时间
    230-354  天气（跟日期时间同一栏，不单独起栏）
    392      分隔线
    412-762  全球新闻（NPR，5 条 × 最多 2 行）
    772      分隔线
    784-1128 中文网络热搜（头条/百度，7 条单行）
    1130+    留白 —— 设备实际是 1152 高，底部约 48px 会被切掉

用法：
    python render.py                  # 渲染一次，输出到 out/dash.png
    python render.py --landscape      # 横版 1200x825
    python render.py --rotate 90      # 顺时针旋转 90 度后输出
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 打包成 exe 后 __file__ 在随机临时目录（每次都换名），可写数据搬去
# %APPDATA%\H9Dash\dashboard —— 与 server.py / topsir_gui.py 同一套规则
if getattr(sys, "frozen", False):
    BASE = Path(os.environ.get("APPDATA") or str(Path.home())) / "H9Dash" / "dashboard"
    BASE.mkdir(parents=True, exist_ok=True)
else:
    BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
OUT = BASE / "out"

# ------------------------------------------------------------------ 时间基准
# ★ 为什么渲染要"超前" 3 秒：
#
#   看板上有时间，所以它必须每分钟更新一次；而决定"要不要重画"的是服务端指纹，
#   指纹里含分钟桶。于是有个边界抖动：设备每 4 秒问一次，若某次请求正好卡在
#   :59.x，渲染出来的是旧分钟、上屏 4 秒后分钟桶变了又闪一次 —— 白闪一次。
#
#   解法就是给渲染一个提前量：用 (now + 3s) 当时间基准，指纹也用这个偏移后的
#   分钟。这样卡在 :59.x 的请求会直接渲染出**下一分钟**，4 秒后那次发现分钟桶
#   没变 → 不重画。时钟永不偏慢，也不多闪。
#
#   （和 music 面板的 lyric_lead_ms 是同一个思路：为"画面被看到的时刻"渲染，
#     而不是为"渲染的时刻"渲染。）
RENDER_LEAD_S = 3


def effective_now():
    """渲染用的"当下"。比真实时间超前 RENDER_LEAD_S 秒。"""
    return datetime.now() + timedelta(seconds=RENDER_LEAD_S)


def minute_bucket(dt=None) -> int:
    """
    分钟桶：指纹的一部分。同一分钟内恒定，跨分钟就 +1。

    ⚠️ 服务端算指纹和画图**必须用同一个 dt** —— 否则整分边界上会出现
       "指纹说变了但画出来还是旧分钟"（或反过来），多闪一次。
       所以 Cache 里是先取 now_eff，再把它同时喂给 _fp() 和 render_now()。
    """
    dt = dt or effective_now()
    return int(dt.timestamp()) // 60


# ------------------------------------------------------------------ 外部数据源
# 这三个缓存文件的 mtime 会被 server 的 _fp() 当作指纹的一部分
# （那行 getattr(render, "EXTRA_INPUTS", ()) 就是为这个留的）。
# 新闻/热搜一更新 → 文件 mtime 变 → 指纹变 → 设备刷屏。
try:
    import data_sources as _ds

    EXTRA_INPUTS = tuple(str(_ds._path(k)) for k in ("weather", "news", "hot"))
except Exception:          # 数据源模块没准备好也不能拖垮渲染
    _ds = None
    EXTRA_INPUTS = ()


def fix_console_encoding():
    """
    让中文输出在 Windows 控制台里正常显示，且**永不让 print 拖崩进程**。

    两个坑叠在一起，都在真机上见过：
      1. `◀ ▶ ⇄ ✓ ✗ ─` 这类符号 **GBK 编不出来** ——
         直接 UnicodeEncodeError 打挂 main()，服务/体检根本起不来。
      2. 就算只打中文，进程用 UTF-8 写、控制台按 GBK 解，满屏也是乱码。
         （报错里那句 "gbk codec can't encode" 就是控制台编码被透传进来了。）

    做法：优先把标准输出改成 UTF-8 且遇错不抛；改不动就只保留 errors="replace"。
    调用点放在各入口（server.py / probe.py / 想打中文的脚本）的最前面。

    Windows 上如果还想要更好的控制台表现，可以再 `chcp 65001`；
    但有了这个兜底，**不加也不会崩，顶多是问号**——这才是关键。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass

WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhl.ttc",     # 微软雅黑 Light
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def font(size, bold=False):
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ------------------------------------------------------------------ 16 阶灰抖动
_BAYER4 = [
    [0, 8, 2, 10],
    [12, 4, 14, 6],
    [3, 11, 1, 9],
    [15, 7, 13, 5],
]


def to_16gray(img: Image.Image) -> Image.Image:
    """量化到 16 阶灰，用 4x4 Bayer 有序抖动。纯黑/纯白保持不变。"""
    import numpy as np

    a = np.asarray(img.convert("L")).astype(np.int32)
    h, w = a.shape
    b = np.array(_BAYER4, dtype=np.int32)
    tile = np.tile(b, ((h + 3) // 4, (w + 3) // 4))[:h, :w]

    q = (a * 15 + tile) // 255          # 0..15
    q = np.clip(q, 0, 15)
    out = (q * 17).astype("uint8")       # 0,17,...,255

    return Image.fromarray(out, "L").convert("RGB")   # 输出 RGB，老安卓解码最稳妥


# ------------------------------------------------------------------ 数据
DEFAULT_CONFIG = {
    "city": "广州",
    "news_count": 5,     # 全球新闻显示几条（每条最多 2 行）
    "hot_count": 7,      # 中文热搜显示几条（每条 1 行）
}


def load_config() -> dict:
    """
    读 config.json。**缺的键用默认值补上** —— 老配置文件里没有 news_count 这种新键，
    不补的话 cfg["news_count"] 会 KeyError，而看板是在后台跑的，崩了没人看见。
    """
    cfg = None
    if CONFIG.exists():
        try:
            v = json.loads(CONFIG.read_text(encoding="utf-8"))
            if isinstance(v, dict):
                cfg = v
            else:
                print("[警告] config.json 不是对象，用默认配置")
        except Exception as e:
            print("[警告] config.json 解析失败，用默认：", e)
    if cfg is None:
        CONFIG.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


# ------------------------------------------------------------------ 文本工具
def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F
            or 0xFF00 <= o <= 0xFFEF or 0x3400 <= o <= 0x4DBF)


def _tokens(text: str):
    """
    把文本切成"最小不可断单元"：CJK 逐字，拉丁文按词，空格单独成 token。

    为什么不能直接按字符切英文：会把单词从中间劈开（"associat / e member"），
    墨水屏上非常难读。
    """
    toks, buf = [], ""
    for ch in text:
        if _is_cjk(ch):
            if buf:
                toks.append(buf)
                buf = ""
            toks.append(ch)
        elif ch.isspace():
            if buf:
                toks.append(buf)
                buf = ""
            toks.append(" ")
        else:
            buf += ch
    if buf:
        toks.append(buf)
    return toks


def wrap_lines(d, text: str, f, max_w: int, max_lines: int = 2):
    """
    按像素宽度折行，返回 list[str]（最多 max_lines 行，放不下的加 …）。

    按**像素**量而不是按字符数 —— 中英文混排时字符宽度差一倍还多，
    按字符数切必然有的行溢出、有的行空一半。
    """
    text = (text or "").strip()
    if not text:
        return []
    if d.textlength(text, font=f) <= max_w:
        return [text]

    lines, cur, cut = [], "", False
    for tk in _tokens(text):
        cand = cur + tk
        if d.textlength(cand.strip(), font=f) <= max_w or not cur.strip():
            cur = cand
            continue
        lines.append(cur.rstrip())
        cur = tk.lstrip() if tk.strip() else ""
        if len(lines) >= max_lines:
            cut = True
            break
    if not cut and cur.strip():
        lines.append(cur.rstrip())

    lines = [ln for ln in lines if ln][:max_lines]
    if cut and lines:
        last = lines[-1]
        while last and d.textlength(last + "…", font=f) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def _ellipsis(d, text: str, f, max_w: int) -> str:
    """单行截断：放不下就砍到能放下为止，末尾加 …。"""
    s = (text or "").strip()
    if not s or d.textlength(s, font=f) <= max_w:
        return s
    while s and d.textlength(s + "…", font=f) > max_w:
        s = s[:-1]
    return s.rstrip() + "…"


# ------------------------------------------------------------------ 数据源读取
def _weather():
    """从缓存读天气。必须是 dict 才往下走 —— 缓存里躺着个 str 就当没有。"""
    if _ds is None:
        return None
    w = _ds.get_cached("weather")
    return w if isinstance(w, dict) else None


def _items(kind: str):
    """从缓存读列表型数据（news / hot）。非 list 一律当空。"""
    if _ds is None:
        return []
    v = _ds.get_cached(kind)
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, dict)]


# ------------------------------------------------------------------ 绘制
def draw_dashboard(cfg: dict, width: int, height: int, now=None) -> Image.Image:
    """
    画整张看板。

    ⚠️ now 由调用方传进来（服务端传的是它算指纹用的同一个时刻）。
       不传就自己取 —— 但那样在整分边界上可能出现"指纹说变了、画出来还是旧分钟"，
       白闪一次。见文件开头 minute_bucket 的注释。

    ⚠️ 安全底边 SAFE_BOTTOM：设备实际是 825x1152，服务端按 1200 渲染，
       底部约 48 px 会被切掉。所有内容必须在 SAFE_BOTTOM 之上。
    """
    img = Image.new("L", (width, height), 255)
    d = ImageDraw.Draw(img)

    pad = 36
    now = now or effective_now()
    max_w = width - pad * 2
    safe_bottom = 1130

    f_date = font(44)
    f_wday = font(30)
    f_time = font(96)
    f_small = font(24)
    f_mid = font(34)
    f_big = font(64)
    f_sec = font(34)          # 板块标题
    f_news = font(23)
    f_hot = font(27)

    # ============================================ 第一栏：日期时间 + 天气
    d.rectangle([0, 0, width, 6], fill=0)

    d.text((pad, 34), "%d月%d日" % (now.month, now.day), font=f_date, fill=0)
    d.text((pad, 88), "%d年  %s" % (now.year, WEEK[now.weekday()]), font=f_wday, fill=60)

    t = now.strftime("%H:%M")
    d.text((width - pad - d.textlength(t, font=f_time), 30), t, font=f_time, fill=0)

    # 天气：紧跟在日期时间下面，属于同一栏，不再单独起一栏
    y = 230
    city = str(cfg.get("city") or "").strip()
    d.text((pad, y), "天气 · %s" % city if city else "天气", font=f_small, fill=90)

    w = _weather()
    if w and w.get("temp") is not None:
        temp = "%s°" % w["temp"]
        d.text((pad, y + 26), temp, font=f_big, fill=0)
        tx = pad + d.textlength(temp, font=f_big) + 22
        desc = str(w.get("desc") or "")
        if desc:
            d.text((tx, y + 40), desc, font=f_mid, fill=0)
        hi, lo = w.get("high"), w.get("low")
        if hi is not None or lo is not None:
            parts = []
            if hi is not None:
                parts.append("最高 %s°" % hi)
            if lo is not None:
                parts.append("最低 %s°" % lo)
            d.text((pad, y + 100), "  ".join(parts), font=f_small, fill=90)
    else:
        d.text((pad, y + 30), "（暂无天气数据）", font=f_mid, fill=120)

    d.line([pad, 392, width - pad, 392], fill=0, width=3)

    # ============================================ 第二栏：全球新闻
    y = 412
    d.text((pad, y), "全球新闻", font=f_sec, fill=0)

    news = _items("news")
    n_news = max(1, min(8, int(cfg.get("news_count", 5) or 5)))
    y = 462
    bullet_w = 24
    slot = 60                      # 每条固定占 60（最多 2 行 × 28）
    for i in range(n_news):
        if y + slot > safe_bottom:
            break
        if i < len(news):
            d.text((pad, y), "·", font=f_news, fill=110)
            for j, ln in enumerate(
                    wrap_lines(d, str(news[i].get("title", "")), f_news,
                               max_w - bullet_w, 2)):
                d.text((pad + bullet_w, y + j * 28), ln, font=f_news, fill=0)
        y += slot
    if not news:
        d.text((pad, 462), "（暂无）", font=f_small, fill=120)

    # ============================================ 第三栏：中文网络热搜
    d.line([pad, 772, width - pad, 772], fill=0, width=3)
    d.text((pad, 784), "中文网络热搜", font=f_sec, fill=0)

    hot = _items("hot")
    n_hot = max(1, min(10, int(cfg.get("hot_count", 7) or 7)))
    y = 834
    num_w = 46
    row = 42
    for i in range(n_hot):
        if y + row > safe_bottom:
            break
        if i < len(hot):
            d.text((pad, y + 2), "%d." % (i + 1), font=f_hot, fill=110)
            d.text((pad + num_w, y),
                   _ellipsis(d, str(hot[i].get("title", "")), f_hot, max_w - num_w),
                   font=f_hot, fill=0)
        y += row
    if not hot:
        d.text((pad, 834), "（暂无）", font=f_small, fill=120)

    return to_16gray(img)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--landscape", action="store_true", help="横版 1200x825")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    ap.add_argument("-o", "--out", default=str(OUT / "dash.png"))
    args = ap.parse_args()

    w, h = (1200, 825) if args.landscape else (825, 1200)
    cfg = load_config()

    # 单独跑 render.py 时（不是服务在跑），缓存可能还是空的/过期的，
    # 这里同步刷一次，保证出图有内容。服务模式下由后台线程负责，不走这条。
    if _ds is not None:
        try:
            _ds.refresh_all(city=str(cfg.get("city") or ""))
        except Exception as e:
            print("[警告] 刷新数据源失败（用旧缓存继续）：", e)

    img = draw_dashboard(cfg, w, h, now=effective_now())
    if args.rotate:
        img = img.rotate(-args.rotate, expand=True)

    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    img.save(p, "PNG", optimize=True)
    print("[OK] %s  %dx%d  %d bytes" % (p, img.size[0], img.size[1], p.stat().st_size))


if __name__ == "__main__":
    main()
