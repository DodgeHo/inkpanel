# 海尔 topsir H9 设备档（硬件 / 系统 / 开发 / 固件路线）

> 这份文档只记**这台机器本身**：硬件参数、系统环境、按键、屏幕驱动，
> 以及将来要做 USB 调试或改固件时的路线图。
>
> 软件开发经验（踩过的坑、架构决策）在 [`development-notes.md`](development-notes.md)。
>
> **最后实测：2026-09-17**（设备盘挂在 `G:`，App 在跑）。带 ⚠️ 的是未验证项。

---

## 0. 结论先行

H9 是一台**开放的 Android 4.0.4 平板**（文石 Onyx 血统），不是 Kindle 那种封闭 Linux。

- **能直接侧装 APK** → 当前方案（H9Dash）**不需要 root、不需要刷机、不需要 ADB**
- **ADB 目前没开**（2026-09-17 实测 `adb devices` 为空），但设备**能用**，所以一直没去动它
- ⚠️ **文石的 EPD SDK 在这台机器上不存在**（2026-09-17 实测四个类全部 `absent`）
  —— 这直接否决了"用 `EpdController` 做局刷"那条路，详见 [§7](#7-屏幕与-epd-刷新)

---

## 1. 硬件参数

| 项目 | 实测值 / 结论 | 来源 |
|---|---|---|
| 设备名 | `TOPSIR_H9`（PnP 名称） | `SWD\WPDBUSENUM\...TOPSIR_H9` |
| 屏幕 | 9.7" E-Ink，**16 阶灰**，前置 10 级背光 | 公开评测 |
| 物理分辨率 | **1200×825**（横版原生） | 公开评测 |
| **可用分辨率** | ⭐ **825×1152，dpi=160** | ⭐ App 实测写入 `h9dash_screen.txt` |
| 触控 | 电容 + 电磁（Wacom）双模，带手写笔 | 公开评测 |
| 主芯片 | Freescale **i.MX6SL**（Cortex-A9 单核 1GHz） | 同平台 Onyx M96 已证实 |
| 系统 | **Android 4.0.4（API 15）** | 公开评测 + 目录/APK 年代交叉验证 |
| 血统 | 文石 Onyx 系 | 装有 `com.onyx.android.data`、`com.onyx.kreader`（Neo Reader） |
| 存储 | 12.13 GB 总、**11.65 GB 可用** | 2026-09-17 实测 |
| USB VID/PID | `VID_18D1&PID_2D01` | ⚠️ 这是**大容量存储接口**，不是 ADB 接口 |
| 挂载方式 | **USB 大容量存储**（不是 MTP） | `Linux File-CD Gadget`，`Service=USBSTOR` |

> **825×1152 而不是 825×1200**：竖版可用高度是 1152，底部约 48px 被状态栏/系统 UI 吃掉。
> 服务端画布画 1200 高，所以**排版内容要压在 y ≤ 1130 以内**（看板已这么做，有测试钉住）。

---

## 2. 系统与软件环境

### 已装的系统包（说明血统）

- `com.onyx.android.data`
- `com.onyx.kreader`（Neo Reader 阅读器）

### 预装 APK

在 `G:\Android\apk\`：Kindle 4.13、微信读书、有道词典、WPS、多看、讯飞语音。

### 关键限制：TLS 只到 1.0

Android 4.0.4 的 TLS 栈最高 TLS 1.0，**现代 HTTPS 站点基本都连不上**。
→ **这是"联网取数全部放 PC 端"这个架构决策的根本原因**，不是偷懒。
设备只走局域网明文 HTTP。

### 未知来源

**实际是允许的**，只是设置菜单里被藏了。实测侧装 APK 成功。

---

## 3. USB 与存储

- 接电脑后挂成**盘符**（本机是 `G:`），可读写，无需任何驱动
- ⚠️ **连接时断时续**：写之前先 `Test-Path G:\`，断线重插后可写
- 设备盘根目录的文件**都是用户可见的普通文件**，这是当前唯一的"调试通道"：
  App 把诊断信息写在那里，我们用读文件代替 logcat

### 设备盘上的文件（2026-09-17）

| 文件 | 大小 | 说明 |
|---|---|---|
| `H9Dash.apk` | 37267 | 部署进去的 APK（09-17 00:26） |
| `dashboard.conf` | 670 | **含真实 IP 和 token**，严禁进仓库 |
| `h9dash_screen.txt` | 26 | 分辨率诊断 |
| `h9dash_onyx.txt` | 288 | EPD SDK 探测结果 |
| `h9dash_auto.txt` | 139 | 自动发现记录 |
| `.moffice_wr_check*` | 0 ×3 | WPS 留下的空文件，无关 |

---

## 4. 设置菜单（海尔深度定制，原生项被阉割）

一级菜单：`wifi` / `声音` / `存储` / `语言输入法` / `日期和时间` / `显示` / `其他`

「其他」里：`显示` / `蓝牙` / `切换版本（教育版）` / `备份和重置` / `存储` /
`电磁屏校准` / `VCOM` / `设备状态` / `认证信息`

**没有**：开发者选项、安全、应用、关于 —— 全部被阉割。

> 对比：同平台的文石 M96/N96 原厂 ROM **有开发者选项，且 USB 调试默认开启**。
> 所以这是海尔定制 UI 的锅，**不是硬件限制**。

---

## 5. 实体键与键码

实体键：电源、翻页上、**圆键（Back）**、翻页下。

### 当前配置（设备 `dashboard.conf`）

```ini
keylog   = 1
key_prev = 24      # KEYCODE_VOLUME_UP
key_next = 25      # KEYCODE_VOLUME_DOWN
```

### ⚠️ 这个取值还需要真机确认（有矛盾证据）

PC 侧 `logs/keys.log` 记录到过**未知键码**：

```
2026-09-16 20:25:10 keycode=92     # KEYCODE_PAGE_UP
2026-09-16 20:31:41 keycode=999
2026-09-16 20:37:49 keycode=92
2026-09-16 20:37:49 keycode=93     # KEYCODE_PAGE_DOWN
```

仓库默认值（`dashboard.conf.example` / `deploy.py`）是 **92 / 93**（PAGE_UP/DOWN），
设备上的实际配置现在是 **24 / 25**（VOLUME_UP/DOWN）。
两侧不一致，且 92/93 曾被当作"未知键"上报过 —— 说明**当时配置里不是 92/93**。

**⚠️ 待办：真机确认一次**。方法（不用重编译 APK）：

1. 按设备上的翻页键
2. 看设备盘 `h9dash_keys.log` 或 PC 侧 `logs/keys.log` 里出现的 keycode
3. 把 `dashboard.conf` 的 `key_prev` / `key_next` 改成那个值
4. **在设备上重开一次 App** 生效

> 圆键（Back）**留给系统，App 绝不拦截**。

---

## 6. 诊断文件（代替 logcat 的手段）

没有 ADB，所以 App 自己把诊断信息写到设备盘。这套东西非常有用，别删。

| 文件 | 谁写的 | 内容 | 用途 |
|---|---|---|---|
| `h9dash_screen.txt` | 启动时 | `825x1152 dpi=160 mode=dash` | ⭐ **判断"App 在不在跑、跑的是哪一版"最快的方法：看它的 mtime** |
| `h9dash_keys.log` | 按到未知键时 | `keycode=NNN` | 摸真实键码 |
| `h9dash_onyx.txt` | 启动时 | EPD SDK 探测 | 见 §7 |
| `h9dash_auto.txt` | 自动发现时 | 扫描结果 + 拿到 token | 排查"连不上 PC" |
| `h9dash_bad.txt` | 解析失败时 | 原始响应 HEAD/TAIL（留最近 3 条） | ⭐ 终结"解析失败"悬案的关键 |
| `h9dash.stop` | 用户放 | 空文件 | 放一个就让 App 退出（救急用） |

> **判断设备状态的标准动作**：先看 `h9dash_screen.txt` 的 mtime 是不是刚刚。
> 是刚刚 = App 活着；不是 = 跑的是旧进程或已退出。

---

## 7. 屏幕与 EPD 刷新

### ⭐ 2026-09-17 实测：文石 EPD SDK 全部不存在

App 启动时会 `Class.forName` 探四个类，结果（设备盘 `h9dash_onyx.txt`）：

```
absent com.onyx.android.sdk.api.device.epd.EpdController
absent com.onyx.android.sdk.api.device.epd.UpdateMode
absent com.onyx.android.sdk.api.device.epd.UpdateOption
absent com.onyx.android.sdk.api.device.DeviceEnvironment
```

**这条结论很重要**：H9 虽然装了 Onyx 的**应用**（`com.onyx.kreader`），
但**没有装 Onyx 的 SDK**。所以：

- ❌ 走 `EpdController.setViewDefaultUpdateMode(view, GU/REGAL)` 做局刷 —— **此路不通**
- ❌ `EpdDeviceManager.setGcInterval(N)` + `applyWithGCInterval()` —— 同样不通
- ✅ 剩下只有反射系统/View 层：
  - `View.postInvalidateDelayed(6参版)`（NTX / Freescale 系）
  - `View.refreshScreen(x, y, w, h, mode)`（Onyx 系）
  ⚠️ 两者都没在 H9 上试过

> **当前策略（已验证够用）**：不调用任何 EPD 接口，靠
> 「**不闪屏 + 只贴脏块 + 定时整屏刷（`flash_min`）**」来控残影。
> 没有真机能验证之前，宁可不动那些魔数 —— 猜错会把整屏刷成灰或留一堆残影。

### 波形耗时参考

同代 i.MX6 EPDC（800×600 数据）：**A2 125ms / DU 300ms / GC16 600ms**。
825×1200 量级会更高些。

→ 这就是"刷新是昂贵的"的物理原因：**别整屏贴新位图**，
脏区整屏 = 每刷必全屏波形 = 每 3 秒闪一下。

### 残影

躲不掉。局刷 5~10 次插一次全刷 → 大约每 15~30 秒闪一下。

### 交互建议

**优先用实体键**，少用屏上按钮。墨水屏上触屏按钮每次都要等刷新才能确认，
实体键有触感、不需要视觉反馈。

> 坑：Onyx 按键常常**松手才发 down/up 成对事件**，不能按"按下即响应"写。
> 另外界面要**禁用动画 / 渐变 / 透明度**。

---

## 8. 侧装 APK（当前唯一在用的通道）

1. H9 用 USB 线接电脑，等它挂成盘符（如 `G:`）
2. 把 APK 拷进去（GUI「设备」页的「部署 APK + 配置」会自动做，含写 `dashboard.conf`）
3. 在设备上：文件管理器 → 点 `H9Dash.apk` → 安装 → 打开

> ⚠️ **装完必须完全退出 App 再打开**，否则跑的还是旧进程（踩过：APK 23:50 部署，
> `h9dash_screen.txt` mtime 还是 23:55 → 白测一整轮）。

### 签名

`android/debug.keystore` **已提交进仓库**（口令是公开的 `android`/`android`），
目的是让所有人编出的 APK 签名一致、**升级能覆盖安装**。

> 换密钥编出来的 APK 签名不同 → 设备上报「签名不一致」→ **必须先卸载再装**。

---

## 9. 编译环境

不用 Gradle，直接调 Android SDK 命令行工具（`android/build.py`）。

| 组件 | 路径 / 版本 |
|---|---|
| Android SDK | `F:\Dev\Android\Sdk`（**路径不写死**，见下） |
| **build-tools** | ⭐ **28.0.3 —— 唯一完整且 d8 无 bug 的版本** |
| JDK | `C:\Program Files\Android\Android Studio\jbr`（JBR 21） |
| compileSdk | 26（platforms 里没有 15，用高的 + `minSdk 15` 即可） |

**路径不绑死本机**：`build.py` 按
`--sdk/--jbr` → `ANDROID_HOME`/`ANDROID_SDK_ROOT` → `H9DASH_SDK`/`H9DASH_JBR`
→ `JAVA_HOME` → 常见位置 的顺序探测。

### 编译链（跨项目可复用的配方）

1. **javac**：`-source 8 -target 8 -bootclasspath <android.jar> -encoding UTF-8 -parameters`
   ⭐ **必须加 `-parameters`**！JDK 21 的 javac 会在 class 里写 MethodParameters 属性
   且参数名为 null，R8/d8 读到直接 NPE。`-g:none` 也行
2. **d8**：`--min-api 15`。⭐ d8 **不接受目录输入**，只吃 .class/.jar → 先用 Python `zipfile` 打个 `classes.jar`
3. **aapt2 link**：`--min-sdk-version 15 --target-sdk-version 15`（无 res 目录也能过）
4. 把 `classes.dex` 用 Python `zipfile` 追加进 apk
5. **zipalign -f 4**
6. **apksigner**：`--min-sdk-version 15`

> ⚠️ **build-tools 34.0.0（R8 8.2.2）有 bug**：处理嵌套匿名类会 NPE。用 28.0.3。
> SDK 里只有 28.0.3 / 34.0.0 / 35 / 36 / 36.1 / 37.0.0-rc2 是完整的，其余目录是空壳。

### minSdk 15 的约束

- **不要引入 AndroidX**（AndroidX 最低支持 14+ 但依赖 modern support 库，且体积爆炸）
- 全屏用 `FLAG_FULLSCREEN` —— `SYSTEM_UI_FLAG_FULLSCREEN` 是 **API 16+**，用不了
- `AndroidManifest` 里 `largeHeap="true"`（825×1200 ARGB_8888 = **3.78 MB**，
  而 targetSdk=15 默认堆只有 16~24 MB）、`hardwareAccelerated="false"`

### 当前申请的权限

`INTERNET` / `ACCESS_NETWORK_STATE` / `ACCESS_WIFI_STATE` / `WAKE_LOCK` /
`RECEIVE_BOOT_COMPLETED` / `READ_EXTERNAL_STORAGE` / `WRITE_EXTERNAL_STORAGE`

---

## 10. ADB / 固件修改路线

### ⭐ 先问一句：现在需要开 USB 调试吗 —— 不需要，而且**开了是净损失**

用户实测：**USB 调试和设备盘挂载只能二选一**（这台机器上两者互斥）。

而当前项目的**全部**调试通道都建立在设备盘上：

| 依赖设备盘的东西 | 没了会怎样 |
|---|---|
| `h9dash_screen.txt` | 没法判断"App 在不在跑、跑的是不是新版" |
| `h9dash_bad.txt` | 解析失败时拿不到原始响应，退回"猜" |
| `h9dash_keys.log` | 摸不到真实键码 |
| `h9dash.stop` | 少了"放个文件就让 App 退出"这个救急开关 |
| 侧装 APK | 拷不进去，只能改用 `adb install` |
| `dashboard.conf` | 没法手工改配置自救 |

**所以结论很直接**：只要侧装 APK 还好用，就**永远不要为了调试去开 USB 调试** ——
那是用"能实时看 logcat"换掉"六条救命通道"，不划算。

#### 什么时候才值得开

只有这几件事**必须** ADB 才能做：

1. 看实时 `logcat`（设备盘诊断文件是"事后快照"，替代不了实时流）
2. 改系统属性 / 推 `/system` 文件（即下面的路线 2、3）
3. 抓 bugreport、看 `dumpsys`（比如想确认屏幕驱动到底走哪套）

**键码确认不算** —— 用 `keylog` 写 `h9dash_keys.log` 就够了，不用 ADB。

#### 如果真要开，先做完这些

1. **备份当前 `dashboard.conf`**（里面有能用的 host/port/token）
2. 确认 `H9Dash.apk` 在电脑上有副本，且知道怎么 `adb install -r`
3. 想清楚 `dashboard.conf` 改走 `adb push` 到哪个路径
   —— 设备盘没了之后，App 的 `sdcard()` 指向哪要重新确认
4. **留出退回来的路**：知道怎么在设置里关回去（或进 recovery 改 `persist.sys.usb.config`）

> ⚠️ 开关 USB 调试之后，GUI 的「部署 APK + 配置」会**整个失效**（它是往盘符拷文件）。
> 这不是 bug，是部署通道被换掉了。

#### 为什么互斥（技术背景）

Android 4.0 的 USB 功能由 `persist.sys.usb.config` 控制，
取值可以是 `mass_storage`、`adb`，或复合的 `mass_storage,adb`。
理论上复合值能让两者共存，但**这台机器的定制 ROM 显然只暴露了互斥的开关**，
所以用户看到的就是二选一。

→ 路线 2（自制 update.zip）里写 `persist.sys.usb.config=mass_storage,adb`
**正是为了打破这个互斥**，让它俩同时可用。

**当前用不上**（侧装 APK 直接可用）。真要做时从第一条开始试。

### 路线 1：进 recovery（风险低）

关机 → **电源 + 圆键（=Back）** 长按，指示灯橙变紫后松电源、继续按圆键
到 BOOX logo 出现。

recovery 是 bootloader 层，**设置阉割影响不到它**。

> ⚠️ 依 Onyx M96 经验，**H9 未实测**。

### 路线 2：自制 update.zip（风险中）

只做三件事：

```
persist.service.adb.enable=1
persist.sys.usb.config=mass_storage,adb
service.adb.tcp.port=5555
```

顺带可以把 APK 塞进 `/system/app`。

- Onyx recovery 会验签 → 报 `signature verification failed` 就用
  **AOSP testkey 的 `signapk`** 重签
- `/system` 挂载优先用 `/dev/block/.../by-name/system`，**别写死 `mmcblk0p?`**

> 为什么不能直接改 `persist.*`：那是系统属性，需要 root 或 recovery 才能写。
> 装一个 APK 去改是行不通的（没有 `android.permission.WRITE_SECURE_SETTINGS`）。

### 路线 3：MfgTool2 线刷 Onyx 原厂整包（风险高）

i.MX6 的 Serial Downloader 是 HID **`VID_15A2&PID_0063`**，
**可反复重刷，不会彻底变死砖** —— 这是 i.MX6 最大的安全垫。

> ⚠️ **最大的坑**：包内
> `Profiles/MX6SL Linux Update/OS Firmware/files/pcba_test/hwinfo`
> 决定了 `WIFI / TP / AUDIO / LIGHT / RESOLUTION / RAM`。
> **错配会丢触控、前光或 Wi-Fi。**
>
> H9 有前光 + 双触 + 16GB，硬件更接近 **N96 系**而非 M96。选包时按这个判断。

### 路线 4：拆机 / 拉 BOOT_MODE 测试点 / 焊 UART（风险最高）

最后手段。会失保，且有硬件损坏风险。

---

## 11. 明确不做的事

没有 ADB、没有原厂镜像、没有校验值、没有回滚路径时，不碰以下任何一项：

- ❌ 刷入其他型号的 boot / recovery / system 镜像
- ❌ 把 `G:` 盘当作系统分区或完整固件备份（**它只是 USB 大容量存储，不是系统分区**）
- ❌ 运行来源不明的一键 root 工具
- ❌ 任何分区写入操作

---

## 12. 待确认项（下次上机时顺手做掉）

| 事项 | 怎么确认 | 影响 |
|---|---|---|
| ⭐ 翻页键真实键码（24/25 还是 92/93） | 按键 → 看 `h9dash_keys.log` / `logs/keys.log` | 按键控制能不能用 |
| 前光能否由 App 控制 | 反射探 `/sys/class/backlight` 或 Onyx 私有接口 | 夜间可读性 |
| `View.postInvalidateDelayed` / `refreshScreen` 在不在 | 反射探测 | 能不能做真正的 EPD 局刷 |
| recovery 组合键在 H9 上是否成立 | 实际按一次 | 路线 1 是否可行 |
| 系统分区布局（`by-name` 有哪些） | 进 recovery 看 | 路线 2 是否可行 |

---

## 附：一次性采集脚本

`scripts/collect_device_info.ps1` —— 只读采集（adb 路径自动探测，不写设备）。
产物落在 `artifacts/device-info/`。

2026-09-15 跑过一次：`DeviceDetected: false`，`adb devices` 为空。
2026-09-17 复测：**仍然为空**。
