# -*- coding: utf-8 -*-
"""
data_sources.py —— 看板的联网数据源：天气 / 全球新闻 / 中文网络热搜

几条铁律（和 music/lyrics.py 保持一致）：

1. **只用标准库 urllib。** requirements.txt 里只有 pillow 和 numpy，没有 requests，
   也不打算为了三个接口加一个依赖。

2. **取数失败绝不抛异常。** 失败返回 None，调用方留着上一次的好缓存。
   墨水屏看板是"挂在那儿一直看"的东西，一次网络抖动就把整张图打挂是不可接受的。

3. **渲染路径绝不联网。** draw_dashboard 只读缓存；真正发请求的是 server.py 里的
   后台刷新线程。否则设备每 4 秒拉一次图，每一次都可能被网络请求拖住几秒。

4. **缓存必须原子写**（写 .tmp 再 os.replace）。渲染线程和刷新线程并发访问同一个
   JSON，读到写了一半的文件会让整张图渲染失败 —— 而且这种失败是偶发的，极难查。

5. **只在数据真变了才落盘。** 缓存文件的 mtime 是服务端指纹的一部分（EXTRA_INPUTS），
   每次刷新都重写 = 每小时骗出一次多余的整屏闪。

三个源都是**免密钥**的：
    天气   Open-Meteo      中文城市名可直接地理编码
    新闻   NPR World RSS   英文，10 条
    热搜   头条热榜 JSON（主，50 条）→ 百度热搜（兜底，从 HTML 注释里抠 JSON，51 条）

实测在本机被网络挡住 / 需要凭据、因此**没有采用**的源（换台机器可能又能用）：
    BBC 中文、BBC World、路透、卫报、Al Jazeera、Google News、DW、RFI、NHK → 502
    微博热搜 → 403（要 cookie）     知乎热榜 → 401（要 cookie）
    央视国际、联合早报 → 404

用法：
    python -m dashboard.data_sources        # 三个源各刷一次并打印结果
"""
from __future__ import annotations

import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 打包成 exe 后 __file__ 在随机临时目录，缓存改去 %APPDATA%（同 server.py）
if getattr(sys, "frozen", False):
    BASE = Path(os.environ.get("APPDATA") or str(Path.home())) / "H9Dash" / "dashboard"
    BASE.mkdir(parents=True, exist_ok=True)
else:
    BASE = Path(__file__).resolve().parent
CACHE = BASE / "cache"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 12

# 各自的刷新周期（秒）。失败时按 RETRY_TTL 提前重试。
TTL = {"weather": 1800, "news": 3600, "hot": 3600}
RETRY_TTL = 600

# 进程内的"下次再试"时刻。放在内存里而不是写进文件，
# 是为了让「内容没变就不落盘」和「别反复重试」这两件事互不干扰。
_next_try: dict = {}

# 兼容一部分老机器上缺根证书导致 urllib 直接报 CERTIFICATE_VERIFY_FAILED 的情况。
# 看板只是读公开信息，这里宁可放宽校验也要保证"能刷出来"。
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


# ------------------------------------------------------------------ 天气：WMO 代码
# Open-Meteo 返回的是 WMO 4677 天气代码，得自己翻成中文。
WMO_CODE = {
    0: "晴", 1: "大部晴朗", 2: "局部多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}


def wmo_desc(code) -> str:
    """WMO 代码 → 中文。不认识的码给个兜底，不要返回空串让版面开天窗。"""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return ""
    return WMO_CODE.get(c, "未知")


# ------------------------------------------------------------------ HTTP
def _get(url: str, timeout: int = TIMEOUT):
    """
    发一次 GET，成功返回 bytes，失败返回 None。

    注意这里**不抛**：HTTPError / URLError / 超时 / 解不开的 body 全部吞掉。
    看板要的是"取不到就用旧的"，不是"取不到就崩"。
    """
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return r.read()
    except Exception:
        return None


def _get_json(url: str, timeout: int = TIMEOUT):
    raw = _get(url, timeout)
    if not raw:
        return None
    try:
        v = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    return v if isinstance(v, dict) else None


def _get_text(url: str, timeout: int = TIMEOUT) -> str:
    raw = _get(url, timeout)
    return raw.decode("utf-8", "replace") if raw else ""


# ------------------------------------------------------------------ 缓存
def _path(kind: str) -> Path:
    return CACHE / ("%s.json" % kind)


def _load(kind: str):
    """
    读缓存，返回 {"at": float, "src": str, "data": ...} 或 None。

    ★ 必须校验类型：磁盘上是别人（历史版本、手工编辑）写的也算，
      一旦漏出去一个 str，报错会出现在几百行之外的渲染代码里。
    """
    try:
        v = json.loads(_path(kind).read_text(encoding="utf-8"))
    except Exception:
        return None
    return v if isinstance(v, dict) else None


def _save(kind: str, src: str, data) -> None:
    """原子写：先 .tmp 再 replace，绝不让渲染线程读到半截文件。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    p = _path(kind)
    tmp = p.with_name(p.name + ".tmp")
    try:
        tmp.write_text(json.dumps({"at": time.time(), "src": src, "data": data},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass


def _save_if_changed(kind: str, src: str, data) -> bool:
    """
    只在内容真的变了才写盘，返回是否写了。

    为什么：缓存文件的 mtime 被 server 的 _fp() 当作指纹的一部分。
    每次刷新都无条件重写 → 内容没变也会让指纹变 → 设备多闪一次整屏。
    """
    old = _load(kind)
    if isinstance(old, dict) and old.get("data") == data and old.get("src") == src:
        return False
    _save(kind, src, data)
    return True


def needs_refresh(kind: str, ttl: int = None) -> bool:
    """这个源现在该不该去网上取一次？"""
    ttl = ttl if ttl is not None else TTL.get(kind, 3600)
    if time.time() < _next_try.get(kind, 0.0):
        return False
    c = _load(kind)
    at = c.get("at") if isinstance(c, dict) else None
    if not isinstance(at, (int, float)):
        return True
    return (time.time() - at) >= ttl


def get_cached(kind: str):
    """
    渲染专用：只读缓存，返回 data 部分。**绝不联网。**

    渲染函数必须能在没有任何网络的情况下把图完整画出来，
    最多是某个板块显示"（暂无）"。
    """
    c = _load(kind)
    if not isinstance(c, dict):
        return None
    return c.get("data")


def cached_at(kind: str) -> float:
    c = _load(kind)
    at = c.get("at") if isinstance(c, dict) else None
    return float(at) if isinstance(at, (int, float)) else 0.0


# ------------------------------------------------------------------ 天气
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search?name=%s&count=1&language=zh&format=json"
FCST_URL = ("https://api.open-meteo.com/v1/forecast?latitude=%.5f&longitude=%.5f"
            "&current=temperature_2m,weather_code"
            "&daily=temperature_2m_max,temperature_2m_min"
            "&timezone=auto&forecast_days=1")


def fetch_weather(city: str):
    """
    城市名 → 经纬度 → 当前天气。返回 dict 或 None。

    两步走：Open-Meteo 的预报接口只认经纬度，所以先用它的地理编码接口把
    中文城市名翻成坐标（实测"广州"能查到 23.11667, 113.25）。
    """
    city = (city or "").strip()
    if not city:
        return None

    geo = _get_json(GEO_URL % urllib.parse.quote(city))
    results = (geo or {}).get("results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    if not isinstance(first, dict):
        return None
    try:
        lat = float(first["latitude"])
        lon = float(first["longitude"])
    except (KeyError, TypeError, ValueError):
        return None

    fc = _get_json(FCST_URL % (lat, lon))
    if not isinstance(fc, dict):
        return None
    cur = fc.get("current")
    if not isinstance(cur, dict):
        return None

    high = low = None
    daily = fc.get("daily")
    if isinstance(daily, dict):
        for k, dst in (("temperature_2m_max", "high"), ("temperature_2m_min", "low")):
            v = daily.get(k)
            if isinstance(v, list) and v and isinstance(v[0], (int, float)):
                if dst == "high":
                    high = v[0]
                else:
                    low = v[0]

    temp = cur.get("temperature_2m")
    if not isinstance(temp, (int, float)):
        return None

    return {
        "city": city,
        "temp": round(float(temp)),
        "code": cur.get("weather_code"),
        "desc": wmo_desc(cur.get("weather_code")),
        "high": None if high is None else round(float(high)),
        "low": None if low is None else round(float(low)),
    }


# ------------------------------------------------------------------ 全球新闻（NPR）
NEWS_URL = "https://feeds.npr.org/1004/rss.xml"


def _unescape(s: str) -> str:
    """
    解 HTML 实体 → 去标签 → 压空白。NPR 的标题里 &apos; &quot; 到处都是。

    ⚠️ 顺序不能反：必须先 unescape **再**剥标签。
       标题里经常出现被转义过的标签（&lt;em&gt;tagged&lt;/em&gt;）——
       先剥标签的话，这时候它们还是 "&lt;em&gt;" 这种字面量，匹配不到 `<...>`，
       unescape 之后反而变成了真标签留在文本里，屏上就印出 "<em>tagged</em>"。
       反过来做（先 unescape 再剥）才能既解实体又去标签。
       最后再剥一次，兜住"实体里套标签"的多层情况。
    """
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]*>", "", s)
    s = html.unescape(s)
    s = re.sub(r"<[^>]*>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_news_rss(text: str, limit: int = 8):
    """
    从 NPR 的 RSS 里抠标题。用正则而不是 ElementTree：
    RSS 里那些 content:encoded 之类的命名空间在 ElementTree 上很啰嗦，
    而我们要的只是 <item> 里的 <title>。
    """
    out = []
    if not text:
        return out
    for block in re.findall(r"<item[\s>].*?</item>", text, re.S):
        m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
        if not m:
            continue
        t = _unescape(m.group(1))
        if not t:
            continue
        out.append({"title": t})
        if len(out) >= limit:
            break
    return out


def fetch_news(limit: int = 8):
    items = parse_news_rss(_get_text(NEWS_URL), limit)
    return items or None


# ------------------------------------------------------------------ 中文热搜
HOT_TOUTIAO = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
HOT_BAIDU = "https://top.baidu.com/board?tab=realtime"


def _coerce_num(v):
    """
    热度值在两个源里的类型不一样：头条给的是**字符串**（"14385460"），
    百度给的是数字。这里统一成 int，取不到就 None。

    ⚠️ 别写 isinstance(v, (int, float)) 然后就完了 —— 头条那边会全变成 None，
    而且**不报错**，只是热度这一列永远空着，很晚才会被发现。
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def parse_hot_toutiao(obj, limit: int = 10):
    """头条：干净的 JSON。data[].Title / .HotValue（注意是字符串）"""
    out = []
    data = (obj or {}).get("data")
    if not isinstance(data, list):
        return out
    for x in data:
        if not isinstance(x, dict):
            continue
        t = str(x.get("Title") or "").strip()
        if not t:
            continue
        out.append({"title": t, "hot": _coerce_num(x.get("HotValue"))})
        if len(out) >= limit:
            break
    return out


def parse_hot_baidu(text: str, limit: int = 10):
    """
    百度：没有 JSON 接口，榜单数据藏在 HTML 的 `<!--s-data:{...}-->` 注释里。

    这种解析方式是脆的 —— 百度改版就会瞎。所以它只当兜底，
    头条挂了才轮到它，解析失败就当没有，绝不让整张图渲染失败。
    """
    out = []
    m = re.search(r"<!--s-data:(.*?)-->", text or "", re.S)
    if not m:
        return out
    try:
        obj = json.loads(m.group(1))
    except Exception:
        return out
    cards = (obj or {}).get("data", {}).get("cards")
    if not isinstance(cards, list):
        return out
    for c in cards:
        if not isinstance(c, dict):
            continue
        for it in (c.get("content") or []):
            if not isinstance(it, dict):
                continue
            t = str(it.get("word") or "").strip()
            if not t:
                continue
            out.append({"title": t, "hot": _coerce_num(it.get("hotScore"))})
            if len(out) >= limit:
                return out
    return out


def fetch_hot(limit: int = 10):
    """头条为主，百度兜底。两个都失败返回 None（调用方留旧缓存）。"""
    items = parse_hot_toutiao(_get_json(HOT_TOUTIAO), limit)
    if items:
        return items, "toutiao"
    items = parse_hot_baidu(_get_text(HOT_BAIDU), limit)
    if items:
        return items, "baidu"
    return None, None


# ------------------------------------------------------------------ 统一刷新入口
def refresh_one(kind: str, city: str = "", force: bool = False):
    """
    刷一个源。返回 (ok: bool, wrote: bool)。

    ok    = 这次从网上取到数据了（或强制跳过，见下）
    wrote = 缓存文件被改写了（内容有变）

    取数失败时**不覆盖旧缓存**，只把"下次再试"往后推 RETRY_TTL。
    """
    ttl = TTL.get(kind, 3600)
    if not force and not needs_refresh(kind, ttl):
        return False, False

    try:
        if kind == "weather":
            data = fetch_weather(city)
            src = "open-meteo"
        elif kind == "news":
            data = fetch_news()
            src = "npr"
        elif kind == "hot":
            data, src = fetch_hot()
        else:
            return False, False
    except Exception:
        # 兜底：任何没想到的异常都不能把刷新线程弄死
        data, src = None, None

    if not data:
        _next_try[kind] = time.time() + RETRY_TTL
        return False, False

    wrote = _save_if_changed(kind, src, data)
    _next_try[kind] = time.time() + ttl
    return True, wrote


def refresh_all(city: str = "", force: bool = False) -> dict:
    """
    三个源各刷一次，返回 {kind: (ok, wrote)}。

    给 server 的后台线程和 GUI 的「立即刷新」按钮用。
    GUI 那边要放到子线程里调 —— 三个源最坏情况要 36 秒，不能卡住界面。
    """
    out = {}
    for kind in ("weather", "news", "hot"):
        try:
            out[kind] = refresh_one(kind, city=city, force=force)
        except Exception:
            out[kind] = (False, False)
    return out


def status(city: str = "") -> dict:
    """给 GUI 显示用：每个源的上次更新时间、来源、条数。"""
    out = {}
    for kind in ("weather", "news", "hot"):
        c = _load(kind)
        d = c.get("data") if isinstance(c, dict) else None
        n = len(d) if isinstance(d, list) else (1 if isinstance(d, dict) else 0)
        out[kind] = {
            "at": cached_at(kind),
            "src": (c or {}).get("src", ""),
            "count": n,
            "stale": needs_refresh(kind),
        }
    out["city"] = city
    return out


if __name__ == "__main__":
    import sys

    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    city = sys.argv[1] if len(sys.argv) > 1 else "广州"
    print("刷新三个源（城市=%s）..." % city)
    for k, (ok, wrote) in refresh_all(city=city, force=True).items():
        print("  %-8s ok=%-5s wrote=%-5s  %s" % (k, ok, wrote, get_cached(k)))
