#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build.py —— 不用 Gradle，直接调用 Android SDK 命令行工具编译 H9Dash.apk

用法：
    python build.py            # 编译并签名
    python build.py --deploy   # 编译后顺手拷到 G:  （设备已用 USB 存储接入时）

SDK / JDK 路径怎么定（按优先级，谁先命中用谁）：
    1. 命令行   --sdk <SDK 根目录>  --jbr <JDK 的 bin 目录>
    2. 环境变量 ANDROID_HOME / ANDROID_SDK_ROOT   （标准名，装了 SDK 的机器通常都有）
    3. 环境变量 H9DASH_SDK / H9DASH_JBR           （本项目自己的覆盖口子）
    4. JAVA_HOME  （JDK）
    5. 常见默认安装位置
所以别人 clone 下来一般不用改代码，只要 SDK 在标准位置或设了环境变量。
"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _default_sdk() -> Path:
    """按「标准环境变量 → 常见安装位置」猜 SDK 根目录。"""
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "H9DASH_SDK"):
        v = os.environ.get(var)
        if v and (Path(v) / "platforms").is_dir():
            return Path(v)
    for cand in (r"C:\Android\Sdk",
                 r"D:\Android\Sdk",
                 str(Path.home() / "AppData" / "Local" / "Android" / "Sdk"),
                 r"F:\Dev\Android\Sdk"):      # 本项目开发机上的位置，放最后
        if (Path(cand) / "platforms").is_dir():
            return Path(cand)
    # 一处都没找到就先给个通用位置，让后面「缺少 xxx」的报错把真实原因说清楚
    return Path(r"C:\Android\Sdk")


def _default_jbr() -> Path:
    """按「环境变量 → Android Studio 自带 JBR → JAVA_HOME」猜 JDK 的 bin 目录。"""
    v = os.environ.get("H9DASH_JBR")
    if v:
        return Path(v)
    for cand in (r"C:\Program Files\Android\Android Studio\jbr\bin",
                 r"D:\Program Files\Android\Android Studio\jbr\bin",
                 str(Path.home() / "AppData" / "Local" / "Programs"
                     / "Android" / "Android Studio" / "jbr" / "bin")):
        if Path(cand).is_dir():
            return Path(cand)
    jh = os.environ.get("JAVA_HOME")
    if jh:
        return Path(jh) / "bin"
    return Path(r"C:\Program Files\Android\Android Studio\jbr\bin")


# 下面几个模块级常量会在 main() 里被 --sdk / --jbr 覆盖后重新计算
SDK = _default_sdk()
JBR = _default_jbr()

ANDROID_JAR = SDK / "platforms" / "android-26" / "android.jar"
BT = SDK / "build-tools" / "28.0.3"   # 34.0.0 的 d8(R8 8.2.2) 处理嵌套匿名类会 NPE，用老版本
AAPT2 = BT / "aapt2.exe"
ZIPALIGN = BT / "zipalign.exe"
D8_JAR = BT / "lib" / "d8.jar"
APKSIGNER_JAR = BT / "lib" / "apksigner.jar"

JAVAC = JBR / "javac.exe"
KEYTOOL = JBR / "keytool.exe"
JAVA = JBR / "java.exe"


def _apply_paths(sdk: str | None, jbr: str | None) -> None:
    """用命令行给的 --sdk / --jbr 重新推导那一串工具路径（原地改模块全局）。

    命令行只给一个也行：没给的那个保持环境变量 / 默认值推导出来的结果。
    """
    global SDK, JBR, ANDROID_JAR, BT, AAPT2, ZIPALIGN, D8_JAR, APKSIGNER_JAR
    global JAVAC, KEYTOOL, JAVA

    if sdk:
        SDK = Path(sdk)
    if jbr:
        JBR = Path(jbr)

    ANDROID_JAR = SDK / "platforms" / "android-26" / "android.jar"
    BT = SDK / "build-tools" / "28.0.3"
    AAPT2 = BT / "aapt2.exe"
    ZIPALIGN = BT / "zipalign.exe"
    D8_JAR = BT / "lib" / "d8.jar"
    APKSIGNER_JAR = BT / "lib" / "apksigner.jar"

    JAVAC = JBR / "javac.exe"
    KEYTOOL = JBR / "keytool.exe"
    JAVA = JBR / "java.exe"

BUILD = BASE / "build"
# 产物直接吐到仓库根目录：GUI 的「部署」和 deploy.py 都从那里取 APK，避免两份不同步
OUT_APK = BASE.parent / "H9Dash.apk"
KS = BASE / "debug.keystore"


LOG = BASE / "build.log"
LOG.write_text("", encoding="utf-8")


def log(msg=""):
    print(msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(str(msg) + "\n")


def run(cmd, **kw):
    log("  > " + " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    if r.stdout and r.stdout.strip():
        log(r.stdout.strip()[:3000])
    if r.stderr and r.stderr.strip():
        log("[stderr] " + r.stderr.strip()[:3000])
    if r.returncode != 0:
        log(f"[失败] 返回码 {r.returncode}")
        sys.exit(1)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deploy", action="store_true", help="编译后拷到 G:\\")
    ap.add_argument("--sdk", default=None,
                    help="Android SDK 根目录（不填则用 ANDROID_HOME / 常见位置）")
    ap.add_argument("--jbr", default=None,
                    help="JDK 的 bin 目录（不填则用 Android Studio 的 jbr / JAVA_HOME）")
    args = ap.parse_args()

    # 路径解析必须在"检查文件是否存在"之前 —— 否则 --sdk 指了也会用错的默认值去校验。
    _apply_paths(args.sdk, args.jbr)

    missing = [p for p in (ANDROID_JAR, AAPT2, ZIPALIGN, D8_JAR, APKSIGNER_JAR,
                           JAVAC, KEYTOOL) if not p.exists()]
    if missing:
        # 把"为什么找不到"一次说清楚，别让用户对着一个路径猜
        print("[错误] 缺少下面这些文件：")
        for p in missing:
            print("        %s" % p)
        print()
        print("  SDK 当前用的是: %s" % SDK)
        print("  JDK 当前用的是: %s" % JBR)
        print("  改法（任一）：")
        print("    · set ANDROID_HOME=D:\\Android\\Sdk")
        print("    · python build.py --sdk D:\\Android\\Sdk --jbr \"C:\\Program Files\\Android\\Android Studio\\jbr\\bin\"")
        print("  另外本项目的 d8 必须来自 build-tools 28.0.3（34.x 处理嵌套匿名类会崩）。")
        print("  缺 build-tools 就在 Android Studio 的 SDK Manager 里勾上 28.0.3。")
        sys.exit(1)

    if BUILD.exists():
        shutil.rmtree(BUILD)
    (BUILD / "classes").mkdir(parents=True, exist_ok=True)

    srcs = [str(p) for p in (BASE / "src").rglob("*.java")]

    # javac 21 会在 class 里留 MethodParameters 属性，部分 d8 版本读到空参数名会 NPE，
    # 换几组参数试试，哪组能过 d8 就用哪组。
    variants = [
        ["-parameters"],
        ["-g:none"],
        ["-g:none", "-parameters"],
        [],
    ]

    dex = BUILD / "classes.dex"
    ok = False
    for extra in variants:
        log("[1/6] 编译 Java  参数: " + (" ".join(extra) or "(默认)"))
        if (BUILD / "classes").exists():
            shutil.rmtree(BUILD / "classes")
        (BUILD / "classes").mkdir(parents=True, exist_ok=True)
        if dex.exists():
            dex.unlink()

        r = subprocess.run(
            [str(JAVAC), "-source", "8", "-target", "8",
             "-bootclasspath", str(ANDROID_JAR),
             "-classpath", str(ANDROID_JAR),
             "-encoding", "UTF-8", "-nowarn"]
            + extra + ["-d", str(BUILD / "classes")] + srcs,
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            log("[javac 失败] " + (r.stderr or "")[:1500])
            continue

        classes = sorted((BUILD / "classes").rglob("*.class"))
        jar_path = BUILD / "classes.jar"
        with zipfile.ZipFile(jar_path, "w", zipfile.ZIP_DEFLATED) as z:
            for c in classes:
                z.write(c, str(c.relative_to(BUILD / "classes")))

        log("[2/6] 打包 dex ...")
        d = subprocess.run(
            [str(JAVA), "-cp", str(D8_JAR), "com.android.tools.r8.D8",
             "--min-api", "15", "--lib", str(ANDROID_JAR),
             "--output", str(BUILD), str(jar_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if d.returncode == 0 and dex.exists():
            log("      d8 通过")
            ok = True
            break
        log("[d8 失败，换参数重试] " + (d.stderr or "")[:800])

    if not ok or not dex.exists():
        sys.exit("[错误] 所有 javac 参数组合都过不了 d8")

    dex = BUILD / "classes.dex"
    if not dex.exists():
        sys.exit("[错误] 没有生成 classes.dex")

    # 3. aapt2 link
    log("[3/6] aapt2 link ...")
    unsigned = BUILD / "unsigned.apk"
    run([str(AAPT2), "link",
         "-I", str(ANDROID_JAR),
         "--manifest", str(BASE / "AndroidManifest.xml"),
         "--min-sdk-version", "15",
         "--target-sdk-version", "15",
         "-o", str(unsigned)])

    # 4. 把 dex 塞进 apk
    log("[4/6] 注入 classes.dex ...")
    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(dex, "classes.dex")

    # 5. zipalign
    log("[5/6] zipalign ...")
    aligned = BUILD / "aligned.apk"
    run([str(ZIPALIGN), "-f", "4", str(unsigned), str(aligned)])

    # 6. 签名
    log("[6/6] 签名 ...")
    if not KS.exists():
        run([str(KEYTOOL), "-genkeypair", "-v",
             "-keystore", str(KS), "-storepass", "android",
             "-alias", "androiddebugkey", "-keypass", "android",
             "-keyalg", "RSA", "-keysize", "2048", "-validity", "10950",
             "-dname", "CN=Android Debug,O=Android,C=US"])
    if OUT_APK.exists():
        OUT_APK.unlink()
    run([str(JAVA), "-cp", str(APKSIGNER_JAR), "com.android.apksigner.ApkSignerTool",
         "sign",
         "--ks", str(KS),
         "--ks-pass", "pass:android",
         "--key-pass", "pass:android",
         "--ks-key-alias", "androiddebugkey",
         "--min-sdk-version", "15",
         "--out", str(OUT_APK),
         str(aligned)])

    print(f"\n[OK] {OUT_APK}  ({OUT_APK.stat().st_size} bytes)")

    if args.deploy:
        g = Path("G:\\")
        if g.exists():
            dst = g / "H9Dash.apk"
            shutil.copy2(OUT_APK, dst)
            print(f"[OK] 已拷贝到 {dst}")
        else:
            print("[提示] 没找到 G:\\，跳过部署")


if __name__ == "__main__":
    main()
