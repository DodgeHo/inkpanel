# -*- coding: utf-8 -*-
"""
build_exe.py —— 一键打包「H9Dash 控制台.exe」

GitHub Release 里那个免 Python 的 exe 就是这个脚本编出来的。

用法（在仓库根目录，用项目的 Python 跑）：
    python build_exe.py

它做的事：
  1. 没有就建 .venv（用当前解释器），装 requirements.txt + pyinstaller；
  2. PyInstaller 打包 topsir_gui.py → dist/H9Dash 控制台.exe：
     单文件、无控制台窗口、中文名、带图标和版本信息；
     dashboard 里的模块（server / render / data_sources）以**顶层模块**
     打进 exe（--paths dashboard），exe 复用同一套代码扮演
     服务/渲染子进程（见 topsir_gui.py 的 --server / --render 分发）；
     顺手把 H9Dash.apk（约 40 KB）内嵌进 exe 当兜底 —— 忘了把 apk
     和 exe 放一起也能部署设备（exe 旁边的 apk 仍然优先）。

怎么跑的就怎么编：项目日常在 32 位 Python 上实测（music 包的
ctypes 代码在那上面验证过），用别的解释器打出来的包没人背过锅。
32 位 exe 在 32/64 位 Windows 上都能跑，所以也没有换 64 位的理由。
"""
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
NAME = "H9Dash 控制台"


def run(cmd):
    print("+", " ".join(str(c) for c in cmd))
    subprocess.check_call([str(c) for c in cmd])


def main():
    if not (VENV / "Scripts" / "python.exe").exists():
        print("建 .venv（用 %s）" % sys.executable)
        venv.create(VENV, with_pip=True)

    py = VENV / "Scripts" / "python.exe"
    run([py, "-m", "pip", "install", "-q",
         "-r", str(ROOT / "requirements.txt"), "pyinstaller"])

    args = [
        py, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",           # 单文件 —— Release 就一个 exe
        "--windowed",          # 无控制台黑框
        "--name", NAME,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--paths", str(ROOT / "dashboard"),   # server/render/data_sources 顶层打进
        # GUI 是运行期才 import 这三个模块（--server / --render 分发），
        # 静态分析看不到，不显式声明就不会进 exe，子进程起服务必然 ModuleNotFound。
        "--hidden-import", "server",
        "--hidden-import", "render",
        "--hidden-import", "data_sources",
        "--icon", str(ROOT / "packaging" / "h9dash.ico"),
        "--version-file", str(ROOT / "packaging" / "version_info.txt"),
    ]

    apk = ROOT / "H9Dash.apk"
    if apk.exists():
        args += ["--add-data", "%s;." % apk]   # 内嵌兜底（40 KB，不亏）
    else:
        print("[提示] 根目录没有 H9Dash.apk，跳过内嵌"
              "（exe 旁边那份仍然可用，编译：cd android && python build.py）")

    args.append(str(ROOT / "topsir_gui.py"))
    run(args)

    exe = ROOT / "dist" / ("%s.exe" % NAME)
    print()
    print("=" * 68)
    print("  完成：%s" % exe)
    print("  冒烟测试：")
    print("    %s --render            # 渲一张图到 %%APPDATA%%\\H9Dash" % exe)
    print("    %s --server --port 8765  # 起服务" % exe)
    print("=" * 68)


if __name__ == "__main__":
    main()
