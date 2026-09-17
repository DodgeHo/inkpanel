package com.wu.h9dash;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.FileReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.Properties;

import org.json.JSONArray;
import org.json.JSONObject;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.PowerManager;
import android.util.Base64;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.TextView;

/**
 * H9Dash —— 海尔 topsir H9 / 老安卓墨水屏看板 + 网易云音乐歌词面板
 *
 * Android 4.0.4 (API 15)，零第三方依赖，全用框架原生 API。
 * 配置文件在 SD 卡根目录：dashboard.conf
 *
 *   # ---- 通用 ----
 *   host         = 192.168.1.100    PC 地址（自动发现成功后会自动写回）
 *   port         = 8765
 *   token        = xxxxx            /health 里抄来的；设备自己也会自动取
 *   mode         = dash | music     **仅兜底**：只在连不上服务端时决定先画哪版
 *   rotate       = 0                dash 模式用；music 模式必须 0
 *   orientation  = portrait
 *   status       = 1                底部状态行 0/1
 *   scale        = fit              fit / crop / stretch
 *   keylog       = 1                未知按键写日志 + 上报（摸键码用）
 *
 *   # ---- dash 模式 ----
 *   flash        = 1                重绘前黑白闪屏去残影
 *
 *   # ---- music 模式 ----
 *   flash_min    = 20               每 N 分钟整屏黑白闪一次去残影；0 = 不闪
 *   key_prev     = 92               上一首（KEYCODE_PAGE_UP）
 *   key_next     = 93               下一首（KEYCODE_PAGE_DOWN）
 *
 * 在 SD 卡根目录放一个名为 h9dash.stop 的文件即可退出看板。
 *
 * ---------------------------------------------------------------
 * 关键设计（都是被墨水屏逼出来的，别乱改）
 *
 * 0) **看哪个面板，由电脑决定，不由设备决定。**
 *    设备只有一个轮询循环：先 GET /panel 问"现在该看哪个"，再按答案取图。
 *    所以电脑侧一点切换，设备几秒内就跟上；设备长按菜单切换改的也是
 *    服务端那份状态（POST /cmd panel_toggle），两边永远一致。
 *    配置里的 mode 退化成"服务端连不上时的兜底"，别再往里塞逻辑。
 *
 * 1) **音乐模式不闪屏。** 面板每 2~3 秒就要更新一次歌词和进度，
 *    再用黑白闪屏就等于一直在闪。所以 music 模式默认整屏不闪，
 *    残影靠 flash_min 定时整屏刷一次来清。
 *
 * 2) **只贴脏块，不重拉整图。** 服务端 /music/crops 会告诉设备"哪几个矩形变了"
 *    并直接给出那几块的 PNG（base64）。设备把它们 Canvas 贴到手头那张整图
 *    （fullBmp）上再 invalidate，省掉 97% 的流量和解码开销。
 *
 * 3) **帧号对不上就必须整屏重铺。** 脏块是"相对上一帧"算出来的；
 *    设备每轮回传自己手里的 frame_id，服务端判断能否拼接。
 *    拼接不会错，错了也不会花 —— 服务端会直接回整帧。
 *
 * 4) **触摸分区是服务端画好、随响应一起下发的**，设备只做坐标命中。
 *    所以改版式不用改 APK；DPR/缩放都在设备端换算。
 *    切到看板时必须清掉分区（看板没有可点区域）。
 *
 * 5) **圆键（Back）留给系统**，绝不拦截；翻页键按 key_prev/key_next 映射。
 *    键码不确定，所以任何没收到的键都会写进 h9dash_keys.log，方便回查。
 *
 * 6) **看板靠 rev 变更检测避免白闪。** 设备带 ?rev=N 请求，内容没变时
 *    服务端只回一个几十字节的 JSON，设备什么都不做 —— 所以轮询可以密，
 *    但只在内容真的变了才刷屏。
 */
public class MainActivity extends Activity {

    private static final String TAG = "H9Dash";
    private static final String STOP = "h9dash.stop";
    private static final String SCREEN_TXT = "h9dash_screen.txt";
    private static final String KEYS_LOG = "h9dash_keys.log";
    private static final String ONYX_TXT = "h9dash_onyx.txt";
    private static final String AUTO_TXT = "h9dash_auto.txt";   // 自动发现的记录
    private static final String BAD_TXT = "h9dash_bad.txt";     // 解析失败时的原始响应

    /** 长按超过这个时长就算"长按"，不当作翻页/点击。 */
    private static final long LONG_PRESS_MS = 900;

    /**
     * 统一轮询的节奏。
     *
     * 为什么是"短轮询"而不是"dash 30 分钟一次"：
     * 面板归属由电脑决定，设备只能靠轮询才知道"电脑刚把面板切走了"。
     * 轮询间隔太长 → 切了要等半天；太短 → 白烧电。4 秒是个折中：
     * 电脑点一下切换，设备最多 4 秒就翻过去了。
     *
     * 注意这**不等于**每 4 秒刷一次墨水屏 —— dash 有 rev 变更检测，
     * 没变的时候服务端只回一个几十字节的 JSON，设备什么都不做。
     */
    private static final long POLL_MS = 4000;

    /** 面板状态轮询失败时的重试间隔（和取图失败分开，避免一起打服务端）。 */
    private static final long POLL_FAIL_MS = 8000;

    private ImageView imageView;
    private TextView statusView;
    private View flashView;
    private View rootView;
    private Handler handler;

    // ------------------------------------------------------------- 配置

    private NetConfig conf;                       // 全部配置都在这
    private String mode = "dash";            // dash | music —— 只作服务端连不上时的兜底
    private int intervalMin = 30;            // 旧配置遗留；轮询节奏现在由 POLL_MS 固定
    private int rotate = 0;
    private boolean showStatus = true;
    private boolean doFlash = true;
    private boolean keylog = true;
    private String scale = "fit";
    private int flashMin = 20;               // music 模式整屏去残影间隔（分钟），0=不闪
    private int keyPrev = KeyEvent.KEYCODE_PAGE_UP;
    private int keyNext = KeyEvent.KEYCODE_PAGE_DOWN;

    // ------------------------------------------------------- 派生地址

    private String host = "192.168.1.100";
    private int port = 8765;
    private String token = "";
    private String panelUrl = "";      // GET /panel —— 免 token，问"现在该看哪个面板"
    private String dashUrl = "";       // GET /dash.png
    private String cropsUrl = "";      // GET /music/crops
    private String cmdUrl = "";        // POST /cmd

    // --------------------------------------------- 面板归属（服务端说了算）

    /** 服务端当前指定的面板。设备只跟随，不自己决定。 */
    private String active = "dash";
    /** 服务端给的面板版本号；变了说明"面板真的换了"，要重铺底图。 */
    private long panelSeq = -1;
    /** dash 面板的内容版本号；和服务端一致就不必刷屏。 */
    private long dashRev = 0;
    /** 本轮该睡多久（毫秒），由上面几个状态算出来。 */
    private boolean polling = false;
    /** /panel 连续失败次数，用于把状态行从"获取失败"升级成"连不上电脑"。 */
    private int pollFails = 0;
    /** fetchBytes 顺带读回的 X-H9Dash-Rev（>0 有效），用完清零。 */
    private long dashRevHeader = 0;

    /**
     * 上一条 HTTP 是「发得出去、收不回来」的（连不上 / 超时 / 断流 / 长度对不上）。
     *
     * 只用来决定屏底状态行怎么说人话：网络问题说「网络断了」，能拿到完整
     * 响应但解析不了才说「解析失败」。以前两种情况都甩一句"解析失败"，
     * 排查时会把方向全带歪 —— 明明服务好好的，看着却像服务在吐坏数据。
     */
    private boolean lastFetchIncomplete = false;

    /**
     * 拿不到画面时最多连试几轮，超过就把手里那帧作废，逼下一轮整帧重来。
     *
     * 为什么需要这个上限：
     *   如果服务端某一段响应一直有问题，而设备一直拿着 frame_id = N，
     *   那它永远只会要「N+1 的脏块」。脏块是往现有底图上贴的 ——
     *   底图本来就错了的话，贴一万次也是错的，屏上就是"定格"。
     *   作废帧号后下一轮 from=-1 → 拿整帧，是唯一能把状态拉回来的路径。
     */
    private static final int MAX_FAIL_ATTEMPTS = 5;
    private int failAttempts = 0;

    /**
     * 上一轮解析失败时，抛出来的异常叫什么、说了什么。
     *
     * 为什么要专门留这个：状态行只写"解析失败"的时候，排查会变成猜谜 ——
     * 到底是 JSON 结构不对（JSONException）、字段类型不对
     * （NumberFormatException / ClassCastException）、还是 base64 坏了？
     * 三者的修法完全不同，而屏上那句话把它们压成了同一个样子。
     *
     * 实测被这个坑过：连着几轮"解析失败(帧5/3954字)"，只能靠翻服务端源码
     * 去猜哪一种，白白绕了很久。把异常短名字 + message 头一段带上，
     * 一眼就能定性。（message 里有换行会打乱状态行，所以要压成一行、截断。）
     */
    private String lastParseError = "";

    /** handleCrops 里读出来的服务端帧号（读到 ≠ 采纳，采纳时机另说） */
    private long _parsedFid = -1;

    /** 把异常压成一行短标签，够定性就行：JSONException: Unterminated object */
    static String shortErr(Throwable t) {
        if (t == null) return "null";
        String n = t.getClass().getSimpleName();
        String m = t.getMessage();
        if (m == null) m = "";
        m = m.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').trim();
        if (m.length() > 60) m = m.substring(0, 60);
        return m.length() == 0 ? n : (n + ": " + m);
    }

    // ------------------------------------------------- 自动发现运行态

    private boolean discovering = false;     // 正在扫网段，防重入
    private boolean autoTriedOnBoot = false; // 启动时只自动试一次
    private boolean firstFetchFailed = false;// 首帧是否失败（决定要不要自动搜）

    // --------------------------------------------------- 音乐模式运行态

    private int imgW = 825, imgH = 1200;     // 面板尺寸（以服务端 /music/crops 的 size 为准）
    private Bitmap fullBmp;
    private long heldFid = -1;
    private boolean fetching = false;
    private int idleRounds = 0;
    private long lastFlashAt = 0;
    private long lastStatusAt = 0;
    private Zone[] zones = new Zone[0];
    private final Paint blitPaint = new Paint();

    /**
     * 服务端下发的"下一句歌词在曲内的时刻"（毫秒）+ 它对应的设备时钟基准。
     *
     * 用途：诊断"字画同步"到底有没有生效。
     *   服务端会把「跨过歌词的那次唤醒」对齐到歌词时间戳本身，
     *   于是下一轮 next_change_ms 会是一个**几十毫秒的小值**（如 70ms）。
     *   这个值小，就说明设备正被安排在歌词那一刻取图 —— 也就是对齐命中了。
     *   这里只做记录与统计，不参与睡眠决策（决策在服务端，保持单一来源）。
     */
    private long lyricNextMs = -1;
    private long lyricNextAtDevice = 0;
    private int leadHits = 0;          // 成功"贴着歌词行"取图的次数（诊断用）

    // 触摸手势（根布局坐标系）
    private float downX, downY;
    private long downT = 0;

    private WifiManager.WifiLock wifiLock;
    private PowerManager.WakeLock wakeLock;

    /** 服务端下发的可点区域（图片坐标系）。 */
    private static class Zone {
        String name = "";
        String action = "";
        int x, y, w, h;

        boolean hit(int px, int py) {
            return px >= x && px < x + w && py >= y && py < y + h;
        }
    }

    // ============================================================= 生命周期

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        handler = new Handler();

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        buildUi();
        loadConfig();
        deriveUrls();

        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.SCREEN_DIM_WAKE_LOCK, "H9Dash:lock");
            wakeLock.acquire();
        }
        WifiManager wm = (WifiManager) getSystemService(Context.WIFI_SERVICE);
        if (wm != null) {
            wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL, "H9Dash:wifi");
            wifiLock.acquire();
        }

        writeScreenInfo();
        probeOnyx();
        lastFlashAt = System.currentTimeMillis();

        Log.i(TAG, "mode=" + mode + " panel=" + active
                + " " + host + ":" + port);
        startPanel();
    }

    /**
     * 起面板。
     *
     * 只启动**一个**轮询循环：每轮先问服务端"现在该看哪个面板"，再按答案取图。
     * 这样电脑侧一切换，设备几秒内就跟上；设备端也没有"dash 一条线、
     * music 一条线"两套并行的逻辑，不会出现两边状态打架。
     *
     * conf.mode 退化成**兜底**：只在服务端完全连不上、又还没完成自动发现时，
     * 用来决定先画哪一版（免得开机一片白）。
     */
    private void startPanel() {
        active = ("music".equals(mode)) ? "music" : "dash";
        panelSeq = -1;          // 强制第一轮当作"面板变了"，重铺底图
        heldFid = -1;
        dashRev = 0;
        tick();
        if (!autoTriedOnBoot) {
            autoTriedOnBoot = true;
            handler.postDelayed(new Runnable() {
                public void run() {
                    // 兜底：这个 boot 探针一旦抛异常，就再也没人叫它第二次，
                    // 而"开机连不上应该转自动发现"这条自救路径就静默失效了。
                    try {
                        if (firstFetchFailed && !discovering) {
                            Log.i(TAG, "首帧失败，转自动发现");
                            autoDiscover(true);
                        }
                    } catch (Throwable t) {
                        Log.w(TAG, "boot autodiscover: " + t);
                    }
                }
            }, 6000);
        }
    }

    /**
     * 统一轮询入口。防重入 + 退避，所有路径最终都回到这里。
     *
     * ★ 整段包兜底，而不是只包 Runnable：这个函数是**心跳本身**，
     *   它是被 handler 直接（或经 sleepThenTick）调起来的。
     *   在这里抛出去的异常会让心跳断掉，而且**没有任何中间层**能接住 ——
     *   现象就是"屏上停在最后一帧、也不打错"，最难查的一种。
     *   兜底时务必把下一跳排上（sleepThenTick），否则吞掉异常等于把循环杀了。
     */
    private void tick() {
        try {
            tickInner();
        } catch (Throwable t) {
            Log.w(TAG, "tick: " + t);
            polling = false;
            fetching = false;
            setStatus("轮询异常，重试中");
            sleepThenTick(POLL_FAIL_MS);
        }
    }

    private void tickInner() {
        if (polling || fetching) return;
        if (new File(sdcard(), STOP).exists()) {
            setStatus("检测到 h9dash.stop，退出");
            handler.postDelayed(new Runnable() {
                public void run() {
                    try { finish(); }
                    catch (Throwable t) { Log.w(TAG, "finish: " + t); }
                }
            }, 1500);
            return;
        }
        polling = true;
        new Thread(new Runnable() {
            public void run() {
                // 1) 问面板归属（免 token，最轻的一个请求）
                String pb = fetchText(panelUrl);
                String want = null;
                long seq = -1;
                if (pb != null && pb.length() > 0) {
                    try {
                        JSONObject o = new JSONObject(pb);
                        want = o.optString("panel", "");
                        seq = o.optLong("seq", -1);
                    } catch (Throwable t) {
                        Log.w(TAG, "panel parse: " + t);
                    }
                }
                final String fWant = want;
                final long fSeq = seq;
                handler.post(new Runnable() {
                    public void run() {
                        polling = false;
                        // ★ 这是整个轮询循环的**主心跳**，兜底最重要：
                        //   这里一抛异常，tickRunnable 就再也不会被排上，
                        //   轮询静默死亡 —— 屏上停在最后一帧，且不打任何错。
                        //   宁可这一轮不换面板，也必须把下一跳排上。
                        try {
                            if (fWant == null || fWant.length() == 0) {
                                // 问不到面板（多半是地址过期）→ 保持现状，退回本地兜底
                                pollFails++;
                                if (pollFails == 3 && !discovering) {
                                    Log.i(TAG, "面板轮询连续失败，转自动发现");
                                    autoDiscover(false);
                                }
                                if (pollFails >= 3) {
                                    setStatus("连不上电脑，检查服务 / WiFi");
                                }
                                handler.removeCallbacks(tickRunnable);
                                handler.postDelayed(tickRunnable, POLL_FAIL_MS);
                                return;
                            }
                            pollFails = 0;
                            boolean switched = !fWant.equals(active) || fSeq != panelSeq;
                            if (switched) {
                                active = fWant;
                                panelSeq = fSeq;
                                // 换面板 → 底图作废。清三项缺一不可：
                                //   heldFid   —— 旧面板的帧号喂给新端点毫无意义
                                //   dashRev   —— 看板内容版本也重来
                                //   fullBmp   —— 旧底图必须先回收，否则 3.78 MB 白占着
                                heldFid = -1;
                                dashRev = 0;
                                recycleFullBmp();
                                Log.i(TAG, "面板切到 " + active + " (seq=" + fSeq + ")");
                            }
                            fetchPanel(switched);
                        } catch (Throwable t) {
                            Log.w(TAG, "panel tick: " + t);
                            setStatus("轮询出错，重试中");
                            sleepThenTick(POLL_FAIL_MS);
                        }
                    }
                });
            }
        }).start();
    }
    private Runnable tickRunnable = new Runnable() {
        public void run() {
            tick();
        }
    };

    private void sleepThenTick(long ms) {
        handler.removeCallbacks(tickRunnable);
        handler.postDelayed(tickRunnable, ms);
    }

    /**
     * ★★ 把一张解码出来的位图变成"可以被 Canvas 画上去"的底图。
     *
     * 为什么必须这么干 —— 这是"解析失败"整整三轮的真凶：
     *
     *   BitmapFactory.decodeByteArray 解出来的位图，在 Android 上**默认是
     *   immutable 的**（不可变）。而 Canvas 的构造器有一个硬性前置条件：
     *   「传入的位图必须 mutable」，否则直接抛
     *
     *       IllegalStateException: Immutable bitmap passed to Canvas constructor
     *
     *   贴脏块的代码是 `new Canvas(fullBmp)` —— 所以只要 fullBmp 来自
     *   decodeByteArray，那一行**必然**抛，100% 复现，跟网络、跟数据、
     *   跟内容一点关系都没有。（这也解释了为什么服务端怎么验都是好的。）
     *
     * 为什么以前偶尔能"成功一次"：抛出去之后 heldFid 被作废，下一轮走整帧；
     * 整帧 path 里 `show(bmp,false)` 直接把 immutable 位图交给 ImageView 也能显示 ——
     * 于是那一轮屏上**看起来**刷出了一帧新的整屏，人以为"成功了"。
     * 但下一轮再要脏块，又撞同一堵墙。所以看到的就是"大部分时间在重试，
     * 偶尔成功一次，20 秒才动一下"。
     *
     * 修法：copy(config, true) 复制一份 mutable 的。多花一张图的内存
     * （825x1200 ARGB = 3.78 MB），但这个代价是**必须**付的 —— 不付就一张脏块都贴不上。
     * 复制失败（OOM）就退回原图：退回后至少整帧还能显示，不会更差。
     *
     * @param src 解码得到的位图（通常 immutable）
     * @param needWritable 调用方是否需要往它上面画（贴脏块）→ 看板只要显示就传 false
     */
    private Bitmap asWritable(Bitmap src, boolean needWritable) {
        if (src == null) return null;
        if (!needWritable) return src;
        // 已经是可写的就白拿（decodeByteArray 带 inMutable 时会出现）
        if (src.isMutable()) return src;
        Bitmap copy = null;
        try {
            copy = src.copy(src.getConfig() == null
                    ? Bitmap.Config.ARGB_8888 : src.getConfig(), true);
        } catch (Throwable t) {                 // OOM 也走这儿
            Log.w(TAG, "asWritable copy threw: " + t);
            copy = null;
        }
        if (copy == null) {
            // 复制不出来 —— 退回原图。屏上字照旧能看，只是这一轮贴不上脏块，
            // 下一轮还会再来。绝不在这里把整帧丢掉。
            Log.w(TAG, "asWritable fallback to immutable (" + src.getWidth()
                    + "x" + src.getHeight() + ")");
            setStatus("内存吃紧，退回只读底图");
            return src;
        }
        try { src.recycle(); }
        catch (Throwable t) { Log.w(TAG, "asWritable recycle src: " + t); }
        return copy;
    }

    /** 丢掉底图。回收失败也吞掉 —— recycle 在已回收的位图上会抛。 */
    private void recycleFullBmp() {
        Bitmap b = fullBmp;
        fullBmp = null;
        if (b != null) {
            try {
                b.recycle();
            } catch (Throwable t) {
                Log.w(TAG, "recycle fullBmp: " + t);
            }
        }
    }

    /**
     * 看板整帧就位：换底图 → 更新尺寸 → 清分区 → 上屏。
     *
     * 这一步以前是内联在 fetchDash 里的。抽出来的原因不在于"好看"，
     * 而在于**换底图是全局状态**：歌词面板的脏块也是往 fullBmp 上贴的，
     * 两个面板的替换路径必须走同一个出口，否则迟早有一边忘了清 heldFid /
     * 忘了回收旧图 —— 那正是"闪退"和"贴到旧底图上"的来源。
     */
    private void swapInDashFrame(Bitmap bmp) {
        // ★★ 这里是**两个面板替换底图的唯一出口**，可写化必须在这一层做 ——
        //    歌词面板的脏块也是往同一张 fullBmp 上贴的，所以哪怕看板自己不画，
        //    切回音乐面板后这张底图也得能被 Canvas 用。放这儿一次覆盖两条路。
        //    不这么做的后果见 asWritable 的长注释：每条脏块都抛
        //    IllegalStateException，屏上只剩整帧偶尔刷一下。
        bmp = asWritable(bmp, true);
        Bitmap old = fullBmp;
        fullBmp = bmp;
        if (old != null && old != bmp) {
            try { old.recycle(); }
            catch (Throwable t) { Log.w(TAG, "swapInDashFrame recycle old: " + t); }
        }
        heldFid = -1;      // 看板没有帧号概念；回到歌词面板时让它重新拿整帧
        imgW = bmp.getWidth();
        imgH = bmp.getHeight();
        clearZones();      // 看板没有触摸分区
        show(bmp, doFlash);
    }

    // ======================================================= 按面板取图

    /**
     * 按当前 active 分派到看板或歌词面板；完事自己安排下一轮。
     *
     * 兜底是刚需：fetchDash / fetchMusic 里只保证"Runnable 体不炸"，
     * 但**起点那一句**（拼 URL、起线程）如果抛了，就没有任何人来安排下一轮 ——
     * 轮询会静悄悄地停在这儿，屏上永远停在最后一帧，看着就是"定格"。
     */
    private void fetchPanel(final boolean switched) {
        try {
            if ("music".equals(active)) {
                fetchMusic();
            } else {
                fetchDash(switched);
            }
        } catch (Throwable t) {
            Log.w(TAG, "fetchPanel: " + t);
            fetching = false;
            setStatus("调度出错，重试中");
            sleepThenTick(POLL_FAIL_MS);
        }
    }

    /**
     * 看板：带 rev 变更检测。
     *
     * 手里有 rev=N 就带上，服务端内容没变时只回 {"unchanged":true}，
     * 设备据此**不刷屏**（每次刷屏都要闪一下，白闪是没法忍的）。
     */
    private void fetchDash(final boolean switched) {
        final String u = dashUrl + (dashUrl.indexOf('?') >= 0 ? "&" : "?") + "rev=" + dashRev;
        fetching = true;
        new Thread(new Runnable() {
            public void run() {
                lastFetchIncomplete = false;
                final byte[] data = fetchBytes(u);
                final boolean netCut = lastFetchIncomplete;
                handler.post(new Runnable() {
                    public void run() {
                        fetching = false;
                        long delay = POLL_MS;
                        // ★ 这个 Runnable 跑在 UI 线程上：任何漏出去的异常 = 直接闪退。
                        //   整帧是 825x1200 ARGB_8888 = 3.78 MB，decode 和 rotate 都可能
                        //   抛 OutOfMemoryError，recycle 在已回收的位图上也会抛。
                        //   所以整段套一层兜底，宁可这轮不刷新也不能把 App 弄挂。
                        try {
                        if (data == null || data.length == 0) {
                            firstFetchFailed = true;
                            setStatus(netCut ? "网络断了，重试中"
                                             : "获取失败，检查 PC 服务 / WiFi");
                            delay = POLL_FAIL_MS;
                        } else if (data.length > 2 && data[0] == '{') {
                            // {"unchanged": true, "rev": N} —— 内容没变，别刷屏
                            try {
                                JSONObject o = new JSONObject(new String(data, "UTF-8"));
                                if (o.optBoolean("unchanged", false)) {
                                    dashRev = o.optLong("rev", dashRev);
                                    setStatus("在线 " + now() + " · 无变化");
                                    sleepThenTick(delay);
                                    return;
                                }
                            } catch (Throwable t) {
                                Log.w(TAG, "dash json: " + t);
                            }
                            setStatus("响应异常，重试中");
                            delay = POLL_FAIL_MS;
                        } else {
                            Bitmap bmp = null;
                            try {
                                bmp = BitmapFactory.decodeByteArray(data, 0, data.length);
                            } catch (Throwable t) {          // OOM 也走这儿
                                Log.w(TAG, "dash decode threw: " + t);
                                bmp = null;
                            }
                            if (bmp == null) {
                                setStatus("图片解码失败");
                                delay = POLL_FAIL_MS;
                            } else if (rotate != 0) {
                                Matrix m = new Matrix();
                                m.postRotate(rotate);
                                Bitmap r = null;
                                try {
                                    r = Bitmap.createBitmap(bmp, 0, 0,
                                            bmp.getWidth(), bmp.getHeight(), m, true);
                                } catch (Throwable t) {
                                    Log.w(TAG, "dash rotate threw: " + t);
                                    r = null;
                                }
                                if (r != null && r != bmp) {
                                    try { bmp.recycle(); }
                                    catch (Throwable t) { Log.w(TAG, "dash recycle: " + t); }
                                    bmp = r;
                                }
                            }
                            if (bmp != null) {
                                swapInDashFrame(bmp);
                                lastFlashAt = System.currentTimeMillis();
                                setStatus("看板更新 " + now());
                                // 服务端把新版本号写在响应头里（整帧和 unchanged 两条路都带），
                                // 取一次即可，下次轮询用它来判断要不要刷。
                                if (dashRevHeader > 0) dashRev = dashRevHeader;
                                dashRevHeader = 0;
                            }
                        }
                        } catch (Throwable t) {
                            Log.w(TAG, "dash handler: " + t);
                            setStatus("界面出错，重试中");
                            delay = POLL_FAIL_MS;
                        }
                        sleepThenTick(delay);
                    }
                });
            }
        }).start();
    }

    /**
     * 歌词面板：拿脏块 → 贴图 → 按服务端说的"下次变化时间"睡。
     * 关键：**每轮都回传自己手上的帧号**，否则永远只能收整帧。
     */
    private void fetchMusic() {
        final String u = cropsUrl + (cropsUrl.indexOf('?') >= 0 ? "&" : "?") + "from=" + heldFid;
        fetching = true;
        final int reqFid = (int) heldFid;   // 记下"这一轮我是拿着哪一帧去问的"
        new Thread(new Runnable() {
            public void run() {
                lastFetchIncomplete = false;
                final String body = fetchText(u);
                final boolean netCut = lastFetchIncomplete;
                handler.post(new Runnable() {
                    public void run() {
                        fetching = false;
                        long delay = 1500;
                        boolean bad = false;
                        try {
                            if (body == null) {
                                // 根本没拿到完整响应（连不上/超时/断流）。
                                // 这跟"拿到了但解析不了"是两码事，说法必须分开，
                                // 否则人会去查服务端，而问题其实在链路。
                                setStatus(netCut ? "网络断了，重试中" : "取不到画面，重试中");
                                delay = 3000;
                                bad = true;
                            } else {
                                try {
                                    delay = handleCrops(body);
                                    lastParseError = "";
                                } catch (Throwable t) {
                                    Log.w(TAG, "handleCrops: " + t);
                                    // ★ 必须带上"哪一步 + 异常名 + message 头一段"。
                                    //   只写"解析失败"会把 JSONException /
                                    //   NumberFormatException / ClassCastException
                                    //   三件完全不同的事压成同一个样子，
                                    //   排查只能靠翻源码猜 —— 实测被这个坑过一整轮。
                                    lastParseError = shortErr(t);
                                    setStatus("解析失败(帧" + reqFid + "/"
                                              + body.length() + "字) "
                                              + lastParseError);
                                    delay = 3000;
                                    bad = true;
                                    // ★★ 把**原始 body** 落盘一份。
                                    //    光有异常名还不够 —— 要确认到底是 JSON 语法
                                    //    坏了、字段类型不对、还是 base64 里混了东西，
                                    //    必须看真正的字节。取最前面一段就够定性了，
                                    //    而且只保留最近几次，别把存储写满。
                                    dumpBadBody(reqFid, body, lastParseError);
                                    // ★ 解析一旦出岔子，手里的帧号就不可信了（底图可能
                                    //   只贴了一半）。立刻作废，下一轮拿整帧。
                                    //   不等 failAttempts 攒够 —— 那样会白白多失败几轮，
                                    //   而每一轮都在往堆里塞临时位图，正是闪退的温床。
                                    heldFid = -1;
                                }
                            }
                            if (bad) {
                                failAttempts++;
                                if (failAttempts >= MAX_FAIL_ATTEMPTS) {
                                    // 连坏这么多轮，说明拿在手里的帧号已经不可信了。
                                    // 作废它 → 下一轮 from=-1 整帧重来（脏块救不回来）。
                                    Log.w(TAG, "give up fid " + heldFid + " after "
                                            + failAttempts + " failures");
                                    heldFid = -1;
                                    failAttempts = 0;
                                    recycleFullBmp();
                                }
                            } else {
                                // 注意：handleCrops 里若判定"有坏块"会把 heldFid 设成 -1
                                // 但它**不算 bad**（它自己已经安排了整帧重试），
                                // 所以这里不能无脑清零 failAttempts。
                                failAttempts = 0;
                            }
                        } catch (Throwable t) {
                            // ★ 最后一道防线：这个 Runnable 跑在 UI 线程上，
                            //   任何漏出去的异常都会让 App 直接闪退。
                            //   宁可这一轮什么都不做，也不能把整个程序带走。
                            Log.w(TAG, "tick handler: " + t);
                        }
                        // 面板归属仍要定期确认（电脑可能随时切走），
                        // 所以不能睡满服务端给的时间，取个上限。
                        sleepThenTick(clampDelay(delay));
                    }
                });
            }
        }).start();
    }

    private void buildUi() {
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.WHITE);
        rootView = root;

        imageView = new ImageView(this);
        imageView.setScaleType(ImageView.ScaleType.FIT_CENTER);
        root.addView(imageView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        flashView = new View(this);
        flashView.setBackgroundColor(Color.WHITE);
        flashView.setVisibility(View.GONE);
        root.addView(flashView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        statusView = new TextView(this);
        statusView.setTextColor(Color.DKGRAY);
        statusView.setTextSize(11f);
        statusView.setBackgroundColor(Color.WHITE);
        statusView.setPadding(6, 3, 6, 3);
        statusView.setGravity(Gravity.LEFT | Gravity.BOTTOM);
        statusView.setLongClickable(false);
        FrameLayout.LayoutParams sp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        sp.gravity = Gravity.BOTTOM;
        root.addView(statusView, sp);

        // 触摸监听挂在根布局上：子控件（ImageView / TextView）都不消费事件，
        // 会一路冒泡到这里，命中判断统一做，不受 FIT_CENTER 留白影响。
        root.setClickable(true);
        root.setOnTouchListener(new View.OnTouchListener() {
            public boolean onTouch(View v, MotionEvent e) {
                switch (e.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        downX = e.getX();
                        downY = e.getY();
                        downT = System.currentTimeMillis();
                        return true;
                    case MotionEvent.ACTION_UP:
                        if (System.currentTimeMillis() - downT >= LONG_PRESS_MS) {
                            // 长按：出菜单（换地址 / 切面板）
                            float ex = e.getX(), ey = e.getY();
                            float ddx = ex - downX, ddy = ey - downY;
                            if (ddx * ddx + ddy * ddy < 60 * 60) {
                                showMenu();
                            }
                        } else {
                            onTap(e.getX(), e.getY());
                        }
                        return true;
                    default:
                        return false;
                }
            }
        });

        setContentView(root);
    }

    // ============================================================= 配置读写

    private File sdcard() {
        return NetConfig.sdcard();
    }

    /**
     * 把 NetConfig 里的值搬进实例字段，并同步 UI（方向、缩放、状态条）。
     * 无论是启动时读盘，还是自动发现成功后改配置，都走这一个入口。
     */
    private void applyConfig(NetConfig c) {
        conf = c;
        host = c.host;
        port = c.port;
        token = c.token;
        mode = c.mode;
        intervalMin = c.intervalMin;
        flashMin = c.flashMin;
        rotate = c.rotate;
        scale = c.scale;
        keyPrev = c.keyPrev;
        keyNext = c.keyNext;
        showStatus = c.showStatus;
        doFlash = c.doFlash;
        keylog = c.keylog;

        if ("landscape".equals(c.orientation)) {
            setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE);
        } else if ("auto".equals(c.orientation)) {
            setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_SENSOR);
        } else {
            setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_PORTRAIT);
        }

        if ("crop".equals(scale)) {
            imageView.setScaleType(ImageView.ScaleType.CENTER_CROP);
        } else if ("stretch".equals(scale)) {
            imageView.setScaleType(ImageView.ScaleType.FIT_XY);
        } else {
            imageView.setScaleType(ImageView.ScaleType.FIT_CENTER);
        }

        statusView.setVisibility(showStatus ? View.VISIBLE : View.GONE);
    }

    private void loadConfig() {
        applyConfig(NetConfig.load());
        Log.i(TAG, "conf loaded: " + host + ":" + port + " mode=" + mode
                + " token=" + (token.length() > 8 ? token.substring(0, 8) : token));
    }

    /**
     * 自动发现 PC 服务并在需要时改写配置。
     *
     * 触发时机：启动时（若现有地址已经不通）、以及用户长按选择"重新搜索"。
     *
     * @param thenRefresh 找到之后是否立刻拉一次画面
     */
    private void autoDiscover(final boolean thenRefresh) {
        if (discovering) return;
        discovering = true;
        setStatus("正在搜索电脑 ...");

        new Thread(new Runnable() {
            public void run() {
                String note;
                boolean ok = false;
                try {
                    AutoDiscover.Result r = AutoDiscover.resolve(conf.port);
                    // 找到了：把地址落盘，以后重启也不用再搜
                    conf.host = r.host;
                    if (r.token.length() > 0) conf.token = r.token;
                    conf.save();

                    final NetConfig newCfg = conf;
                    handler.post(new Runnable() {
                        public void run() {
                            try {
                                applyConfig(newCfg);
                                deriveUrls();
                            } catch (Throwable t) {
                                Log.w(TAG, "applyConfig: " + t);
                            }
                        }
                    });
                    note = "找到 " + r.host + ":" + r.port
                            + " 用时 " + r.elapsedMs + "ms"
                            + (r.token.length() > 0 ? " 拿到 token" : " 未拿到 token");
                    ok = true;
                } catch (AutoDiscover.NotFound e) {
                    note = e.getMessage();
                } catch (Throwable t) {
                    note = "搜索出错：" + t;
                }

                writeAutoLog(note);
                final String finalNote = note;
                final boolean finalOk = ok;
                handler.post(new Runnable() {
                    public void run() {
                        discovering = false;
                        setStatus(finalOk ? ("已连上 " + host) : ("未找到：" + finalNote));
                        try {
                            if (thenRefresh || finalOk) {
                                // 换了服务端：底图和帧号全部作废，当作刚换面板重来
                                panelSeq = -1;
                                dashRev = 0;
                                heldFid = -1;
                                pollFails = 0;
                                firstFetchFailed = false;
                                recycleFullBmp();
                                handler.removeCallbacks(tickRunnable);
                                tick();
                            }
                        } catch (Throwable t) {
                            Log.w(TAG, "autodiscover done: " + t);
                            // 兜底也要把心跳接上，否则发现流程一炸轮询就永久停了
                            sleepThenTick(POLL_FAIL_MS);
                        }
                    }
                });
            }
        }).start();
    }

    private void writeAutoLog(String note) {
        try {
            String line = new SimpleDateFormat("MM-dd HH:mm:ss", Locale.CHINA).format(new Date())
                    + "  " + note + "\n";
            File f = new File(sdcard(), AUTO_TXT);
            if (f.exists() && f.length() > 32768) f.delete();
            FileOutputStream fos = new FileOutputStream(f, true);
            fos.write(line.getBytes("UTF-8"));
            fos.flush();
            fos.close();
        } catch (Throwable t) {
            Log.w(TAG, "auto log: " + t);
        }
    }

    /**
     * 解析失败时，把原始 body 落盘一份（只留最近的，别写满存储）。
     *
     * 为什么非要留原文：异常名只告诉我们"哪一类"，但同样的 JSONException
     * 可能是"少了个括号"、"第 3000 个字符处有非法字节"、"字符串没闭合"……
     * 不看到真字节就只能在源码里猜。设备上取文件很方便（SD 卡直接能读），
     * 所以这是最划算的一招。
     *
     * 存的位置：h9dash_bad.txt（和 h9dash_screen.txt 那些诊断文件放一起）。
     * 每次覆盖写最近 3 条，不做追加 —— 避免"坏了 500 次"把文件写爆。
     */
    private void dumpBadBody(long fid, String body, String err) {
        try {
            String line = new SimpleDateFormat("MM-dd HH:mm:ss", Locale.CHINA)
                    .format(new Date())
                    + "  fid=" + fid + " len=" + body.length() + "  " + err + "\n"
                    + "  HEAD: " + body.substring(0, Math.min(600, body.length())) + "\n"
                    + "  TAIL: " + (body.length() > 600
                            ? body.substring(Math.max(0, body.length() - 300)) : "(同上)")
                    + "\n\n";
            String old = "";
            File f = new File(sdcard(), BAD_TXT);
            if (f.exists() && f.length() > 0) {
                // 只保留最近 3 条（按空行分块，多余的扔掉）
                byte[] b = new byte[(int) Math.min(f.length(), 65536)];
                FileInputStream in = new FileInputStream(f);
                try {
                    int n = in.read(b);
                    if (n > 0) old = new String(b, 0, n, "UTF-8");
                } finally {
                    close(in);
                }
                String[] parts = old.split("\n\n");
                if (parts.length > 3) {
                    StringBuilder sb = new StringBuilder();
                    for (int i = parts.length - 3; i < parts.length; i++) {
                        sb.append(parts[i]).append("\n\n");
                    }
                    old = sb.toString();
                }
            }
            writeFile(f, old + line);
        } catch (Throwable t) {
            Log.w(TAG, "dump bad body: " + t);
        }
    }

    /**
     * 长按屏幕弹出的简易菜单：重新搜索 / 切面板 / 强制重画。
     *
     * 切面板走的是 POST /panel，改的是**服务端**那份状态 —— 所以电脑侧
     * 看到的立刻同步。以前这里是改本地 dashboard.conf 再重启自己，
     * 结果是"平板切了、电脑不知道"，两边各说各话。
     */
    private void showMenu() {
        final boolean toMusic = !"music".equals(active);
        final String[] items = {
                "重新搜索电脑",
                "切到" + (toMusic ? "歌词面板" : "信息看板"),
                "强制重画",
                "查看当前地址",
        };
        new AlertDialog.Builder(this)
                .setTitle("H9Dash")
                .setItems(items, new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int which) {
                        switch (which) {
                            case 0:
                                autoDiscover(true);
                                break;
                            case 1:
                                sendCommand("panel_" + (toMusic ? "music" : "dash"));
                                setStatus("正在切到" + (toMusic ? "歌词面板" : "信息看板") + " ...");
                                // 服务端状态变了，下一轮 tick 会自己发现；这里提前催一下
                                handler.postDelayed(new Runnable() {
                                    public void run() {
                                        // tick() 是整条轮询循环的入口。
                                        // 它一抛异常，心跳就再也没人接上 ——
                                        // 屏上停在最后一帧，静默死亡。
                                        try {
                                            tick();
                                        } catch (Throwable t) {
                                            Log.w(TAG, "kick tick: " + t);
                                            sleepThenTick(POLL_FAIL_MS);
                                        }
                                    }
                                }, 700);
                                break;
                            case 2:
                                heldFid = -1;
                                dashRev = -1;    // 用一个不可能的值逼服务端回整图
                                handler.removeCallbacks(tickRunnable);
                                tick();
                                break;
                            default:
                                setStatus(host + ":" + port + "  面板=" + active
                                        + "  token=" + (token.length() > 10 ? token.substring(0, 10) + "..." : token));
                                break;
                        }
                    }
                })
                .show();
    }

    private static int parseInt(String s, int def) {
        try {
            return Integer.parseInt(s.trim());
        } catch (Throwable t) {
            return def;
        }
    }

    /**
     * 从 host/port/token 推出全部端点地址。
     *
     * 一律从 host/port 现算，而不是从 url 字符串里切 —— 面板是服务端决定的，
     * 设备的 url 里已经固定不了"该拉哪张图"了，留着那条路径只会误导。
     *
     *   /panel        免 token   "现在该看哪个面板"
     *   /dash.png     token      看板（带 rev 变更检测）
     *   /music/crops  token      歌词面板脏块
     *   /cmd          token      指令 / 面板操作（POST）
     */
    private void deriveUrls() {
        String base = "http://" + host + ":" + port;
        String auth = (token.length() > 0) ? ("?t=" + token) : "";
        panelUrl = base + "/panel";
        dashUrl = base + "/dash.png" + auth;
        cropsUrl = base + "/music/crops" + auth;
        cmdUrl = base + "/cmd" + auth;
        Log.i(TAG, "panel=" + panelUrl + " dash=" + dashUrl + " crops=" + cropsUrl);
    }

    private void writeScreenInfo() {
        try {
            DisplayMetrics dm = new DisplayMetrics();
            getWindowManager().getDefaultDisplay().getMetrics(dm);
            String s = dm.widthPixels + "x" + dm.heightPixels + " dpi=" + dm.densityDpi
                    + " mode=" + mode;
            writeFile(new File(sdcard(), SCREEN_TXT), s);
            Log.i(TAG, "screen " + s);
        } catch (Throwable t) {
            Log.w(TAG, "screen info: " + t);
        }
    }

    /**
     * 探一下设备有没有文石的 EPD 接口，把结果写到 SD 卡。
     *
     * 只探测、不调用。原因：EpdController 的 UpdateMode 是一堆魔数，
     * 不同 ROM 上含义不同，猜错了会把整屏刷成灰或者留一堆残影。
     * 没有真机能验证之前，宁可不动它 —— 靠"不闪屏 + 定时整屏刷"已经够用。
     */
    private void probeOnyx() {
        StringBuilder sb = new StringBuilder();
        String[] classes = {
                "com.onyx.android.sdk.api.device.epd.EpdController",
                "com.onyx.android.sdk.api.device.epd.UpdateMode",
                "com.onyx.android.sdk.api.device.epd.UpdateOption",
                "com.onyx.android.sdk.api.device.DeviceEnvironment",
        };
        for (String c : classes) {
            try {
                Class.forName(c);
                sb.append("FOUND  ").append(c).append("\n");
            } catch (Throwable t) {
                sb.append("absent ").append(c).append("\n");
            }
        }
        sb.append("\n结论：有 FOUND 也只做记录，APK 不调用其接口。\n");
        writeFile(new File(sdcard(), ONYX_TXT), sb.toString());
        Log.i(TAG, "onyx probe:\n" + sb);
    }

    /**
     * 夹一下服务端给的休眠时长。
     *
     * ⚠️ 两处曾经把"字画同步"毁掉，改动前务必读完：
     *
     *  1) 下限曾经是 400ms。服务端在对齐歌词时会故意回一个很小的值
     *     （50ms = "立刻去取，现在这一帧就是那一句"）。被夹到 400ms 之后，
     *     这个信号就废了 —— 每句歌词都平白晚 350ms。
     *     下限只要保住"别变成 0/负数把 postDelayed 打成忙循环"就够了，
     *     用 40ms（比一次 HTTP 往返短得多，不会造成自旋）。
     *
     *  2) 退避倍数。服务端现在给的间隔本身就带着意图（该醒了才叫醒），
     *     再乘 2/3 会让它错过歌词行。所以只对"没内容变化"的轮次做**轻微**退避，
     *     而且设了上限 —— 真正的省电靠服务端给大间隔，不靠这里乱乘。
     */
    private long clampDelay(long delay) {
        if (idleRounds > 10) delay = delay * 2;
        if (delay < 40) delay = 40;
        if (delay > 15000) delay = 15000;
        return delay;
    }

    /**
     * 处理 /music/crops 的响应，返回"下次该睡多久"。
     *
     * 响应形态：
     *   {"frame_id":N, "full":true,  "png":"<base64 整帧>", "zones":[...], "next_change_ms":..}
     *   {"frame_id":N, "full":false, "rects":[{"x","y","w","h","png"}...], "zones":[...]}
     *   {"frame_id":N, "up_to_date":true, "rects":[]}
     */
    private long handleCrops(String body) throws Exception {
        if (body == null || body.length() == 0) {
            firstFetchFailed = true;
            setStatus("连接失败，检查 PC 服务 / WiFi");
            return 3000;
        }
        // ★ 给解析的每一步打上"罪名"，抛出去时上层就能说出是哪一步坏的。
        //   不这么做的话，状态行只剩"解析失败"四个字 —— 而 JSON 结构错、
        //   字段类型错、base64 坏，这三件事的修法完全不同。
        int step = 0;
        JSONObject o;
        try {
            step = 1;
            o = new JSONObject(body);
            step = 2;
            long fid0 = o.optLong("frame_id", -1);
            step = 3;
            // 立刻把 fid 读出来，但**先存着不采纳**（采纳时机见下面的长注释）
            this._parsedFid = fid0;
        } catch (Exception e) {
            throw new Exception("第" + step + "步 " + shortErr(e));
        }
        long fid = this._parsedFid;
        long next = o.optLong("next_change_ms", 1500);

        // 记下"下一句歌词"的信息：服务端已经把它换算成"还差多少毫秒"了
        // （next_change_ms 就含这个），这里再存一份原始时刻，用于诊断
        // "这一轮是不是为了对齐歌词而醒的"。
        lyricNextMs = o.optLong("lyric_next_ms", -1);
        lyricNextAtDevice = System.currentTimeMillis();
        if (next <= 120) leadHits++;      // 几毫秒级的小值 = 正落在歌词那一刻取图

        try {
            step = 4;
            parseZones(o.optJSONArray("zones"));
            step = 5;
            JSONObject size = o.optJSONObject("size");
            if (size != null) {
                imgW = size.optInt("w", imgW);
                imgH = size.optInt("h", imgH);
            }
        } catch (Exception e) {
            throw new Exception("第" + step + "步 " + shortErr(e));
        }

        boolean upToDate = o.optBoolean("up_to_date", false);
        boolean full = o.optBoolean("full", false);

        if (upToDate) {
            // ⚠️ 顺序很要紧：帧号必须在**确认这轮没有任何异常**之后再采纳。
            // 见下面 rects 分支前的长注释 —— 提前采纳会让一次失败变成永久失败。
            if (fid >= 0) heldFid = fid;
            idleRounds++;
            maybePeriodicFullFlash();
            // 状态行只在没变的时候低频更新，避免那块小条一直闪
            long nowMs = System.currentTimeMillis();
            if (nowMs - lastStatusAt > 60000) {
                lastStatusAt = nowMs;
                setStatus("在线 " + now() + " · 无变化");
            }
            return next;
        }

        if (full) {
            String b64 = o.optString("png", "");
            byte[] raw = null;
            Bitmap bmp = null;
            try {
                raw = Base64.decode(b64, Base64.DEFAULT);
                bmp = BitmapFactory.decodeByteArray(raw, 0, raw.length);
            } catch (Throwable t) {
                // OOM 也走这儿 —— 绝不让它冒泡上去，否则整个 App 会挂掉
                Log.w(TAG, "full frame decode threw: " + t);
                bmp = null;
            }
            if (bmp == null) {
                // 整帧没解出来 —— 这是最要紧的一路：屏上现在什么都没有/还是旧帧，
                // 必须马上要一帧新的，不能就这么停在这。
                Log.w(TAG, "full frame decode failed, b64=" + b64.length()
                        + " raw=" + (raw == null ? -1 : raw.length));
                heldFid = -1;              // 逼下一轮重新整帧（脏块路径没有底图可用）
                setStatus("整帧解码失败，重试中");
                return 1500;
            }
            // ★★ 整帧这条路拿到的底图必须是可写的（下一轮的脏块要往它上面贴）。
            //    这里和 swapInDashFrame 都调了 asWritable —— 看着冗余，其实是两条
            //    独立的赋值路径（音乐整帧不走 swapInDashFrame）；asWritable 里
            //    有 isMutable() 短路，已经是可写的时候零开销，不会重复复制。
            bmp = asWritable(bmp, true);
            Bitmap old = fullBmp;
            fullBmp = bmp;
            if (old != null && old != bmp) {
                try { old.recycle(); }
                catch (Throwable t) { Log.w(TAG, "recycle old full: " + t); }
            }
            idleRounds = 0;
            show(bmp, false);              // ★ 不闪屏
            setStatus("整屏 " + now() + "  " + bmp.getWidth() + "x" + bmp.getHeight());
            lastStatusAt = System.currentTimeMillis();
            lastFlashAt = System.currentTimeMillis();   // 刚整屏刷过，重新计时
            if (fid >= 0) heldFid = fid;
            return next;
        }

        // ---- 脏块：贴到现有底图上
        JSONArray rects = o.optJSONArray("rects");
        if (fullBmp == null) {
            // 底图还没建立（不该发生）→ 下一轮强制整帧
            heldFid = -1;
            setStatus("等待整帧 ...");
            return 600;
        }
        int applied = 0;
        int badTiles = 0;                  // 解不开的块数 —— 决定要不要重来一整帧
        int nRects = (rects == null ? 0 : rects.length());
        if (rects != null && nRects > 0) {
            Canvas c = new Canvas(fullBmp);
            for (int i = 0; i < nRects; i++) {
                JSONObject r = rects.optJSONObject(i);
                if (r == null) { badTiles++; continue; }
                String b64 = r.optString("png", "");
                if (b64.length() == 0) continue;
                byte[] raw = null;
                Bitmap tile = null;
                try {
                    raw = Base64.decode(b64, Base64.DEFAULT);
                    tile = BitmapFactory.decodeByteArray(raw, 0, raw.length);
                } catch (Throwable t) {
                    // ★ 单块解码失败（含 OOM）必须就地吞掉。
                    //   让它冒泡出去的后果：heldFid 已经被上面的服务端帧号改过，
                    //   而底图一块都没贴上 → 下一轮还要 N+1 的脏块 → 永远贴不上
                    //   → 闪退前会一直"解析失败"。这一次失败绝不能升级成永久失败。
                    Log.w(TAG, "tile " + i + " decode threw: " + t);
                    tile = null;
                }
                if (tile == null) { badTiles++; continue; }
                try {
                    c.drawBitmap(tile, (float) r.optInt("x"), (float) r.optInt("y"), blitPaint);
                    applied++;
                } catch (Throwable t) {
                    Log.w(TAG, "tile " + i + " blit threw: " + t);
                    badTiles++;
                } finally {
                    tile.recycle();
                }
            }
            if (applied > 0) {
                imageView.invalidate();    // 只让系统重画，不重建位图
            }
        }
        if (badTiles > 0) {
            // 有块没贴上 —— 说明屏上现在既有新内容也有旧内容，不一致。
            // 光等下一轮脏块是修不回来的（服务端不会再发同样的块），
            // 必须主动要一整帧把底图抹平，否则就会"定格"在这种半新半旧的样子。
            //
            // ★★ 关键：**绝不在这里采纳服务端的帧号**。
            //    这一轮底图是残缺的，若把 heldFid 更新成 N，下一轮就会要 N+1 的脏块，
            //    贴到一张本来就错的底图上 —— 错上加错，且**再也没有整帧的机会**。
            //    保持 heldFid 不变（或直接作废），让下一轮走 from=-1 拿整帧。
            Log.w(TAG, "bad tiles " + badTiles + "/" + nRects
                    + " (fid=" + fid + ", held=" + heldFid + ")");
            heldFid = -1;
            idleRounds = 0;
            setStatus("局部图坏 " + badTiles + "/" + nRects + " 块，重新整帧");
            return 800;
        }
        // 全部贴上、无异常 —— 现在才可以放心采纳服务端的帧号
        if (fid >= 0) heldFid = fid;
        idleRounds = 0;
        maybePeriodicFullFlash();
        if (applied > 0) {
            setStatus("局部更新 " + applied + " 块  " + now());
            lastStatusAt = System.currentTimeMillis();
        }
        return next;
    }

    /** 音乐模式不闪屏，残影靠这个定时整屏刷一次清掉。 */
    private void maybePeriodicFullFlash() {
        if (flashMin <= 0) return;
        long nowMs = System.currentTimeMillis();
        if (nowMs - lastFlashAt < flashMin * 60L * 1000L) return;
        lastFlashAt = nowMs;
        if (fullBmp != null) {
            show(fullBmp, true);
            setStatus("整屏去残影 " + now());
        }
    }

    private void parseZones(JSONArray arr) {
        if (arr == null) return;
        List<Zone> out = new ArrayList<Zone>();
        for (int i = 0; i < arr.length(); i++) {
            JSONObject z = arr.optJSONObject(i);
            if (z == null) continue;
            Zone zone = new Zone();
            zone.name = z.optString("name", "");
            zone.action = z.optString("action", "");
            zone.x = z.optInt("x");
            zone.y = z.optInt("y");
            zone.w = z.optInt("w");
            zone.h = z.optInt("h");
            if (zone.action.length() > 0 && zone.w > 0 && zone.h > 0) out.add(zone);
        }
        zones = out.toArray(new Zone[out.size()]);
    }

    /** 看板没有可点区域 —— 换过去的时候必须清掉，否则会拿歌词面板的分区误判点击。 */
    private void clearZones() {
        zones = new Zone[0];
    }

    // ============================================================= 触摸

    private void onTap(float vx, float vy) {
        if (System.currentTimeMillis() - downT > 900) return;         // 长按不当点击
        float dx = vx - downX, dy = vy - downY;
        if (dx * dx + dy * dy > 40 * 40) return;                     // 拖动不当点击

        if (fullBmp == null) {
            setStatus("还没拿到画面");
            return;
        }
        int[] ip = viewToImage(vx, vy);
        if (ip == null) return;
        for (Zone z : zones) {
            if (z.hit(ip[0], ip[1])) {
                Log.i(TAG, "tap zone " + z.name + " -> " + z.action);
                sendCommand(z.action);
                return;
            }
        }
    }

    /**
     * 视图坐标 → 图片坐标（FIT_CENTER 的等比缩放 + 居中留白）。
     * 触摸监听挂在根布局上，它和 ImageView 同为 MATCH_PARENT，坐标空间一致。
     */
    private int[] viewToImage(float vx, float vy) {
        int vw = imageView.getWidth(), vh = imageView.getHeight();
        int iw = fullBmp.getWidth(), ih = fullBmp.getHeight();
        if (vw <= 0 || vh <= 0 || iw <= 0 || ih <= 0) return null;
        float sc = Math.min((float) vw / iw, (float) vh / ih);
        float dw = iw * sc, dh = ih * sc;
        float offX = (vw - dw) / 2f, offY = (vh - dh) / 2f;
        int ix = (int) ((vx - offX) / sc);
        int iy = (int) ((vy - offY) / sc);
        if (ix < 0 || iy < 0 || ix >= iw || iy >= ih) return null;
        return new int[]{ix, iy};
    }

    // ============================================================= 按键

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        // 翻页键 → 上一首 / 下一首（按下的第一次，忽略长按重复）
        if (event.getRepeatCount() == 0) {
            if (keyCode == keyPrev) {
                noteKey(keyCode, event, "prev");
                if ("music".equals(mode)) {
                    sendCommand("prev");
                    return true;
                }
            } else if (keyCode == keyNext) {
                noteKey(keyCode, event, "next");
                if ("music".equals(mode)) {
                    sendCommand("next");
                    return true;
                }
            }
        } else if (keyCode == keyPrev || keyCode == keyNext) {
            return "music".equals(mode);   // 长按重复都吃掉，避免刷屏
        }

        // 圆键（Back）留给系统，不拦
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            return super.onKeyDown(keyCode, event);
        }

        // 其它键：记下来，方便之后把映射写死
        noteKey(keyCode, event, null);
        return super.onKeyDown(keyCode, event);
    }

    private void noteKey(int keyCode, KeyEvent event, String mapped) {
        logKey(keyCode, event, mapped);
        if (keylog && "music".equals(mode)) {
            sendCommandRaw("{\"action\":\"key\",\"code\":" + keyCode
                    + ",\"scan\":" + event.getScanCode()
                    + ",\"mapped\":\"" + (mapped == null ? "" : mapped) + "\"}");
        }
    }

    private void logKey(int keyCode, KeyEvent event, String mapped) {
        try {
            String line = String.format(Locale.US, "%s keyCode=%d scan=%d repeat=%d %s\n",
                    new SimpleDateFormat("MM-dd HH:mm:ss", Locale.CHINA).format(new Date()),
                    keyCode, event.getScanCode(), event.getRepeatCount(),
                    mapped == null ? "(未映射)" : "-> " + mapped);
            File f = new File(sdcard(), KEYS_LOG);
            // 超过 64KB 就重开，别把 SD 卡写满
            if (f.exists() && f.length() > 65536) f.delete();
            FileOutputStream fos = new FileOutputStream(f, true);
            fos.write(line.getBytes("UTF-8"));
            fos.flush();
            fos.close();
        } catch (Throwable t) {
            Log.w(TAG, "logKey: " + t);
        }
    }

    // ============================================================= 指令

    private void sendCommand(final String action) {
        sendCommandRaw("{\"action\":\"" + action + "\"}");
        setStatus("已发送：" + action);
        // 下发后马上拉一次，别让用户等一个刷新周期
        handler.removeCallbacks(tickRunnable);
        handler.postDelayed(tickRunnable, 250);
    }

    private void sendCommandRaw(final String json) {
        new Thread(new Runnable() {
            public void run() {
                try {
                    byte[] body = json.getBytes("UTF-8");
                    HttpURLConnection conn = (HttpURLConnection) new URL(cmdUrl).openConnection();
                    conn.setConnectTimeout(8000);
                    conn.setReadTimeout(12000);
                    conn.setRequestMethod("POST");
                    conn.setDoOutput(true);
                    conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                    conn.setFixedLengthStreamingMode(body.length);
                    OutputStream os = conn.getOutputStream();
                    os.write(body);
                    os.flush();
                    os.close();
                    int code = conn.getResponseCode();
                    InputStream in = (code == 200) ? conn.getInputStream() : conn.getErrorStream();
                    String text = readAll(in);
                    conn.disconnect();
                    Log.i(TAG, "cmd " + code + " " + text);
                } catch (Throwable t) {
                    Log.w(TAG, "cmd: " + t);
                    setStatus("指令失败：" + t);
                }
            }
        }).start();
    }

    // ============================================================= 网络

    private byte[] fetchBytes(String urlStr) {
        HttpURLConnection conn = null;
        InputStream in = null;
        try {
            URL u = new URL(urlStr);
            conn = (HttpURLConnection) u.openConnection();
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(30000);
            conn.setUseCaches(false);
            conn.setRequestMethod("GET");
            int code = conn.getResponseCode();
            if (code != 200) {
                Log.w(TAG, "http " + code);
                return null;
            }
            // 看板的内容版本号随图一起下发，顺手带回来（没有这个头就是 0）
            try {
                String rv = conn.getHeaderField("X-H9Dash-Rev");
                if (rv != null && rv.length() > 0) {
                    dashRevHeader = Long.parseLong(rv.trim());
                }
            } catch (Throwable t) {
                dashRevHeader = 0;
            }
            // 长度声明与实际收到的字节数要能对得上 —— 对不上说明中途断流，
            // 宁可当「这次没取到」（设备下一轮重试），也绝不把半截 JSON 交上去。
            // 半截 JSON 会一路走到 org.json → JSONException → 屏上就定格成
            // 「解析失败，重试中」，看着像服务坏了，其实只是网络抖了一下。
            int declared = conn.getContentLength();
            in = conn.getInputStream();
            ByteArrayOutputStream bos = new ByteArrayOutputStream(
                declared > 0 && declared < (1 << 20) ? declared : 16384);
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            byte[] got = bos.toByteArray();
            if (declared > 0 && got.length != declared) {
                lastFetchIncomplete = true;   // 让调用方把状态说成「网络断了」
                Log.w(TAG, "short body: " + got.length + "/" + declared);
                return null;
            }
            return got;
        } catch (Throwable t) {
            Log.w(TAG, "fetch: " + t);
            lastFetchIncomplete = true;
            return null;
        } finally {
            close(in);
            if (conn != null) {
                try {
                    conn.disconnect();
                } catch (Throwable t) {
                }
            }
        }
    }

    private String fetchText(String urlStr) {
        byte[] b = fetchBytes(urlStr);
        if (b == null) return null;
        try {
            return new String(b, "UTF-8");
        } catch (Throwable t) {
            return null;
        }
    }

    private static String readAll(InputStream in) {
        if (in == null) return "";
        try {
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            return new String(bos.toByteArray(), "UTF-8");
        } catch (Throwable t) {
            return "";
        } finally {
            close(in);
        }
    }

    // ============================================================= 显示

    private void show(final Bitmap bmp, boolean flash) {
        if (!flash) {
            imageView.setImageBitmap(bmp);
            return;
        }
        // 全屏刷黑再刷白，强制 E-Ink 全局刷新去残影
        flashView.setBackgroundColor(Color.BLACK);
        flashView.setVisibility(View.VISIBLE);
        flashView.bringToFront();
        handler.postDelayed(new Runnable() {
            public void run() {
                // 闪屏这两步也在 UI 线程上，同样要兜底。
                // 这里尤其怕"位图已被回收"—— 350ms 的窗口里完全可能发生
                // （比如期间换了面板，fullBmp 被 recycle 掉了）。
                try {
                    flashView.setBackgroundColor(Color.WHITE);
                    handler.postDelayed(new Runnable() {
                        public void run() {
                            try {
                                imageView.setImageBitmap(bmp);
                                flashView.setVisibility(View.GONE);
                            } catch (Throwable t) {
                                Log.w(TAG, "flash restore: " + t);
                            }
                        }
                    }, 350);
                } catch (Throwable t) {
                    Log.w(TAG, "flash start: " + t);
                }
            }
        }, 350);
    }

    // ============================================================= 工具

    private void setStatus(final String s) {
        handler.post(new Runnable() {
            public void run() {
                // 内容没变就别 setText，省掉一次墨水屏刷新
                // 这本身也跑在 UI 线程上，同样要兜底 —— 状态行是要用来报错的，
                // 不能自己变成新的崩溃点。
                try {
                    if (!s.equals(statusView.getText().toString())) {
                        statusView.setText(s);
                    }
                } catch (Throwable t) {
                    Log.w(TAG, "setStatus: " + t);
                }
            }
        });
    }

    private String now() {
        return new SimpleDateFormat("MM-dd HH:mm", Locale.CHINA).format(new Date());
    }

    private static void writeFile(File f, String content) {
        FileOutputStream fos = null;
        try {
            fos = new FileOutputStream(f, false);
            fos.write(content.getBytes("UTF-8"));
            fos.flush();
        } catch (Throwable t) {
            Log.w(TAG, "write " + f + ": " + t);
        } finally {
            close(fos);
        }
    }

    private static void close(java.io.Closeable c) {
        if (c != null) {
            try {
                c.close();
            } catch (Throwable t) {
            }
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (wakeLock != null && !wakeLock.isHeld()) wakeLock.acquire();
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(tickRunnable);
        try {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        } catch (Throwable t) {
        }
        try {
            if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
        } catch (Throwable t) {
        }
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        // 圆键（Back）：交给系统，不做任何阻拦
        super.onBackPressed();
    }
}
