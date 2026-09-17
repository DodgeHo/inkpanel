# -*- coding: utf-8 -*-
"""
server.py —— 给 H9 看板客户端提供图片和指令的局域网 HTTP 服务

两个面板共用这一个服务：
  · 信息看板   GET  /dash.png           （原有）
  · 音乐面板   GET  /music.png          （整帧）
               GET  /music.png?crop=x,y,w,h  （只要一小块，调试用）
               GET  /music/crops?from=N     （★ 设备用：一次拿到所有脏块 + 触摸分区）
               GET  /music/meta         （脏矩形 / 触摸分区 / 下次刷新时刻）
               POST /cmd                （{"action":"next"} 等）

关于 /music/crops?from=N（N = 设备手里那张图的 frame_id）：
    懒刷新的脏矩形是"相对上一帧"算出来的，所以设备必须刚好持有第 N 帧。
      1. N == 服务端当前帧号      → 无新帧，什么都不用做
      2. N == 服务端当前帧号 - 1  → 返回每个脏块的 PNG（base64），原子、一次请求
      3. 其它（首次访问 / 掉帧）  → 直接回整帧 PNG，设备重铺底图
    把判断放在服务端，设备端逻辑就只剩下"拼接"。

- 明文 HTTP（安卓 4.0.4 只有 TLS 1.0，别上 HTTPS）
- 带随机 token 校验，别把端口暴露到公网

用法：
    python server.py                      # 默认 0.0.0.0:8765
    python server.py --port 9000
    python server.py --landscape          # 横版
    python server.py --no-music           # 只跑信息看板
"""
import argparse
import base64
import io
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 打包成单文件 exe 后，__file__ 在一个每次都换名的随机临时目录里，
# 可写数据（config / 缓存 / 输出图）改去 %APPDATA%\H9Dash\dashboard，
# 和 GUI 进程重定向到同一个地方，两边看到同一份数据。
if getattr(sys, "frozen", False):
    BASE = Path(os.environ.get("APPDATA") or str(Path.home())) / "H9Dash" / "dashboard"
    BASE.mkdir(parents=True, exist_ok=True)
else:
    BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))          # frozen 时插的是数据目录，无害 ——
sys.path.insert(0, str(BASE.parent))   # 模块导入由打包器的导入器负责

import render  # noqa: E402

render.fix_console_encoding()   # 必须在任何 print 之前：GBK 控制台下中文会打崩进程

try:
    import data_sources  # noqa: E402
except Exception:         # 没有它看板就只有时间会动，不至于起不来
    data_sources = None

TOKEN_FILE = Path.home() / ".h9dash" / "token"
TTL = 45          # 秒；小于这个间隔就直接返回缓存图


class Refresher(threading.Thread):
    """
    后台刷新看板的联网数据（天气 / 新闻 / 热搜），每 60 秒醒一次。

    为什么不在渲染时取数：设备每 4 秒拉一次图，渲染是**按需**触发的 ——
    把网络请求塞进渲染路径，等于每次设备轮询都可能被网络拖住几秒，
    网络一抖设备就取不到图。所以取数归这里，渲染只读缓存。

    各源的周期（30 分钟 / 1 小时）和失败重试（10 分钟）写在
    data_sources.refresh_one 里；这里只负责"定时叫醒"。
    """

    def __init__(self, city_getter, interval: float = 60.0):
        super().__init__(name="dash-refresher", daemon=True)
        self.city_getter = city_getter   # 每次现取 —— GUI 里改了城市立刻生效
        self.interval = interval
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        if data_sources is None:
            print("[数据源] 模块加载失败，联网栏目将显示（暂无）")
            return
        # 启动先刷一轮：缓存是空的话，设备头一分钟会看到"（暂无）"
        try:
            r = data_sources.refresh_all(city=self.city_getter())
            print("[数据源] 首刷 " + "  ".join(
                "%s=%s" % (k, "ok" if ok else "失败") for k, (ok, _w) in r.items()))
        except Exception as e:
            print("[数据源] 首刷出错（不影响服务启动）：", e)
        while not self._stop.wait(self.interval):
            try:
                data_sources.refresh_all(city=self.city_getter())
            except Exception as e:
                # 刷新线程绝不能死：死了新闻就永远停在上一次
                print("[数据源] 刷新出错：", e)


class PanelState:
    """
    "当前该显示哪个面板" —— 这个决定权在**电脑**，不在设备。

    为什么要有这么个东西：面板模式原先只写在设备的 dashboard.conf 里，
    想切换就得插 USB 改文件、再重开 App。而实际上用户就坐在电脑前，
    让他为了一句话去碰设备显然是设计错了。

    现在设备每轮取图前问一次这里，所以：
      · 电脑侧点一下切换 → 设备下一轮（几秒内）自动切过去
      · 设备长按菜单切换  → 走 POST 改的也是这里，电脑侧看到的立刻同步
      · 两边看到的状态永远一致，没有"谁是权威"的问题

    seq 每次切换自增，方便设备判断"面板真的换了"还是"只是又轮询了一次"。
    """

    def __init__(self, current: str = "dash"):
        self._lock = threading.Lock()
        self._current = current if current in ("dash", "music") else "dash"
        self._seq = 0
        self._changed_at = time.time()
        self._enabled = {"dash": True, "music": True}

    def set_enabled(self, dash: bool, music: bool):
        with self._lock:
            self._enabled["dash"] = bool(dash)
            self._enabled["music"] = bool(music)
            # 当前面板被禁用了就自动切到另一个
            if not self._enabled.get(self._current):
                for alt in ("music", "dash"):
                    if self._enabled.get(alt):
                        self._current = alt
                        self._seq += 1
                        self._changed_at = time.time()
                        break

    def available(self):
        with self._lock:
            return [k for k in ("dash", "music") if self._enabled.get(k)]

    def get(self) -> str:
        with self._lock:
            return self._current

    def set(self, name: str):
        name = (name or "").strip().lower()
        if name not in ("dash", "music"):
            raise ValueError("panel must be 'dash' or 'music'")
        with self._lock:
            if not self._enabled.get(name):
                raise ValueError("panel '%s' is disabled" % name)
            if name != self._current:
                self._current = name
                self._seq += 1
                self._changed_at = time.time()

    def toggle(self) -> str:
        with self._lock:
            other = "music" if self._current == "dash" else "dash"
            if self._enabled.get(other):
                return other
            return self._current

    def snapshot(self) -> dict:
        with self._lock:
            return {"panel": self._current, "seq": self._seq,
                    "changed_at": self._changed_at,
                    "available": [k for k in ("dash", "music") if self._enabled.get(k)]}


def get_token() -> str:
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    t = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    return t


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class Cache:
    """
    信息看板的图片缓存 + **变化检测**。

    变化检测是为了配合"短轮询、只在变时刷新"：
    设备每 4 秒问一次，但只有画面内容真的变了才值得让它刷一次墨水屏
    （每次刷屏都会闪一下，白闪是没法忍的）。

    怎么判断"变了"：把渲染用的输入做个指纹 ——
      · config.json 的 mtime + 大小
      · 天气 / 新闻 / 热搜三个缓存文件的 mtime + 大小（render.EXTRA_INPUTS）
      · ★ 分钟桶（render.minute_bucket）—— **没有它看板永远不会自己更新**：
        旧版指纹里没有时间，于是"指纹没变 → 回缓存不重画"恒成立，
        屏上的时钟是卡死的，只有改 config.json 才会动一下。
    指纹不同就重画，相同就直接告诉设备"没变，别刷了"。

    分钟桶配合 render.RENDER_LEAD_S（3 秒提前量）：指纹和画面用**同一个**
    偏移后的时刻（见 get()），既保证每分钟恰好闪一次，又不会在整分边界上
    因为请求卡在 :59.x 而多闪一次。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.png = None
        self.at = 0.0
        self.fingerprint = ""
        self.rev = 0            # 内容版本号，变了才自增；设备拿它判断要不要刷

    def _fp(self, now_eff) -> str:
        """渲染输入的指纹。任何影响画面的东西变了，这里都要能体现出来。"""
        parts = []
        try:
            st = render.CONFIG.stat()
            parts.append("%d:%.0f" % (st.st_size, st.st_mtime))
        except Exception:
            parts.append("-")
        # 看板可能读的外部数据（天气/新闻/热搜缓存），有就带上（没有就跳过）
        for extra in getattr(render, "EXTRA_INPUTS", ()):
            try:
                st = Path(extra).stat()
                parts.append("%s=%d:%.0f" % (Path(extra).name, st.st_size, st.st_mtime))
            except Exception:
                pass
        # ★ 分钟桶：时间也在画面里，就得进指纹
        parts.append("m=%d" % render.minute_bucket(now_eff))
        return "|".join(parts)

    def render_now(self, w, h, rotate, now_eff=None):
        """强制重画（不看缓存），返回 png bytes。"""
        cfg = render.load_config()
        img = render.draw_dashboard(cfg, w, h, now=now_eff)
        if rotate:
            img = img.rotate(-rotate, expand=True)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue()

    def get(self, w, h, rotate, force=False):
        """
        返回 (png, changed, rev)。
        changed=True 表示相对上一次调用，画面内容变了（设备该刷屏）。
        """
        with self.lock:
            now = time.time()
            # ★ 先定下"这一轮用哪个时刻"，指纹和渲染都吃它。
            #   分开取的话，整分边界上可能指纹算的是新分钟、画出来是旧分钟
            #   （或反过来），设备就会多闪一次。
            now_eff = render.effective_now()
            fp = self._fp(now_eff)
            fresh = (self.png is not None and (now - self.at) < TTL
                     and fp == self.fingerprint)
            if fresh and not force:
                return self.png, False, self.rev

            # 指纹没变但缓存过期 → 内容其实一样，延长缓存即可，不必重画
            if self.png is not None and fp == self.fingerprint and not force:
                self.at = now
                return self.png, False, self.rev

            png = self.render_now(w, h, rotate, now_eff=now_eff)
            content_changed = (fp != self.fingerprint) or (self.png is None)
            if self.png is not None and not content_changed:
                # 指纹没变，但强制重画了 —— 比一下字节，避免误报"变了"
                content_changed = (png != self.png)
            self.png = png
            self.at = now
            self.fingerprint = fp
            if content_changed:
                self.rev += 1
            return self.png, content_changed, self.rev


class Handler(BaseHTTPRequestHandler):
    server_version = "H9Dash/1.2"
    cache: Cache = None
    token: str = ""
    width = 825
    height = 1200
    rotate = 0
    music = None          # music.service.MusicService 或 None
    panel: "PanelState" = None

    def log_message(self, fmt, *a):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % a))

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    @staticmethod
    def _parse_qs(qs):
        params = {}
        for kv in qs.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                params[k] = v
        return params

    def _authed(self, params) -> bool:
        return params.get("t") == self.token

    @staticmethod
    def _lan_ip() -> str:
        """
        猜本机在局域网里的地址，用于 /health 里给设备报门牌号。

        用一个 UDP connect 把路由选出来 —— 不实际发包，只是让内核挑出口网卡，
        所以不需要联网，也不会被防火墙拦。失败就退回 127.0.0.1。
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()

    # ------------------------------------------------------------ GET

    def do_GET(self):
        path = self.path.split("?")[0]
        params = self._parse_qs(self.path.split("?")[1] if "?" in self.path else "")

        if path in ("/", "/health"):
            info = {"ok": True, "ts": int(time.time()), "service": "H9Dash",
                    "server_version": self.version_string(),
                    "endpoints": ["/dash.png", "/health", "/panel"]}
            ip = self._lan_ip()
            info["lan_ip"] = ip
            info["discover"] = "H9Dash v2"      # 设备端靠这个字段认人
            if self.music is not None:
                info["endpoints"] += ["/music.png", "/music/crops?from=N", "/music/meta",
                                      "/music/status", "/cmd(POST)", "/panel(POST)"]
                info["music_url"] = "http://%s:%d/music.png?t=%s" % (ip, self.server.server_port, self.token)
            info["dash_url"] = "http://%s:%d/dash.png?t=%s" % (ip, self.server.server_port, self.token)
            if self.panel is not None:
                info.update(self.panel.snapshot())
            self._json(200, info)
            return

        # ------------------------------------------------ 当前该显示哪个面板
        # 设备每轮取图前问这个，所以电脑侧一切换，设备几秒内就跟上了。
        # 免 token：内容不含隐私，只是"现在该看哪张图"，而且设备需要在
        # 鉴权之前就知道该去拉哪个端点。（与 /health 同理）
        if path == "/panel":
            if self.panel is None:
                self._json(200, {"panel": "dash", "seq": 0, "available": ["dash"]})
                return
            self._json(200, self.panel.snapshot())
            return

        if path == "/dash.png":
            if not self._authed(params):
                self._send(403, b"bad token")
                return
            # want_rev：设备手里那张图的版本号。给了就顺带告诉它"要不要刷"。
            want_rev = params.get("rev")
            force = params.get("force") == "1"
            try:
                png, changed, rev = self.cache.get(self.width, self.height, self.rotate,
                                                   force=force)
            except Exception as e:
                self._send(500, ("render failed: %s" % e).encode("utf-8"))
                return
            if want_rev is not None:
                try:
                    if int(want_rev) == rev and not force:
                        # 内容没变 → 回一个小 JSON，让设备别刷屏（省一次闪屏）
                        self._json(200, {"unchanged": True, "rev": rev})
                        return
                except ValueError:
                    pass
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("X-H9Dash-Rev", str(rev))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(png)
            except Exception:
                pass
            return

        # ---------------------------------------------------- 音乐面板
        if path in ("/music.png", "/music/meta", "/music/crops", "/music/status",
                    "/music/queue", "/music/playlists"):
            if self.music is None:
                self._send(404, b"music panel disabled (--no-music)")
                return
            if not self._authed(params):
                self._send(403, b"bad token")
                return
            try:
                if path == "/music/meta":
                    res = self.music.frame()
                    self._json(200, res.meta())
                    return
                if path == "/music/crops":
                    self._send_crops(params)
                    return
                if path == "/music/status":
                    self._json(200, self.music.status())
                    return
                if path == "/music/queue":
                    n = int(params.get("n") or 30)
                    self._json(200, {"queue": self.music.queue(n)})
                    return
                if path == "/music/playlists":
                    self._json(200, {"playlists": self.music.playlists()})
                    return

                res = self.music.frame()
                crop = params.get("crop")
                if crop:
                    try:
                        x, y, w, h = [int(v) for v in crop.split(",")]
                        png = res.crop_png(x, y, w, h)
                    except Exception as e:
                        self._send(400, ("bad crop: %s" % e).encode("utf-8"))
                        return
                    if not png:
                        self._send(400, b"empty crop")
                        return
                    self._send(200, png, "image/png")
                    return
                self._send(200, res.png, "image/png")
            except Exception as e:
                self._send(500, ("music render failed: %r" % (e,)).encode("utf-8"))
            return

        self._send(404, b"not found")

    # ------------------------------------------------- 音乐面板：脏块打包

    def _send_crops(self, params) -> None:
        """一次请求返回"设备要补的所有脏块"，原子且省往返。

        设备带 ?from=<它手里的帧号>：
            from == 当前帧号     → up_to_date，什么都不用传
            from == 当前帧号 - 1 → rects（每块一段 base64 PNG）
            其它                 → full（整帧 PNG）
        """
        try:
            want = int(params.get("from", "-1"))
        except ValueError:
            want = -1

        res = self.music.frame()
        fid = int(getattr(res, "frame_id", 0))
        base = {
            "frame_id": fid,
            "size": {"w": res.size[0], "h": res.size[1]},
            "next_change_ms": res.next_change_ms,
            "lyric_next_ms": getattr(res, "lyric_next_ms", None),
            "sync": getattr(res, "sync", {}) or {},
            "zones": [z.as_dict() for z in res.zones],
        }

        if want == fid:
            base.update({"full": False, "up_to_date": True, "changes": [], "rects": []})
            self._json(200, base)
            return

        if want == fid - 1 and not res.full:
            rects = []
            for r in res.rects:
                x0, y0, x1, y1 = r
                png = res.crop_png(x0, y0, x1 - x0, y1 - y0)
                if not png:
                    continue
                rects.append({"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
                              "png": base64.b64encode(png).decode("ascii")})
            if rects:
                base.update({"full": False, "up_to_date": False,
                             "changes": res.changed, "rects": rects})
                self._json(200, base)
                return
            # 没有任何脏块 —— 也算"跟上进度了"
            base.update({"full": False, "up_to_date": True, "changes": [], "rects": []})
            self._json(200, base)
            return

        # 首次 / 掉帧 / 服务端刚整屏重画过 —— 直接给整帧
        base.update({"full": True, "up_to_date": False, "changes": res.changed,
                     "rects": [], "png": base64.b64encode(res.png).decode("ascii")})
        self._json(200, base)

    # ------------------------------------------------------------ POST

    def do_POST(self):
        path = self.path.split("?")[0]
        params = self._parse_qs(self.path.split("?")[1] if "?" in self.path else "")

        # ------------------------------------------------ 切换当前面板
        if path == "/panel":
            if not self._authed(params):
                self._send(403, b"bad token")
                return
            if self.panel is None:
                self._send(503, b"panel state not initialized")
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                body = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception:
                body = {}
            want = body.get("panel") or params.get("panel") or ""
            if want == "toggle":
                want = self.panel.toggle()
            try:
                self.panel.set(want)
            except ValueError as e:
                self._send(400, str(e).encode("utf-8"))
                return
            snap = self.panel.snapshot()
            snap["ok"] = True
            print("[panel] -> %s (seq=%d)" % (snap["panel"], snap["seq"]))
            self._json(200, snap)
            return

        if path not in ("/cmd", "/music/cmd"):
            self._send(404, b"not found")
            return
        if not self._authed(params):
            self._send(403, b"bad token")
            return

        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            body = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            body = {"action": params.get("action", "")}

        action = body.get("action") or params.get("action") or ""

        # 面板动作要放在"音乐面板是否启用"的检查之前：这样 --no-music 启动时
        # 仍然能用 /cmd 把面板切回看板，不然设备会卡在一个取不到图的面板上。
        if action in ("panel_dash", "panel_music", "panel_toggle"):
            if self.panel is None:
                self._json(400, {"ok": False, "error": "panel state not initialized"})
                return
            try:
                if action == "panel_toggle":
                    self.panel.set(self.panel.toggle())
                else:
                    self.panel.set(action.split("_", 1)[1])
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            snap = self.panel.snapshot()
            snap["ok"] = True
            print("[panel] (via /cmd) -> %s (seq=%d)" % (snap["panel"], snap["seq"]))
            self._json(200, snap)
            return

        if self.music is None:
            self._send(404, b"music panel disabled")
            return

        kw = {k: v for k, v in body.items() if k != "action"}
        result = self.music.command(action, **kw)
        self._json(200 if result.get("ok") else 400, result)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--landscape", action="store_true")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    ap.add_argument("--once", action="store_true", help="只渲染一次并退出")
    ap.add_argument("--no-music", action="store_true", help="不启用音乐面板")
    ap.add_argument("--music-lines", type=int, default=7, help="歌词行数 5/7/9")
    ap.add_argument("--no-trans", action="store_true", help="默认关掉翻译歌词")
    ap.add_argument("--read-only", action="store_true", help="音乐面板只看不控（不下发按键）")
    ap.add_argument("--panel", choices=["dash", "music"], default="dash",
                    help="启动时显示哪个面板（之后可用 POST /panel 或 GUI 随时切）")
    args = ap.parse_args()

    w, h = (1200, 825) if args.landscape else (825, 1200)

    if args.once:
        cfg = render.load_config()
        if data_sources is not None:
            try:
                data_sources.refresh_all(city=str(cfg.get("city") or ""))
            except Exception as e:
                print("[警告] 刷新数据源失败（用旧缓存继续）：", e)
        img = render.draw_dashboard(cfg, w, h, now=render.effective_now())
        if args.rotate:
            img = img.rotate(-args.rotate, expand=True)
        out = render.OUT / "dash.png"
        render.OUT.mkdir(parents=True, exist_ok=True)
        img.save(out, "PNG", optimize=True)
        print("[OK]", out, img.size)
        return

    token = get_token()
    Handler.cache = Cache()
    Handler.token = token
    Handler.width = w
    Handler.height = h
    Handler.rotate = args.rotate

    # ---- 音乐面板（可选）
    music = None
    music_note = "未启用（--no-music）"
    if not args.no_music:
        try:
            from music.service import MusicService
            music = MusicService(width=w, height=h, lyric_lines=args.music_lines,
                                 translation=not args.no_trans,
                                 allow_control=not args.read_only)
            # 先渲染一帧，早失败早报错（比如没装 Pillow）
            music.frame(force=True)
            music_note = "已启用（歌词 %d 行，翻译 %s，控制 %s）" % (
                args.music_lines, "关" if args.no_trans else "开",
                "禁用" if args.read_only else "启用")
        except Exception as e:
            music = None
            music_note = "启用失败：%r" % (e,)
    Handler.music = music

    # ---- 当前面板：决定权在电脑端
    panel = PanelState(args.panel)
    panel.set_enabled(dash=True, music=(music is not None))
    Handler.panel = panel

    # ---- 看板的联网数据：天气 / 新闻 / 热搜
    # daemon=True：主进程退出时它跟着死，不留孤儿线程
    refresher = Refresher(city_getter=lambda: str(render.load_config().get("city") or ""))
    refresher.start()

    ip = lan_ip()

    # ------------------------------------------------------------------
    # 绑定前先确认端口是空的。这一步**不能省**。
    #
    # 为什么：Windows 上 TCP 监听默认带 SO_REUSEADDR，于是**第二个进程也能绑上
    # 已经被监听的端口**，而且不报错。后果极其难查：
    #   · 内核会把新连接**随机分给其中一个**监听者；
    #   · 于是约一半请求正常、一半"连上了但一个字节都不回"（RemoteDisconnected）；
    #   · 用户看到的是"服务时好时坏"，而两个进程各自都是好的。
    # 更糟的是 GUI 起的 server.py 如果成了孤儿（GUI 退出/崩溃时没被回收），
    # 它会一直占着端口，之后每次新起一个就再多一份"随机抢答"。
    #
    # 所以这里用**不带 SO_REUSEADDR** 的裸 socket 先试一次 bind：
    # 能绑上说明真空闲（然后立刻放掉），绑不上就说明有人占着 —— 直接报错退出，
    # 让用户看见"端口被占"，而不是默默起一个会随机失败的实例。
    # ------------------------------------------------------------------
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((args.host, args.port))
    except OSError as e:
        print("=" * 68)
        print("  [启动失败] 端口 %d 已被占用，本次不启动。" % args.port)
        print()
        print("  原因：%s" % e)
        print()
        print("  常见来源：上一个 server.py / GUI 起的服务没退干净（成了孤儿进程）。")
        print("  处理办法（任选其一）：")
        print("    1) 在 GUI 里点「停止」，等状态变成「未运行」，再点「启动」；")
        print("    2) 或者用管理员 PowerShell 执行：")
        print("         netstat -ano | findstr :%d     # 看 LISTENING 后面那个 PID" % args.port)
        print("         taskkill /PID <PID> /F")
        print("    3) 或者换个端口启动：  python server.py --port 8766")
        print("       （GUI 的「端口」输入框里改一下也一样）")
        print("=" * 68)
        raise SystemExit(2)
    finally:
        probe.close()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    avail = "/".join(panel.available())
    print("=" * 68)
    print("  H9 看板服务已启动")
    print()
    print("  【当前面板】%s   （可用：%s）" % (panel.get(), avail))
    print("      切换：GUI「服务」页的三个按钮（信息看板 / 歌词面板 / 切换）")
    print("      或  POST /panel  {\"panel\":\"dash\"|\"music\"|\"toggle\"}")
    print("      设备每 4 秒问一次 GET /panel，所以点完最多 4 秒它就翻过去了")
    print()
    print("  【信息看板】")
    print("      整图 http://%s:%d/dash.png?t=%s" % (ip, args.port, token))
    print("      带变化检测 http://%s:%d/dash.png?rev=0&t=%s   （设备用这个）" % (ip, args.port, token))
    print("      刷新节奏：每分钟一次（分钟对齐 + %d 秒提前量，见 render.RENDER_LEAD_S）"
          % render.RENDER_LEAD_S)
    if data_sources is not None:
        print("      联网栏目：天气（30 分钟）/ 全球新闻（1 小时）/ 中文热搜（1 小时）")
    else:
        print("      联网栏目：data_sources 不可用，只显示时间和（暂无）")
    print()
    print("  【音乐面板】")
    print("      状态 %s" % music_note)
    print("      整帧 http://%s:%d/music.png?t=%s" % (ip, args.port, token))
    print("      脏块 http://%s:%d/music/crops?from=-1&t=%s   （设备用这个）" % (ip, args.port, token))
    print("      本机先看看： http://127.0.0.1:%d/music.png?t=%s" % (args.port, token))
    print()
    print("  token 文件：%s" % TOKEN_FILE)
    print("  按 Ctrl+C 停止")
    print("=" * 68)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
