# -*- coding: utf-8 -*-
"""
deploy.py —— 命令行版部署脚本（GUI 里点「部署 APK + 配置」就是调这套逻辑）

用法：
    python deploy.py                              # 默认部署到 G:，信息看板模式
    python deploy.py --mode music                 # 部署成网易云歌词面板
    python deploy.py --drive E:                   # 指定盘符
    python deploy.py --port 9000 --interval 60

注意：APK 来自仓库根目录的 H9Dash.apk（由 android/build.py 生成）。
      如果设备上装过用别的密钥签名的老版本，升级会报签名不一致，
      需要先在设备上卸载老版本再装。
"""
import argparse
import shutil
import socket
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
APK = BASE / "H9Dash.apk"

# 配置格式说明：
#   设备端会自己扫局域网找 PC（AutoDiscover），所以 host/port 只是"起点"，
#   发现成功后会由 App 自己改写。token 同理 —— 设备从 /health 里直接拿。
#   所以这个文件主要负责"开机兜底面板"和"显示/按键"，网络部分只要大致对就行。
CONF_TMPL = """# H9Dash 配置
# 这个文件由 App 自动维护（自动发现到 PC 就会更新 host/port）。
# 手工改也认：改完在设备上重开一次 App 即可。
#
# 想固定地址就在路由器上给电脑做 MAC 绑定，然后这里填死 host。

# ---- 网络 ----
host = {ip}
port = {port}
token = {token}

# ---- 面板 ----
# 注意：**看哪个面板是电脑决定的，不是这里决定的。**
# 设备每 4 秒问一次服务端的 /panel，所以：
#   · 电脑上点一下切换（GUI「服务」页，或 POST /panel）→ 平板几秒内跟过去
#   · 平板上长按屏幕 → 菜单里也能切，改的同样是服务端状态
# 下面这个 mode 只在**连不上服务端**时兜底：开机先画哪一版。
#   mode = dash   信息看板
#   mode = music  网易云歌词面板
mode = {mode}

# ---- 刷新节奏 ----
# interval_min 已废弃（设备统一 4 秒轮询 /panel），留着是为了兼容老配置。
# 看板真正"要不要重画"由服务端的 rev 变更检测决定 —— 内容没变就不刷屏。
interval_min = {interval}
flash = {flash}
flash_min = 20

# ---- 显示 ----
rotate = 0
orientation = portrait
status = 1
scale = fit

# ---- 按键 ----
keylog = 1
key_prev = 92
key_next = 93
"""


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def token():
    p = Path.home() / ".h9dash" / "token"
    if p.exists():
        t = p.read_text(encoding="utf-8").strip()
        if t:
            return t
    import secrets
    p.parent.mkdir(parents=True, exist_ok=True)
    t = secrets.token_urlsafe(32)
    p.write_text(t, encoding="utf-8")
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drive", default="G:", help="设备存储盘符，如 G: 或 E:")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=int, default=30, help="dash 模式刷新间隔（分钟）")
    ap.add_argument("--mode", choices=["dash", "music"], default="dash",
                    help="开机兜底面板：dash 信息看板 / music 歌词面板"
                         "（真正的切换在 PC 的 GUI 或 POST /panel）")
    args = ap.parse_args()

    drive = Path(args.drive.rstrip("\\/") + "\\")
    if not drive.exists():
        sys.exit("[错误] 没找到 %s，请确认设备已用 USB 存储接入" % drive)
    if not APK.exists():
        sys.exit("[错误] 没找到 %s（先在 android/ 里跑 build.py）" % APK)

    shutil.copy2(APK, drive / "H9Dash.apk")
    print("[OK] 复制 APK -> %s" % (drive / "H9Dash.apk"))

    conf = CONF_TMPL.format(
        mode=args.mode,
        ip=lan_ip(), port=args.port, token=token(),
        interval=args.interval,
        flash=1 if args.mode == "dash" else 0)
    (drive / "dashboard.conf").write_text(conf, encoding="utf-8")
    print("[OK] 写入配置 -> %s" % (drive / "dashboard.conf"))
    print(conf)
    print("[提示] 设备端会自己扫局域网找 PC，所以这里 IP 写错也没关系 ——")
    print("       开机首帧失败会自动搜索并改正，也可以长按屏幕选「重新搜索电脑」。")
    print("[提示] 部署的 mode 只是开机兜底；想看另一个面板不用重新部署 ——")
    print("       在 PC 的 GUI「服务」页点一下切换，或 curl -X POST .../panel 即可。")


if __name__ == "__main__":
    main()
