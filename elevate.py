# -*- coding: utf-8 -*-
"""
elevate.py —— 防火墙放行的「按需提权」：程序平时普通权限，要管理员的那一步
              通过 UAC 只提权一小下，而且一辈子只需要一次。

背景（为什么要有这个模块）：

  放行防火墙（netsh advfirewall）需要管理员。一个"谁都会用"的 exe
  总不能整体要求管理员 —— 那每次启动都弹 UAC，而且整个进程一直
  拿着管理员令牌跑，难看也没必要。

  Windows 的正规解法就是「按需提权」：平时什么都不做；用户点
  「放行防火墙」时，用 ShellExecuteEx 的 "runas" 动词启动一个
  **只干加规则这一件事**的提权子进程。系统弹一次 UAC，用户点「是」，
  规则写进防火墙（永久生效），子进程退出，之后再也不弹。
  各种软件的安装器都是这么干的。

为什么规则按「端口」放、不按「程序路径」放：

  打包成单文件 exe（PyInstaller onefile）后，每次运行都会先把自己
  解压到一个**随机名字**的临时目录再执行。按程序路径匹配的防火墙
  规则永远对不上号，Windows 的"允许访问"弹窗会无穷无尽地出现。
  按端口放行（TCP 8765）与路径无关，一次就够 —— 代价是任何程序
  监听这个端口都被放行，但服务本身带 token 校验，且不该暴露到公网，
  这个代价可以接受。

只用标准库 + ctypes：和 music/ 包同一个理由 —— 32 位解释器上
pywin32 / comtypes 的轮子经常装不上，纯 ctypes 两边都能跑。

自检（只读，不会弹 UAC）：
    python elevate.py 8765        # 看这个端口的规则在不在
    python elevate.py 8765 --apply  # 真的去确保规则存在（可能弹 UAC）
"""
import subprocess
import sys
import tempfile
from pathlib import Path

RULE_PREFIX = "H9Dash"


def rule_name(port) -> str:
    """防火墙规则名。带上端口，改端口后能各放各的。"""
    return "%s %s" % (RULE_PREFIX, port)


# ---------------------------------------------------------------- 探测

def rule_exists(port):
    """放行 TCP port 的规则在不在。

    返回 True / False；探测手段本身不可用（没有 PowerShell 等）返回
    None —— 调用方对 None 的正确反应是"当作不知道"，别当成"不在"。

    为什么用 Get-NetFirewallRule 而不是 netsh show rule：
    netsh 的输出跟着系统界面语言走（中文系统是"没有与指定标准相匹配
    的规则"，英文系统是别的），解析文本等于把程序绑死在一种语言上。
    PowerShell 这里回 True/False，全世界都一样。
    标准账户查询防火墙规则不需要管理员 —— 只有"改"才需要。
    """
    ps = ("[bool](Get-NetFirewallRule -DisplayName '%s' "
          "-ErrorAction SilentlyContinue)" % rule_name(port))
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=25)
    except Exception:
        return None
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out in ("True", "False"):
        return out == "True"
    return None


def _add_args(port):
    """加规则的 netsh 参数（不提权直接跑，管理员会话下就能成）。"""
    return ["netsh", "advfirewall", "firewall", "add", "rule",
            "name=%s" % rule_name(port),
            "dir=in", "action=allow", "protocol=TCP",
            "localport=%s" % port, "profile=any"]


def add_rule_direct(port):
    """不提权直接加（当前会话本来就是管理员时一步到位，省一次 UAC）。"""
    try:
        r = subprocess.run(_add_args(port), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
    except Exception as e:
        return False, repr(e)
    detail = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode == 0, detail


# ---------------------------------------------------------------- 提权执行

# 带 NOCLOSEPROCESS 才能拿到子进程句柄：不然 ShellExecute 是
# "发射后不管"，成没成功只能靠猜。
SEE_MASK_NOCLOSEPROCESS = 0x00000040
WAIT_TIMEOUT_MS = 0x00000102


def run_elevated(exe, params, timeout_s=180):
    """用 UAC 提权跑一条命令，等它结束。返回 (status, exit_code)。

    status:
      "ok"        跑完了（退出码在第二个返回值里）
      "cancelled" 用户在 UAC 确认框上点了「否」
      "failed"    启动失败 / 读退出码失败
      "timeout"   等太久（多半是 UAC 弹窗一直没人点）
    """
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", wintypes.LPVOID),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.hwnd = None
    info.lpVerb = "runas"          # ← 就是这个动词触发 UAC
    info.lpFile = exe
    info.lpParameters = params
    info.lpDirectory = None
    info.nShow = 0                 # SW_HIDE：别闪黑框

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == 1223:            # ERROR_CANCELLED —— 用户点了「否」
            return "cancelled", None
        return "failed", None

    if not info.hProcess:          # 理论上不会（带了 NOCLOSEPROCESS）
        return "ok", None

    try:
        rv = kernel32.WaitForSingleObject(info.hProcess, int(timeout_s * 1000))
        if rv == WAIT_TIMEOUT_MS:
            return "timeout", None
        code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            return "failed", None
        return "ok", int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def _elevated_params(port, out_file):
    """提权 cmd 里跑的一串命令：先删同名规则再加 —— 幂等，重复点不堆规则。

    add 的输出重定向到 out_file：提权子进程的 stdout 我们拿不到（它在
    管理员上下文里），落个文件才能把失败原因带回来给用户看。
    """
    return ('/c netsh advfirewall firewall delete rule name="%s" >nul 2>&1 '
            '& netsh advfirewall firewall add rule name="%s" dir=in '
            'action=allow protocol=TCP localport=%s profile=any > "%s" 2>&1'
            % (rule_name(port), rule_name(port), port, out_file))


# ---------------------------------------------------------------- 入口

def ensure_rule(port):
    """确保「放行 TCP port 入站」的规则存在 —— GUI 的「放行防火墙」调这个。

    返回 (status, detail)：
      status ∈ "already" / "ok" / "cancelled" / "timeout" / "failed"
    顺序：先看在不在 → 不提权直接试 → 都不行才弹 UAC。
    """
    port = int(port)

    if rule_exists(port) is True:
        return "already", "规则已存在，不用动"

    ok, detail = add_rule_direct(port)
    if ok:
        return "ok", "已添加（当前会话是管理员，没弹 UAC）"

    # 走到这多半就是权限不够（netsh 会报"请求的操作需要提升"）。
    out_file = Path(tempfile.gettempdir()) / "h9dash_fw_result.txt"
    try:
        out_file.unlink()
    except OSError:
        pass

    status, code = run_elevated("cmd.exe", _elevated_params(port, out_file))

    add_out = ""
    try:
        add_out = out_file.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        pass

    if status == "ok" and code == 0:
        return "ok", add_out or "已添加"
    if status == "cancelled":
        return "cancelled", "在 UAC 确认框上点了「否」"
    if status == "timeout":
        return "timeout", "提权命令超时（UAC 弹窗一直没点？）"
    return "failed", add_out or detail or ("exit=%s" % code)


if __name__ == "__main__":
    # 只读自检：python elevate.py 8765 [--apply]
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    p = sys.argv[1]
    print("规则 %s 存在吗：%r" % (rule_name(p), rule_exists(p)))
    if "--apply" in sys.argv:
        print("ensure_rule ->", ensure_rule(p))
