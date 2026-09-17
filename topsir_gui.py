#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
topsir_gui.py —— H9 墨水屏看板的 Windows 图形界面

四个功能：
  1. 启动 / 停止 PC 端看板服务（server.py），带实时日志、一键放行防火墙、设置开机自启
  2. 可视化编辑 dashboard/config.json（城市、天气、日程、待办）
  3. 网易云音乐墨水屏歌词面板：实时预览 + 播放控制 + 歌词校准
  4. 把 H9Dash.apk 和 dashboard.conf 部署到设备存储盘

只依赖 Python 标准库（tkinter）。渲染部分需要 Pillow + numpy，见 requirements.txt。

运行：
    python topsir_gui.py
    pythonw topsir_gui.py        # 不弹控制台
    或双击 start_gui.bat
"""
import atexit
import json
import io
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

import elevate

# ---------------- 打包成 exe 后的路径规则 ----------------
#
# 单文件 exe（PyInstaller --onefile）每次运行都先把自己解压到一个
# **随机名字**的临时目录再执行，__file__ 指向的是那里 —— 拿它当数据
# 目录的话，config.json 和缓存每次启动都会丢。
# 所以打包后（sys.frozen）可写数据一律搬去 %APPDATA%\H9Dash，
# 子目录结构和仓库里一模一样（dashboard/、music/、logs/），
# 服务子进程（exe --server）也重定向到同一个地方，两边看到同一份数据。
# 源码运行不受影响，还在原地。
FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    BASE = Path(os.environ.get("APPDATA") or str(Path.home())) / "H9Dash"
    try:
        BASE.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
else:
    BASE = Path(__file__).resolve().parent
DASH = BASE / "dashboard"
CONFIG = DASH / "config.json"
TOKEN_FILE = Path.home() / ".h9dash" / "token"
OUT_PNG = DASH / "out" / "dash.png"


def _find_apk() -> Path:
    """H9Dash.apk 在哪。

    优先级：
      1. exe 旁边 —— 发布包就是「一个 exe + 一个 apk」放一起，
         设备部署用它，而且 apk 可以单独更新、不用重新打包 exe；
      2. 打包时内嵌进 exe 的兜底副本 —— 只有 40 KB，忘了带 apk 也能部署；
      3. 源码运行：仓库根目录。
    """
    if FROZEN:
        beside = Path(sys.executable).resolve().parent / "H9Dash.apk"
        if beside.exists():
            return beside
        inner = Path(getattr(sys, "_MEIPASS", "") or ".") / "H9Dash.apk"
        if inner.exists():
            return inner
        return beside
    return BASE / "H9Dash.apk"


APK = _find_apk()

PORT_DEFAULT = 8765


# ------------------------------------------------------------------ 工具
def port_is_free(port: int, host: str = "0.0.0.0") -> bool:
    """端口是不是真空闲。

    ⚠️ 必须用**不带 SO_REUSEADDR** 的裸 socket 试绑，不能只靠 connect 探活。
    原因见 dashboard/server.py 里的长注释：Windows 上带 SO_REUSEADDR 时，
    第二个进程也能"成功"绑上已被监听的端口，于是两个实例抢答、时好时坏。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def find_free_port(start: int = PORT_DEFAULT, tries: int = 20) -> int:
    """从 start 往后找一个空端口（给"自动换端口"用）。"""
    for p in range(int(start), int(start) + tries):
        if port_is_free(p):
            return p
    return int(start)


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_token() -> str:
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    import secrets
    t = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    return t


def regen_token() -> str:
    if TOKEN_FILE.exists():
        try:
            TOKEN_FILE.unlink()
        except Exception:
            pass
    return get_token()


def startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / \
        r"Microsoft\Windows\Start Menu\Programs\Startup"


def drive_type(c: str) -> int:
    """返回 Windows GetDriveType 值：2=可移动 3=固定 4=网络 5=光驱 6=内存盘"""
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW("%s:\\" % c)
    except Exception:
        return 0


def removable_drives():
    """盘符列表，可移动盘排在最前（H9 是 USB 大容量存储，认成可移动盘）。"""
    fixed, removable = [], []
    for c in "DEFGHIJKLMNOPQRSTUVWXYZ":
        p = Path(c + ":/")
        try:
            if not p.exists():
                continue
        except Exception:
            continue
        (removable if drive_type(c) == 2 else fixed).append(c + ":")
    return removable + fixed


# ------------------------------------------------------------------ 主界面
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("H9 看板控制台")
        self.root.geometry("900x680")
        self.root.minsize(820, 600)

        self.proc = None
        self.q = queue.Queue()
        self.cfg = {}

        # GUI 万一崩溃/被强杀，也要把子进程带走 —— 否则会留下一个占着 8765 的
        # 孤儿 server.py，下次启动就会两个实例抢答（表现为"时好时坏的连不上"）。
        # Windows 上用 Job Object 是最彻底的：进程一死，Job 里的都跟着死。
        self._job = None
        atexit.register(self._kill_proc_quietly)
        self._setup_kill_on_close_job()

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_server = ttk.Frame(nb)
        self.tab_content = ttk.Frame(nb)
        self.tab_music = ttk.Frame(nb)
        self.tab_device = ttk.Frame(nb)
        nb.add(self.tab_server, text="  服务  ")
        nb.add(self.tab_content, text="  看板内容  ")
        nb.add(self.tab_music, text="  音乐  ")
        nb.add(self.tab_device, text="  设备  ")

        # 音乐面板的运行时状态（服务实例懒加载，避免没装依赖时整个 GUI 起不来）
        self.music_svc = None
        self.music_photo = None
        self.music_live = False
        self.music_stop_ev = threading.Event()
        self.music_q = queue.Queue()

        self.build_server_tab()
        self.build_content_tab()
        self.build_music_tab()
        self.build_device_tab()

        self.load_config()
        self.refresh_url()
        self.poll_panel_state()
        self.root.after(200, self.pump_log)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------------------------------------------------- Tab 1 服务
    def build_server_tab(self):
        f = self.tab_server

        top = ttk.LabelFrame(f, text="服务状态")
        top.pack(fill="x", padx=8, pady=8)

        row = ttk.Frame(top)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="本机 IP：").pack(side="left")
        self.ip_var = tk.StringVar(value=lan_ip())
        ttk.Entry(row, textvariable=self.ip_var, width=18).pack(side="left")
        ttk.Button(row, text="刷新", command=lambda: self.ip_var.set(lan_ip())).pack(side="left", padx=4)
        ttk.Label(row, text="端口：").pack(side="left", padx=(16, 0))
        self.port_var = tk.StringVar(value=str(PORT_DEFAULT))
        ttk.Entry(row, textvariable=self.port_var, width=8).pack(side="left")

        self.state_var = tk.StringVar(value="● 未运行")
        self.state_lbl = ttk.Label(top, textvariable=self.state_var, foreground="#b00020")
        self.state_lbl.pack(anchor="w", padx=8, pady=(0, 6))

        row2 = ttk.Frame(top)
        row2.pack(fill="x", padx=8, pady=6)
        ttk.Label(row2, text="面板状态地址：").pack(side="left")
        self.url_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.url_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row2, text="复制", command=self.copy_url).pack(side="left")

        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=8, pady=8)
        self.btn_start = ttk.Button(btns, text="启动服务", command=self.start_server)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="停止服务", command=self.stop_server, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(btns, text="重新生成 Token", command=self.do_regen_token).pack(side="left", padx=6)
        ttk.Button(btns, text="渲染预览图", command=self.render_preview).pack(side="left", padx=6)
        ttk.Button(btns, text="打开预览", command=self.open_preview).pack(side="left", padx=6)

        # ---- 当前显示哪个面板（这个才是用户日常要按的东西）----
        # 面板归属存在服务端，设备每轮取图前问一次，所以这里一点，
        # 平板几秒内就翻过去了 —— 不用碰设备、不用重装、不用改配置。
        pan = ttk.LabelFrame(f, text="平板现在显示什么")
        pan.pack(fill="x", padx=8, pady=4)
        prow = ttk.Frame(pan)
        prow.pack(fill="x", padx=8, pady=8)
        ttk.Button(prow, text="◀ 信息看板", width=14,
                   command=lambda: self.set_panel("dash")).pack(side="left")
        ttk.Button(prow, text="歌词面板 ▶", width=14,
                   command=lambda: self.set_panel("music")).pack(side="left", padx=6)
        ttk.Button(prow, text="⇄ 切换", width=10,
                   command=lambda: self.set_panel("toggle")).pack(side="left", padx=6)
        self.panel_var = tk.StringVar(value="当前：—")
        self.panel_lbl = ttk.Label(prow, textvariable=self.panel_var, foreground="#0a6")
        self.panel_lbl.pack(side="left", padx=(14, 0))
        ttk.Button(prow, text="自检", width=6,
                   command=self.panel_selftest).pack(side="left", padx=(10, 0))
        ttk.Label(prow, text="（点一下，平板几秒内自动切过去）",
                  foreground="#888").pack(side="left", padx=(8, 0))

        adv = ttk.LabelFrame(f, text="系统")
        adv.pack(fill="x", padx=8, pady=4)
        ab = ttk.Frame(adv)
        ab.pack(fill="x", padx=8, pady=8)
        ttk.Button(ab, text="放行防火墙", command=self.open_firewall).pack(side="left")
        ttk.Button(ab, text="设置开机自启", command=self.set_autostart).pack(side="left", padx=6)
        ttk.Button(ab, text="取消开机自启", command=self.clear_autostart).pack(side="left")

        logf = ttk.LabelFrame(f, text="服务日志")
        logf.pack(fill="both", expand=True, padx=8, pady=8)
        self.log = scrolledtext.ScrolledText(logf, height=14, font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

    def refresh_url(self):
        """这里展示 /panel —— 设备真正依赖的就是它（免 token，问"该看哪个面板"）。

        看板地址仍然可用，但设备不再是"配死一个 /dash.png"了，
        所以把 /panel 摆在前面更符合现在的实际数据流。
        """
        self.url_var.set("http://%s:%s/panel"
                         % (self.ip_var.get().strip(), self.port_var.get().strip()))

    # ------------------------------------------------- 面板切换（服务端状态）

    def _panel_base(self):
        return "http://127.0.0.1:%s" % self.port_var.get().strip()

    def set_panel(self, which):
        """把面板切到 dash / music / toggle。

        走的是 POST /panel，改的是**服务端**那份状态 —— 设备下一轮轮询
        就会发现自己该换面板了。所以这里不需要去碰设备。

        注意：这是本机回环调用，不经过 lan_ip()，所以不受代理/防火墙影响。
        """
        url = self._panel_base() + "/panel?t=" + get_token()

        def work():
            body = json.dumps({"panel": which}).encode("utf-8")
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=4) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # 403 = token 对不上（最常见：GUI 手里那个 token 和服务端不是同一个）
                # 这跟"服务没跑"是两回事，别混成一句话，否则排查方向全错。
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:80]
                except Exception:
                    pass
                if e.code == 403:
                    return {"_err": "token 不匹配", "_hint":
                            "服务在跑，但 token 对不上。点「重新生成 Token」后"
                            "重启服务，再部署一次设备配置。", "_code": 403}
                return {"_err": "HTTP %d" % e.code, "_hint": detail, "_code": e.code}
            except Exception as e:
                return {"_err": str(e), "_hint": "连不上 127.0.0.1，服务可能没启动",
                        "_code": 0}

        def done(res):
            if "_err" in res:
                self.panel_var.set("当前：切换失败 —— %s" % res["_err"])
                self.panel_lbl.config(foreground="#b00020")
                self.log_line("[面板] 切换失败：%s（%s）"
                              % (res["_err"], res.get("_hint", "")))
                return
            name = res.get("panel", "?")
            label = {"dash": "信息看板", "music": "网易云歌词面板"}.get(name, name)
            self.panel_var.set("当前：%s" % label)
            self.panel_lbl.config(foreground="#0a6")
            self.mode_var.set(label)
            self._sync_mode_hint()
            self.log_line("[面板] 已切到 %s（seq=%s）" % (label, res.get("seq")))

        def run():
            res = work()
            self.root.after(0, lambda: done(res))

        threading.Thread(target=run, daemon=True).start()

    def poll_panel_state(self):
        """低频回读服务端的面板状态，让按钮旁边的字跟得上实际值。

        只读不写（/panel 免 token），服务没起就安静地显示"未运行"，不弹错误。
        """
        url = self._panel_base() + "/panel"

        def work():
            try:
                with urllib.request.urlopen(url, timeout=1.5) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception:
                return None

        def done(res):
            if res is None:
                self.panel_var.set("当前：服务未运行")
                self.panel_lbl.config(foreground="#888")
            else:
                name = res.get("panel", "?")
                label = {"dash": "信息看板", "music": "网易云歌词面板"}.get(name, name)
                self.panel_var.set("当前：%s" % label)
                self.panel_lbl.config(foreground="#0a6")
                if self.mode_var.get() != label:
                    self.mode_var.set(label)
                    self._sync_mode_hint()
            self.root.after(3000, self.poll_panel_state)

        def run():
            res = work()
            self.root.after(0, lambda: done(res))

        threading.Thread(target=run, daemon=True).start()

    def panel_selftest(self):
        """一键排掉"切不了"里最常见的两种原因：服务没起 / token 对不上。

        别让人对着"改不了"猜 —— 直接告诉他到底是哪一种。
        """
        base = self._panel_base()
        lines = []

        # 1) 服务在不在
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                h = json.loads(r.read().decode("utf-8"))
            lines.append("服务：在跑（%s，当前面板 %s）"
                         % (h.get("service"), h.get("panel")))
            srv_token_ok = True
        except Exception as e:
            lines.append("服务：连不上 %s —— %r" % (base, e))
            messagebox.showerror("自检", "\n".join(lines))
            return

        # 2) token 对不对（用 /panel 写一个"当前值"是无副作用的：同值不改 seq）
        cur = h.get("panel", "dash")
        try:
            body = json.dumps({"panel": cur}).encode("utf-8")
            req = urllib.request.Request(
                base + "/panel?t=" + get_token(), data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=3):
                pass
            lines.append("Token：一致，可以切换")
            lines.append("设备侧 token 文件里应填：%s" % get_token())
        except urllib.error.HTTPError as e:
            if e.code == 403:
                lines.append("Token：**对不上**（服务在跑，但两边 token 不同）")
                lines.append("处理：点「重新生成 Token」→ 重启服务 → 重新部署设备配置")
            else:
                lines.append("Token：校验失败 HTTP %d" % e.code)
        except Exception as e:
            lines.append("Token：校验出错 %r" % (e,))

        messagebox.showinfo("自检", "\n".join(lines))

    def copy_url(self):
        self.refresh_url()
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url_var.get())
        messagebox.showinfo("已复制", "地址已复制到剪贴板")

    def do_regen_token(self):
        if self.proc:
            messagebox.showwarning("请先停止服务", "停止服务后再重新生成 Token")
            return
        regen_token()
        self.refresh_url()
        messagebox.showinfo("完成", "Token 已重新生成，记得重新部署到设备")

    def start_server(self):
        if self.proc:
            return
        port = self.port_var.get().strip() or str(PORT_DEFAULT)
        try:
            port_n = int(port)
        except ValueError:
            messagebox.showerror("端口错误", "端口必须是数字")
            return

        # 启动前先确认端口是空的。不查的话会遇到最难查的一类故障：
        # 上一个服务成了孤儿仍占着端口，新实例在 Windows 上**照样能绑上**
        # （SO_REUSEADDR），于是两个进程抢答同一个端口 ——
        # 表现就是"有时连得上、有时 RemoteDisconnected"，而两边单独看都是好的。
        if not port_is_free(port_n):
            alt = find_free_port(port_n + 1)
            ans = messagebox.askyesno(
                "端口被占用",
                "端口 %d 已经被占用，服务多半没能退干净（留下了一个孤儿进程）。\n\n"
                "是否自动改用空闲端口 %d 启动？\n\n"
                "选「否」的话，就先在「停止」一下、或用管理员权限结束那个进程再试。"
                % (port_n, alt))
            if not ans:
                self.q.put("[GUI] 端口 %d 被占用，已取消启动。" % port_n)
                return
            port_n = alt
            self.port_var.set(str(port_n))

        # 打包后没有 server.py 可以拿去跑 —— exe 自己就会扮演服务进程
        # （见文件尾 main() 里的 --server 分发）。GUI 照旧用子进程 +
        # 管道收日志，进程管理结构（Job Object / 孤儿回收）一概不变。
        if FROZEN:
            cmd = [sys.executable, "--server", "--port", str(port_n)]
        else:
            cmd = [sys.executable, "-u", str(DASH / "server.py"), "--port", str(port_n)]
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(DASH),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except Exception as e:
            messagebox.showerror("启动失败", str(e))
            self.proc = None
            return
        threading.Thread(target=self.reader, daemon=True).start()
        self._adopt_into_job(self.proc)      # 让子进程跟着 GUI 一起结束
        self.state_var.set("● 运行中")
        self.state_lbl.configure(foreground="#0a7a3d")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.ip_var.set(lan_ip())
        self.refresh_url()
        self.q.put("[GUI] 服务已启动，端口 %d，面板状态地址：\n       %s\n"
                   % (port_n, self.url_var.get()))
        threading.Thread(target=self.watch_proc, daemon=True).start()

    def reader(self):
        p = self.proc
        try:
            for line in p.stdout:
                self.q.put(line.rstrip("\n"))
        except Exception:
            pass

    def watch_proc(self):
        """盯着子进程：它要是启动就挂了（比如 print 撞编码），把界面状态改回来。

        不加这个的话按钮会一直显示"运行中"，而实际上什么都没跑 ——
        于是用户去点切换，得到的就是那句误导人的"服务没跑？"。
        """
        p = self.proc
        if p is None:
            return
        code = p.wait()
        # 是用户主动停的就别重复处理
        if self.proc is not p:
            return
        self.proc = None
        self.q.put("[GUI] 服务进程已退出（退出码 %s）—— 上面若有 Traceback，"
                   "那就是原因" % code)
        self.root.after(0, lambda: self._on_proc_died(code))

    def _on_proc_died(self, code):
        self.state_var.set("● 未运行（进程退出，码 %s）" % code)
        self.state_lbl.configure(foreground="#b00020")
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")

    def pump_log(self):
        try:
            while True:
                line = self.q.get_nowait()
                self.log_line(line)
        except queue.Empty:
            pass
        self.root.after(300, self.pump_log)

    def log_line(self, text):
        ts = time.strftime("%H:%M:%S")
        self.log.insert("end", "[%s] %s\n" % (ts, text))
        self.log.see("end")

    def _setup_kill_on_close_job(self):
        """建一个 "关掉 job 就杀掉里面所有进程" 的 Windows Job Object。

        有它之后，GUI 正常退出、崩掉、被任务管理器结束，子进程都会被系统一并回收。
        拿不到（非 Windows / 权限不足）就算了，atexit 那层还能兜一下。
        """
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

            job = k32.CreateJobObjectW(None, None)
            if not job:
                return

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            JobObjectExtendedLimitInformation = 9
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                    job, JobObjectExtendedLimitInformation,
                    ctypes.byref(info), ctypes.sizeof(info)):
                return
            self._job = job
        except Exception:
            self._job = None

    def _adopt_into_job(self, p):
        """把子进程放进 job，让它跟着 GUI 一起结束。"""
        if not self._job or p is None:
            return
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.AssignProcessToJobObject(self._job, int(p._handle))
        except Exception:
            pass

    def _kill_proc_quietly(self):
        """退出时的最后一道保险（不弹任何对话框）。"""
        p = self.proc
        if p is None:
            return
        try:
            p.terminate()
            try:
                p.wait(timeout=3)
            except Exception:
                p.kill()
        except Exception:
            pass

    def stop_server(self):
        if not self.proc:
            return
        p = self.proc
        self.proc = None          # 先摘掉，让 watch_proc 认出"这是主动停的"
        try:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        except Exception:
            pass
        self.state_var.set("● 未运行")
        self.state_lbl.configure(foreground="#b00020")
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.q.put("[GUI] 服务已停止")

    def render_preview(self):
        try:
            if FROZEN:
                cmd = [sys.executable, "--render"]
            else:
                cmd = [sys.executable, str(DASH / "render.py")]
            r = subprocess.run(
                cmd,
                cwd=str(DASH), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120)
            if r.returncode == 0:
                self.q.put("[GUI] " + (r.stdout or "").strip())
                messagebox.showinfo("完成", "预览图已生成：\n%s" % OUT_PNG)
            else:
                messagebox.showerror("渲染失败", (r.stderr or r.stdout or "")[:1500])
        except Exception as e:
            messagebox.showerror("渲染失败", str(e))

    def open_preview(self):
        if not OUT_PNG.exists():
            self.render_preview()
        if OUT_PNG.exists():
            try:
                os.startfile(str(OUT_PNG))
            except Exception as e:
                messagebox.showerror("打不开", str(e))

    def open_firewall(self):
        """放行防火墙 —— exe / 脚本都一样：普通权限运行，按需提权。

        流程见 elevate.py 的模块注释。这里负责把"会弹一次 UAC"提前
        讲清楚 —— 不打招呼就弹系统确认框，是最容易被当成病毒的行为。
        """
        port = self.port_var.get().strip() or str(PORT_DEFAULT)
        try:
            port_n = int(port)
        except ValueError:
            messagebox.showerror("端口错误", "端口必须是数字")
            return

        ans = messagebox.askyesno(
            "放行防火墙",
            "将放行 TCP %d 的入站连接（局域网设备靠它取图）。\n\n"
            "Windows 会弹出「用户账户控制」确认框 —— 点「是」即可。\n"
            "这是系统在确认管理员权限，**只需要这一次**，规则永久生效。"
            % port_n)
        if not ans:
            return

        try:
            status, detail = elevate.ensure_rule(port_n)
        except Exception as e:
            messagebox.showerror("失败", repr(e))
            return

        self.log_line("[防火墙] %s：%s" % (status, detail))
        if status in ("ok", "already"):
            messagebox.showinfo(
                "完成", "防火墙已放行 TCP %d。\n\n设备现在可以通过局域网取图了。" % port_n)
        elif status == "cancelled":
            messagebox.showwarning(
                "已取消",
                "管理员确认被拒绝，规则没有添加。\n\n"
                "设备可能连不上（墨水屏上表现为「获取失败」）。\n"
                "随时可以再点「放行防火墙」重试。")
        else:
            messagebox.showwarning(
                "自动放行失败（%s）" % status,
                "%s\n\n可以手动来：\n"
                "1. 右键「Windows PowerShell」→ 以管理员身份运行\n"
                "2. 粘贴这一行：\n\n"
                "netsh advfirewall firewall add rule name=\"H9Dash %d\" "
                "dir=in action=allow protocol=TCP localport=%d profile=any"
                % (detail or "", port_n, port_n))

    def set_autostart(self):
        port = self.port_var.get().strip() or str(PORT_DEFAULT)
        if FROZEN:
            # exe 场景写注册表 Run 键，不写启动文件夹的 .bat：
            # bat 按本机代码页解析，exe 名字 / 用户目录带中文时整行会坏
            # （README「改代码时注意」里记过这个坑）；注册表值是
            # Unicode，怎么都不会坏，登录时也不会闪一下黑框。
            import winreg
            run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, "H9Dash", 0, winreg.REG_SZ,
                                      '"%s" --server --port %s'
                                      % (sys.executable, port))
                messagebox.showinfo(
                    "完成", "已设置开机自启：登录后自动在后台启动服务\n"
                    "（不弹窗口；想看状态就打开这个控制台）")
            except OSError as e:
                messagebox.showerror("失败", str(e))
            return
        d = startup_dir()
        if not d.exists():
            messagebox.showerror("失败", "找不到启动目录：%s" % d)
            return
        bat = d / "H9DashServer.bat"
        bat.write_text(
            '@echo off\r\ncd /d "%s"\r\n"%s" "%s" --port %s\r\n'
            % (DASH, sys.executable, DASH / "server.py",
               self.port_var.get().strip() or str(PORT_DEFAULT)),
            encoding="utf-8")
        messagebox.showinfo("完成", "已创建开机自启：\n%s" % bat)

    def clear_autostart(self):
        if FROZEN:
            import winreg
            run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, "H9Dash")
                messagebox.showinfo("完成", "已取消开机自启")
            except FileNotFoundError:
                messagebox.showinfo("提示", "本来就没有设置开机自启")
            except OSError as e:
                messagebox.showerror("失败", str(e))
            return
        bat = startup_dir() / "H9DashServer.bat"
        if bat.exists():
            bat.unlink()
            messagebox.showinfo("完成", "已取消开机自启")
        else:
            messagebox.showinfo("提示", "本来就没有设置开机自启")

    # ------------------------------------------------------ Tab 2 看板内容
    def build_content_tab(self):
        f = self.tab_content

        box = ttk.LabelFrame(f, text="基本信息")
        box.pack(fill="x", padx=8, pady=8)
        r = ttk.Frame(box)
        r.pack(fill="x", padx=8, pady=8)
        ttk.Label(r, text="城市：").pack(side="left")
        self.city_var = tk.StringVar()
        ttk.Entry(r, textvariable=self.city_var, width=12).pack(side="left")
        ttk.Label(r, text="新闻条数：").pack(side="left", padx=(16, 0))
        self.news_count_var = tk.StringVar(value="5")
        ttk.Spinbox(r, from_=1, to=8, width=4,
                    textvariable=self.news_count_var).pack(side="left")
        ttk.Label(r, text="热搜条数：").pack(side="left", padx=(16, 0))
        self.hot_count_var = tk.StringVar(value="7")
        ttk.Spinbox(r, from_=1, to=10, width=4,
                    textvariable=self.hot_count_var).pack(side="left")

        # ---- 联网数据：天气 / 新闻 / 热搜都是自动抓的，这里只看不改 ----
        # 天气以前是手填的（temp/desc/high/low 四个输入框），现在由
        # dashboard/data_sources.py 从网上抓，GUI 里改成只读展示 + 手动刷新。
        ds = ttk.LabelFrame(f, text="联网数据（天气每 30 分钟、新闻与热搜每 1 小时自动更新）")
        ds.pack(fill="x", padx=8, pady=4)
        grid = ttk.Frame(ds)
        grid.pack(fill="x", padx=8, pady=6)
        grid.columnconfigure(1, weight=1)

        self.weather_var = tk.StringVar(value="（尚未获取）")
        self.news_upd_var = tk.StringVar(value="（尚未获取）")
        self.hot_upd_var = tk.StringVar(value="（尚未获取）")
        ttk.Label(grid, text="天气：").grid(row=0, column=0, sticky="w")
        ttk.Label(grid, textvariable=self.weather_var, foreground="#444").grid(
            row=0, column=1, sticky="w")
        ttk.Label(grid, text="全球新闻：").grid(row=1, column=0, sticky="w")
        ttk.Label(grid, textvariable=self.news_upd_var, foreground="#444").grid(
            row=1, column=1, sticky="w")
        ttk.Label(grid, text="中文热搜：").grid(row=2, column=0, sticky="w")
        ttk.Label(grid, textvariable=self.hot_upd_var, foreground="#444").grid(
            row=2, column=1, sticky="w")
        self.btn_dash_refresh = ttk.Button(grid, text="立即刷新",
                                           command=self.dash_refresh_now)
        self.btn_dash_refresh.grid(row=0, column=2, rowspan=3, padx=(12, 0), sticky="ns")

        ttk.Label(ds, foreground="#666", justify="left", wraplength=680,
                  text="数据源（均免密钥）：Open-Meteo 天气 · NPR 全球新闻（英文） · "
                       "头条热榜（百度兜底）。\n"
                       "服务运行时由后台线程自动刷新；这里点「立即刷新」可以马上取一次。\n"
                       "取不到（断网/接口挂了）时墨水屏上显示（暂无），不影响其它栏目。"
                  ).pack(anchor="w", padx=8, pady=(0, 6))

        sb = ttk.Frame(f)
        sb.pack(fill="x", padx=8, pady=8)
        ttk.Button(sb, text="保存并渲染预览", command=self.save_config).pack(side="left")
        ttk.Button(sb, text="重新加载", command=self.load_config).pack(side="left", padx=6)
        ttk.Button(sb, text="用记事本打开 config.json", command=self.open_config).pack(side="left", padx=6)

    # ------ 联网数据状态展示与手动刷新

    def _dash_ds(self):
        """拿 dashboard/data_sources 模块。拿不到（文件缺失等）返回 None。"""
        try:
            if str(DASH) not in sys.path:
                sys.path.insert(0, str(DASH))
            import data_sources
            return data_sources
        except Exception:
            return None

    def dash_update_status(self):
        """把三个源的当前状态刷到界面上。"""
        ds = self._dash_ds()
        if ds is None:
            for v in (self.weather_var, self.news_upd_var, self.hot_upd_var):
                v.set("（模块不可用）")
            return
        try:
            st = ds.status()
        except Exception as e:
            self.weather_var.set("（读取失败：%s）" % e)
            return

        def fmt(ts):
            return time.strftime("%H:%M", time.localtime(ts)) if ts else "从未"

        w = st.get("weather") or {}
        if w.get("count"):
            self.weather_var.set("%s（%s 更新，来源 %s）"
                                 % (self._weather_brief(), fmt(w.get("at")), w.get("src") or "?"))
        else:
            self.weather_var.set("（暂无，点「立即刷新」或等服务跑起来）")
        for kind, var, unit in (("news", self.news_upd_var, "条"),
                                ("hot", self.hot_upd_var, "条")):
            s = st.get(kind) or {}
            if s.get("count"):
                var.set("%d %s（%s 更新，来源 %s）"
                        % (s["count"], unit, fmt(s.get("at")), s.get("src") or "?"))
            else:
                var.set("（暂无）")

    def _weather_brief(self):
        ds = self._dash_ds()
        if ds is None:
            return ""
        try:
            w = ds.get_cached("weather")
        except Exception:
            return ""
        if not isinstance(w, dict):
            return ""
        parts = []
        if w.get("temp") is not None:
            parts.append("%s°" % w["temp"])
        if w.get("desc"):
            parts.append(str(w["desc"]))
        if w.get("high") is not None or w.get("low") is not None:
            parts.append("%s/%s" % (w.get("high", "?"), w.get("low", "?")))
        return " ".join(parts) if parts else "已获取"

    def dash_refresh_now(self):
        """手动刷一次三个源。网络最坏要 30+ 秒，必须丢到后台线程。"""
        ds = self._dash_ds()
        if ds is None:
            messagebox.showwarning("联网数据", "data_sources 模块不可用")
            return
        city = self.city_var.get().strip()
        self.btn_dash_refresh.config(state="disabled")
        self.weather_var.set("刷新中…")

        def work():
            try:
                ds.refresh_all(city=city, force=True)
            except Exception:
                pass
            self.q.put("[dash] 联网数据已刷新")

        threading.Thread(target=work, daemon=True).start()
        # 刷新完成由状态轮询带出来（App 不是 widget，after 要挂在 root 上）
        self.root.after(1500, self._dash_status_loop, 12)

    def _dash_status_loop(self, left):
        """「立即刷新」期间轮询状态，直到按钮恢复。"""
        self.dash_update_status()
        if left <= 0:
            self.btn_dash_refresh.config(state="normal")
            return
        self.root.after(1500, self._dash_status_loop, left - 1)

    def load_config(self):
        if CONFIG.exists():
            try:
                self.cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
            except Exception as e:
                messagebox.showerror("配置读取失败", str(e))
                self.cfg = {}
        else:
            self.cfg = {}
        if not isinstance(self.cfg, dict):
            self.cfg = {}
        self.city_var.set(self.cfg.get("city", ""))
        self.news_count_var.set(str(self.cfg.get("news_count", 5)))
        self.hot_count_var.set(str(self.cfg.get("hot_count", 7)))
        self.dash_update_status()

    def collect_config(self):
        def _int(var, lo, hi, dft):
            try:
                return max(lo, min(hi, int(var.get())))
            except (TypeError, ValueError):
                return dft
        return {
            "city": self.city_var.get().strip(),
            "news_count": _int(self.news_count_var, 1, 8, 5),
            "hot_count": _int(self.hot_count_var, 1, 10, 7),
        }

    def save_config(self):
        cfg = self.collect_config()
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return
        self.cfg = cfg
        self.render_preview()

    def open_config(self):
        try:
            os.startfile(str(CONFIG))
        except Exception as e:
            messagebox.showerror("打不开", str(e))

    # ---------------------------------------------------------- Tab 3 音乐
    #
    # 这一页把 music 包（真·墨水屏歌词面板的渲染与状态机）直接跑在 GUI 进程里，
    # 好处是调版式、试按键、看歌词都能在一个窗口里完成，不用去碰设备。
    # 渲染 / 取歌词会阻塞（HTTP 最长 8 秒），所以一律丢到后台线程，
    # 结果经 queue 回到主线程刷 UI —— tkinter 不是线程安全的。

    def build_music_tab(self):
        f = self.tab_music

        # ---- 顶部：开关 + 状态 ----
        bar = ttk.LabelFrame(f, text="网易云音乐 · 墨水屏歌词面板")
        bar.pack(fill="x", padx=8, pady=8)

        r = ttk.Frame(bar)
        r.pack(fill="x", padx=8, pady=6)
        self.music_state_var = tk.StringVar(value="未启动预览")
        ttk.Label(r, textvariable=self.music_state_var).pack(side="left")

        self.music_allow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="允许下发控制", variable=self.music_allow_var,
                        command=self.music_sync_allow).pack(side="left", padx=(16, 0))

        ttk.Button(r, text="停止预览", command=self.music_stop_run).pack(side="right")
        self.btn_music_start = ttk.Button(r, text="开始实时预览", command=self.music_start)
        self.btn_music_start.pack(side="right", padx=6)

        # ---- 主体：左预览、右控制 ----
        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.LabelFrame(mid, text="面板预览（825×1200 等比缩放）")
        left.pack(side="left", fill="y", padx=(0, 8))
        self.music_canvas = tk.Canvas(left, width=300, height=436,
                                      highlightthickness=1, highlightbackground="#bbb",
                                      bg="#f4f4f4")
        self.music_canvas.pack(padx=6, pady=6)
        ttk.Button(left, text="导出当前帧 PNG", command=self.music_export).pack(pady=(0, 8))

        right = ttk.Frame(mid)
        right.pack(side="left", fill="both", expand=True)

        ctl = ttk.LabelFrame(right, text="控制（下发到网易云客户端）")
        ctl.pack(fill="x")
        grid = ttk.Frame(ctl)
        grid.pack(padx=8, pady=8)
        btns = [
            ("上一首", "prev"), ("播放 / 暂停", "play_pause"), ("下一首", "next"),
            ("音量 -", "vol_down"), ("音量 +", "vol_up"), ("喜欢", "like"),
            ("歌词对早 5s", "lyric_earlier"), ("歌词对晚 5s", "lyric_later"), ("重新对齐", "realign"),
            ("翻译 开/关", "toggle_trans"), ("歌词行数", "cycle_lines"), ("强制重画", "refresh"),
        ]
        for i, (label, act) in enumerate(btns):
            ttk.Button(grid, text=label, width=14,
                       command=lambda a=act: self.music_cmd(a)).grid(
                row=i // 3, column=i % 3, padx=4, pady=4, sticky="ew")

        inf = ttk.LabelFrame(right, text="当前状态")
        inf.pack(fill="both", expand=True, pady=(8, 0))
        self.music_info_txt = scrolledtext.ScrolledText(inf, height=14, font=("Consolas", 9))
        self.music_info_txt.pack(fill="both", expand=True, padx=6, pady=6)

        # ---- 歌词提前量 ----
        # 墨水屏"取图 → 屏上真的变了"有 ~0.6s 固有延迟，再叠加整秒量化，
        # 屏上的歌词天生比耳朵慢半拍。提前正好补回来 —— 而且歌词早出现不影响阅读
        # （人读一句要好几秒），晚出现才难受，所以这个方向上提前是净收益。
        lead = ttk.LabelFrame(right, text="歌词提前量")
        lead.pack(fill="x", pady=(8, 0))
        lr = ttk.Frame(lead)
        lr.pack(fill="x", padx=8, pady=8)

        ttk.Label(lr, text="在每句结束前").pack(side="left")
        self.lead_var = tk.StringVar(value="2.0")
        lead_cb = ttk.Combobox(lr, textvariable=self.lead_var, width=5,
                               values=("0", "0.5", "1.0", "1.5", "2.0", "2.5",
                                       "3.0", "4.0", "5.0"))
        lead_cb.pack(side="left", padx=4)
        ttk.Label(lr, text="秒滚到下一句").pack(side="left")
        ttk.Button(lr, text="应用", command=self.music_apply_lead).pack(side="left", padx=(10, 0))

        self.lead_hint = ttk.Label(lead, foreground="#666", justify="left",
                                   text="0 = 关闭（歌播到哪句就显示哪句）。\n"
                                        "提前只改「哪句算当前句」，不动进度条、不动歌。")
        self.lead_hint.pack(anchor="w", padx=8, pady=(0, 8))

        hint = ttk.Label(f, foreground="#666",
                         text="提示：需要网易云音乐 PC 端正在运行；控制走系统媒体键，"
                              "不会写客户端的任何数据。")
        hint.pack(anchor="w", padx=12, pady=(0, 6))

    # -------------------------------------------------- 音乐：服务与后台线程

    def _music_service(self):
        """懒加载 MusicService（内部有锁，可跨线程调用）。"""
        if self.music_svc is None:
            if str(BASE) not in sys.path:
                sys.path.insert(0, str(BASE))
            from music import service as _m
            # 界面上填的提前量作为初始值带进去，免得"界面上写着 2.0，
            # 实际生效的是默认值"这种看不到的分叉。
            try:
                lead_ms = int(round(float(self.lead_var.get()) * 1000))
            except (TypeError, ValueError, AttributeError):
                lead_ms = _m.LYRIC_LEAD_DEFAULT_MS
            self.music_svc = _m.MusicService(
                825, 1200, lyric_lines=7, translation=True,
                allow_control=bool(self.music_allow_var.get()),
                lyric_lead_ms=lead_ms)
        return self.music_svc

    def music_sync_allow(self):
        if self.music_svc is not None:
            self.music_svc.allow_control = bool(self.music_allow_var.get())
        self.music_note("控制已%s" % ("开启" if self.music_allow_var.get() else "关闭"))

    def music_apply_lead(self):
        """把界面上的"提前 N 秒"发给渲染器。走 service 命令，和设备端按键同一条路。"""
        try:
            sec = float(self.lead_var.get())
        except (TypeError, ValueError):
            messagebox.showwarning("歌词提前量", "请填一个秒数，例如 2 或 1.5")
            return
        ms = int(round(max(0.0, min(10.0, sec)) * 1000))
        svc = self._music_service()
        r = svc.command("set_lyric_lead", ms=ms)
        if r.get("ok"):
            self.music_note("歌词提前 %s" % ("关闭" if ms == 0 else "%.1fs" % (ms / 1000.0)))
        else:
            self.music_note("设置失败：%s" % r.get("error"))

    def music_start(self):
        if self.music_live:
            return
        try:
            self._music_service()
        except Exception as e:
            messagebox.showerror("音乐面板不可用", "%s\n\n（需要 Pillow / numpy）" % e)
            return
        self.music_live = True
        self.music_stop_ev.clear()
        self.btn_music_start.config(state="disabled")
        threading.Thread(target=self._music_worker, daemon=True).start()
        self.root.after(150, self._music_pump)
        self.music_state_var.set("预览中…")
        self.music_note("预览已启动")

    def music_stop_run(self):
        if not self.music_live:
            return
        self.music_live = False
        self.music_stop_ev.set()
        self.btn_music_start.config(state="normal")
        self.music_state_var.set("已停止")
        self.music_note("预览已停止")

    def _music_worker(self):
        svc = self.music_svc
        while self.music_live and not self.music_stop_ev.is_set():
            wait_s = 1.0
            try:
                res = svc.frame()
                self.music_q.put(("frame", res, svc.status()))
                # 服务按"下次画面会变"的时间安排，别自己瞎轮询。
                # 下限必须是 0.05 而不是 0.6：歌词对齐靠的就是"跨过歌词的那次取图
                # 落在歌词时刻"，而那个值常常只有几十毫秒（比如 70ms）。
                # 卡到 0.6 秒 = 每次换行都晚半秒，正是"看着差好几句"的元凶之一。
                wait_s = max(0.05, min(3.0, res.next_change_ms / 1000.0))
            except Exception as e:
                self.music_q.put(("err", repr(e), None))
                wait_s = 2.0
            self.music_stop_ev.wait(wait_s)

    def _music_pump(self):
        try:
            while True:
                kind, a, b = self.music_q.get_nowait()
                if kind == "frame":
                    self._music_show(a, b)
                elif kind == "cmd":
                    self.music_note("指令 %s → %s" % (a.get("action"), a.get("did") or a.get("error") or "ok"))
                elif kind == "err":
                    self.music_note("!! %s" % a)
        except queue.Empty:
            pass
        if self.music_live:
            self.root.after(150, self._music_pump)

    # -------------------------------------------------- 音乐：UI 更新

    def _music_show(self, res, st):
        try:
            from PIL import Image, ImageTk
            img = Image.open(io.BytesIO(res.png))
            cw = int(self.music_canvas["width"])
            ch = int(self.music_canvas["height"])
            img.thumbnail((cw, ch))
            self.music_photo = ImageTk.PhotoImage(img)     # 必须留引用，否则被 GC
            self.music_canvas.delete("all")
            self.music_canvas.create_image(cw // 2, ch // 2, image=self.music_photo)
        except Exception as e:
            self.music_state_var.set("预览失败：%s" % e)

        t = st.get("track") or {}
        ly = st.get("lyric") or {}
        view = st.get("view") or {}
        pl = (st.get("playlist") or {}).get("name") or "—"
        dur = int(t.get("duration_ms") or 0)
        pos = int(t.get("position_ms") or 0)
        rid = t.get("id") or ""
        resolved = t.get("resolved_id") or ""

        def mmss(ms):
            ms = max(0, int(ms))
            return "%d:%02d" % (ms // 60000, (ms // 1000) % 60)

        lines = [
            "曲目      : %s" % (t.get("label") or "(未知曲目)"),
            "  专辑    : %s" % (t.get("album") or "—"),
            "  时长    : %s" % (mmss(dur) if dur else "未知"),
            "  进度    : %s  (%s)" % (mmss(pos), "播放中" if t.get("playing") else "暂停"),
            "  id      : %s" % (rid or "—"),
            "  补的 id : %s" % (resolved or "—（本地库反查不到，线上也没搜到）"),
            "",
            "歌词      : %d 行  翻译=%s" % (
                ly.get("lines", 0), "有" if ly.get("has_translation") else "无"),
            "  取词 id : %s" % (ly.get("song_id") or "—"),
            "  错误    : %s" % (ly.get("error") or "无"),
            "",
            "视图      : %d 行  翻译=%s" % (view.get("lyric_lines", 0),
                                            "开" if view.get("translation") else "关"),
            "歌单      : %s" % pl,
            "本帧      : 变 %d / 共 %d 块  %s" % (
                len(res.changed), len(res.blocks), ", ".join(res.changed[:6]) or "—"),
            "下次刷新  : %d ms%s" % (res.next_change_ms, "   [整屏]" if res.full else ""),
            "允许控制  : %s" % ("是" if st.get("allow_control") else "否"),
        ]
        errs = st.get("errors") or []
        if errs:
            lines.append("")
            lines.append("最近错误  :")
            for e in errs[-3:]:
                lines.append("  " + str(e))

        self.music_info_txt.delete("1.0", "end")
        self.music_info_txt.insert("1.0", "\n".join(lines) + "\n")
        self.music_state_var.set("%s · %s   [%s / %s]" % (
            t.get("label") or "—", "播放中" if t.get("playing") else "暂停", mmss(pos), mmss(dur)))

    def music_note(self, msg):
        """把一条消息追加到状态框（不覆盖上面的快照）。"""
        try:
            self.music_info_txt.insert("end", "\n[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
            self.music_info_txt.see("end")
        except Exception:
            pass

    def music_cmd(self, action):
        """下发指令；顺手强制出一帧，让预览立刻反映结果。"""
        try:
            svc = self._music_service()
        except Exception as e:
            messagebox.showerror("音乐面板不可用", str(e))
            return

        def run():
            try:
                r = svc.command(action)
                self.music_q.put(("cmd", r, None))
                self.music_q.put(("frame", svc.frame(force=True), svc.status()))
            except Exception as e:
                self.music_q.put(("err", repr(e), None))

        threading.Thread(target=run, daemon=True).start()
        if not self.music_live:
            self.root.after(150, self._music_pump)

    def music_export(self):
        try:
            svc = self._music_service()
            res = svc.frame(force=True)
        except Exception as e:
            messagebox.showerror("渲染失败", str(e))
            return
        p = filedialog.asksaveasfilename(
            title="保存当前帧", defaultextension=".png", initialfile="music_frame.png",
            filetypes=[("PNG 图片", "*.png")])
        if not p:
            return
        try:
            Path(p).write_bytes(res.png)
            messagebox.showinfo("已保存", p)
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    # ---------------------------------------------------------- Tab 4 设备
    def build_device_tab(self):
        f = self.tab_device

        box = ttk.LabelFrame(f, text="部署到设备")
        box.pack(fill="x", padx=8, pady=8)
        r = ttk.Frame(box); r.pack(fill="x", padx=8, pady=8)
        ttk.Label(r, text="设备盘符：").pack(side="left")
        self.drive_var = tk.StringVar()
        drives = removable_drives()
        self.drive_cb = ttk.Combobox(r, textvariable=self.drive_var, values=drives, width=6)
        self.drive_cb.pack(side="left")
        if drives:
            self.drive_var.set(drives[0])
        ttk.Button(r, text="刷新盘符", command=self.refresh_drives).pack(side="left", padx=6)
        ttk.Button(r, text="打开设备盘", command=self.open_drive).pack(side="left")
        ttk.Label(r, text="刷新间隔（分钟）：").pack(side="left", padx=(16, 0))
        self.interval_var = tk.StringVar(value="30")
        ttk.Entry(r, textvariable=self.interval_var, width=6).pack(side="left")
        ttk.Button(r, text="部署 APK + 配置", command=self.deploy).pack(side="left", padx=10)

        m = ttk.Frame(box)
        m.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(m, text="开机兜底面板：").pack(side="left")
        self.mode_var = tk.StringVar(value="信息看板")
        cb = ttk.Combobox(m, textvariable=self.mode_var, width=14, state="readonly",
                          values=("信息看板", "网易云歌词面板"))
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_mode_change())
        self.mode_hint = ttk.Label(m, foreground="#666")
        self.mode_hint.pack(side="left", padx=(10, 0))
        self._sync_mode_hint()

        info = ttk.LabelFrame(f, text="设备屏幕信息（首次打开 App 后才会生成）")
        info.pack(fill="x", padx=8, pady=4)
        self.screen_var = tk.StringVar(value="未读取")
        ttk.Label(info, textvariable=self.screen_var).pack(anchor="w", padx=8, pady=6)
        ttk.Button(info, text="读取", command=self.read_screen).pack(anchor="w", padx=8, pady=(0, 8))

        cf = ttk.LabelFrame(f, text="dashboard.conf 内容（部署后由设备读取）")
        cf.pack(fill="both", expand=True, padx=8, pady=8)
        self.conf_txt = scrolledtext.ScrolledText(cf, height=12, font=("Consolas", 9))
        self.conf_txt.pack(fill="both", expand=True, padx=6, pady=6)
        cb = ttk.Frame(cf); cb.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(cb, text="生成预览", command=self.preview_conf).pack(side="left")
        ttk.Button(cb, text="写入设备", command=self.write_conf).pack(side="left", padx=6)

        self.refresh_drives()

    # ------------------------------------------------- 设备页：模式与提示

    def mode_panel(self):
        """'网易云歌词面板' → music，其余 → dash。"""
        return "music" if self.mode_var.get().startswith("网易云") else "dash"

    def _on_mode_change(self):
        self._sync_mode_hint()
        try:
            self.preview_conf()
        except Exception:
            pass

    def _sync_mode_hint(self):
        """这里的值只是**设备端的兜底**，不是现场切面板的开关。

        真正决定"平板显示什么"的是服务端那份状态（用上面「平板现在显示什么」
        那排按钮切）。这个下拉只影响设备开机、又还没连上服务端时先画哪一版。
        """
        if self.mode_panel() == "music":
            self.mode_hint.config(
                text="仅是兜底：设备连不上服务端时先画歌词面板；切面板请用「服务」页的按钮")
        else:
            self.mode_hint.config(
                text="仅是兜底：设备连不上服务端时先画信息看板；切面板请用「服务」页的按钮")

    def refresh_drives(self):
        d = removable_drives()
        self.drive_cb["values"] = d
        cur = self.drive_var.get().strip()
        if d and (not cur or cur not in d):
            # 自动选中第一个可移动盘（H9 接入后会出现在这里）
            self.drive_var.set(d[0])

    def open_drive(self):
        d = self.drive_var.get().strip().rstrip("\\/") + "\\"
        try:
            os.startfile(d)
        except Exception as e:
            messagebox.showerror("打不开", str(e))

    def conf_text(self):
        music = self.mode_panel() == "music"
        panel = "music" if music else "dash"
        return ("# H9Dash 配置\n"
                "# 这个文件由 App 自动维护（自动发现到 PC 就会更新 host/port）。\n"
                "# 手工改也认：改完在设备上重开一次 App 即可。\n"
                "#\n"
                "# 想固定地址就在路由器上给电脑做 MAC 绑定，然后这里填死 host。\n"
                "\n"
                "# ---- 网络 ----\n"
                "host = %s\n"
                "port = %s\n"
                "token = %s\n"
                "\n"
                "# ---- 面板 ----\n"
                "# 注意：看哪个面板是**电脑**决定的，不是这里。设备每 4 秒问一次\n"
                "# 服务端的 /panel，所以电脑上一点切换，平板几秒内就跟着走。\n"
                "# 下面这个 mode 只在**连不上服务端**时兜底：开机先画哪一版。\n"
                "# mode = dash   信息看板\n"
                "# mode = music  网易云歌词面板\n"
                "mode = %s\n"
                "\n"
                "# ---- 刷新节奏 ----\n"
                "# interval_min 已废弃（统一 4 秒轮询 /panel）；留着兼容老配置。\n"
                "# 看板要不要重画由服务端的 rev 变更检测决定，内容没变就不刷屏。\n"
                "interval_min = %s\n"
                "flash = %d\n"
                "flash_min = 20\n"
                "\n"
                "# ---- 显示 ----\n"
                "rotate = 0\n"
                "orientation = portrait\n"
                "status = 1\n"
                "scale = fit\n"
                "\n"
                "# ---- 按键 ----\n"
                "keylog = 1\n"
                "key_prev = 92\n"
                "key_next = 93\n"
                % (self.ip_var.get().strip(),
                   self.port_var.get().strip() or str(PORT_DEFAULT),
                   get_token(),
                   panel,
                   self.interval_var.get().strip() or "30",
                   0 if music else 1))

    def preview_conf(self):
        self.refresh_url()
        self.conf_txt.delete("1.0", "end")
        self.conf_txt.insert("1.0", self.conf_text())

    def write_conf(self):
        d = self.drive_var.get().strip()
        if not d:
            messagebox.showinfo("提示", "先选盘符")
            return
        p = Path(d + "\\") / "dashboard.conf"
        try:
            p.write_text(self.conf_txt.get("1.0", "end").rstrip() + "\n", encoding="utf-8")
            self.log_line("已写入 %s" % p)
            messagebox.showinfo("完成", "已写入 %s" % p)
        except Exception as e:
            self.log_line("写入失败 %s -> %s" % (p, e))
            messagebox.showerror("写入失败", str(e))

    def deploy(self):
        d = self.drive_var.get().strip()
        if not d:
            messagebox.showinfo("提示", "先选盘符")
            return
        if not APK.exists():
            hint = ("找不到 %s\n\n" % APK)
            if FROZEN:
                hint += "把 H9Dash.apk 和这个 exe 放在同一个文件夹里再试。\n（或者从 GitHub Releases 重新下载 apk）"
            else:
                hint += "先编译：cd android && python build.py，或从 Releases 下载。"
            messagebox.showerror("缺少 APK", hint)
            return
        root_p = Path(d + "\\")
        try:
            import shutil
            shutil.copy2(APK, root_p / "H9Dash.apk")
            self.log_line("复制 %s -> %s" % (APK.name, root_p / "H9Dash.apk"))
            (root_p / "dashboard.conf").write_text(self.conf_text(), encoding="utf-8")
            self.log_line("写入 %s" % (root_p / "dashboard.conf"))
        except Exception as e:
            self.log_line("部署失败：%s" % e)
            messagebox.showerror("部署失败", str(e))
            return
        self.preview_conf()
        messagebox.showinfo(
            "完成",
            "已部署到 %s（面板模式：%s）\n\n"
            "接下来在设备上：文件管理器 → 点 H9Dash.apk → 安装 → 打开\n\n"
            "如果提示无法安装，需要在设备「设置 → 安全」里允许未知来源。\n\n"
            "如果提示「应用签名不一致」，说明设备上装的是用别的密钥签名的老版本，\n"
            "请先在设备上卸载 H9Dash，再装这一个。"
            % (d, self.mode_var.get()))

    def read_screen(self):
        d = self.drive_var.get().strip()
        if not d:
            messagebox.showinfo("提示", "先选盘符")
            return
        p = Path(d + "\\") / "h9dash_screen.txt"
        if not p.exists():
            self.screen_var.set("没找到 %s —— 先在设备上打开一次 H9Dash" % p)
            return
        try:
            self.screen_var.set(p.read_text(encoding="utf-8", errors="replace").strip())
        except Exception as e:
            self.screen_var.set("读取失败：%s" % e)

    # ---------------------------------------------------------------- 退出
    def on_close(self):
        if self.music_live:
            self.music_live = False
            self.music_stop_ev.set()
        if self.proc:
            if messagebox.askyesno("退出", "服务还在运行，要停止并退出吗？"):
                self.stop_server()
            else:
                return
        self.root.destroy()


def _restore_std_streams():
    """打包成 --windowed exe 后，sys.stdout/stderr 未必是 None，
    更常见是被引导器换成一个「写向黑洞」的占位对象 —— 服务横幅的每一句 print
    都被吞掉：GUI 用 stdout=PIPE 起的服务子进程一条日志也收不到，
    读管道的 readline 会永久阻塞。

    所以这里不看 is None，打包态一律用继承来的 OS fd 1/2 重建成未缓冲的 UTF-8 文本流。
    GUI 起子进程时 fd 1/2 就是那根管道，重建后即可正常回传日志。
    fd 不可用（比如被人单独双击、没重定向）就保持原样，那种场景也没人读管道。
    只在打包态调用，源码模式不动，避免把真实控制台也改掉。"""
    import io
    for name, fd in (("stdout", 1), ("stderr", 2)):
        try:
            setattr(sys, name, io.TextIOWrapper(
                os.fdopen(fd, "wb", 0), encoding="utf-8",
                errors="replace", write_through=True))
        except Exception:
            pass


def _dispatch_subcommand():
    """打包后 exe 复用同一份二进制扮演服务 / 渲染子进程。

    返回 True 表示本次进程是子命令、已处理完（调用方直接退出，不开 GUI）。
    标记 token（--server / --render）本身要摘掉，否则子模块的 argparse 会当成未知参数。
    """
    argv = sys.argv[1:]
    if argv and argv[0] in ("--server", "--render"):
        # 源码模式：server/render 住在 dashboard/ 子目录，得先挂上路径才能 import。
        # 打包后它们已在 exe 内部，无需处理。
        if not FROZEN and str(DASH) not in sys.path:
            sys.path.insert(0, str(DASH))
    if argv and argv[0] == "--server":
        if FROZEN:
            _restore_std_streams()
        sys.argv = [sys.argv[0]] + argv[1:]     # 去掉 --server，剩 --port N 等
        import server
        server.main()
        return True
    if argv and argv[0] == "--render":
        if FROZEN:
            _restore_std_streams()
        sys.argv = [sys.argv[0]] + argv[1:]     # 去掉 --render
        import render
        render.main()
        return True
    return False


def main():
    # 检查渲染依赖
    try:
        import PIL  # noqa
    except Exception:
        print("[警告] 未检测到 Pillow，渲染功能不可用。请执行：pip install -r requirements.txt")
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    if not _dispatch_subcommand():
        main()
