package com.wu.h9dash;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.FileReader;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;

import android.os.Environment;
import android.util.Log;

/**
 * 网络配置（host / port / token / 面板模式）的读写。
 *
 * 之前这段逻辑长在 MainActivity.loadConfig() 里，跟 UI 混在一起：
 * 结果就是"自动发现找到了新地址"这件事没法回写 —— 因为 loadConfig 只读不写。
 * 抽出来之后，两条路径共用同一个落盘动作：
 *   · 手工改 dashboard.conf   → 设备重启读进来
 *   · 自动发现找到 PC         → 直接写回 dashboard.conf，以后不再受 DHCP 变动影响
 *
 * 文件格式沿用 Properties：`key = value`，带 # 注释。
 * 保留注释是有意的 —— 用户拿记事本打开时得看得懂每个键干什么。
 */
public final class NetConfig {

    private static final String TAG = "H9Dash";
    private static final String CONF = "dashboard.conf";

    public String host = "192.168.1.100";
    public int port = 8765;
    public String token = "";
    public String mode = "dash";          // dash | music

    // ---- 下面这些不属于网络，但同在一个文件里，顺带存一下免得反复解析 ----
    public int intervalMin = 30;
    public int flashMin = 20;
    public int rotate = 0;
    public String orientation = "portrait";
    public boolean showStatus = true;
    public boolean doFlash = true;
    public boolean keylog = true;
    public String scale = "fit";
    public int keyPrev = 92;
    public int keyNext = 93;

    /** 上次是从哪来的，仅用于状态行显示。 */
    public String source = "";

    // ------------------------------------------------------------ 路径

    public static File sdcard() {
        File f = null;
        try {
            f = Environment.getExternalStorageDirectory();
        } catch (Throwable t) {
            f = null;
        }
        if (f != null && f.exists()) return f;
        String[] cands = {"/mnt/sdcard", "/storage/sdcard0", "/sdcard", "/mnt/extsd"};
        for (String c : cands) {
            File t = new File(c);
            if (t.exists()) return t;
        }
        return new File("/mnt/sdcard");
    }

    public static File confFile() {
        return new File(sdcard(), CONF);
    }

    // ------------------------------------------------------------ 读

    /**
     * 读配置。文件不存在时生成一份带注释的默认文件（指向 192.168.1.100，
     * 反正真正的地址靠自动发现来填）。
     */
    public static NetConfig load() {
        NetConfig c = new NetConfig();
        File conf = confFile();
        if (!conf.exists()) {
            c.writeDefaultFile();
        }

        Properties p = new Properties();
        BufferedReader br = null;
        try {
            br = new BufferedReader(new FileReader(conf));
            p.load(br);
        } catch (Throwable t) {
            Log.w(TAG, "read conf: " + t);
        } finally {
            close(br);
        }

        c.host = p.getProperty("host", "").trim();
        c.port = parseInt(p.getProperty("port", "8765"), 8765);
        c.token = p.getProperty("token", "").trim();

        // 兼容老版本：配置里只有一个完整 url 时，把它拆开。
        String url = p.getProperty("url", "").trim();
        if (url.length() > 0) {
            parseUrlInto(url, c);
        }
        if (c.host.length() == 0) c.host = "192.168.1.100";

        c.mode = p.getProperty("mode", "dash").trim().toLowerCase(java.util.Locale.US);
        if (!"music".equals(c.mode)) c.mode = "dash";

        c.intervalMin = parseInt(p.getProperty("interval_min", "30"), 30);
        c.flashMin = parseInt(p.getProperty("flash_min", "20"), 20);
        c.rotate = parseInt(p.getProperty("rotate", "0"), 0);
        c.keyPrev = parseInt(p.getProperty("key_prev", "92"), 92);
        c.keyNext = parseInt(p.getProperty("key_next", "93"), 93);

        c.orientation = p.getProperty("orientation", "portrait").trim();
        c.scale = p.getProperty("scale", "fit").trim();
        c.showStatus = !"0".equals(p.getProperty("status", "1").trim());
        c.doFlash = !"0".equals(p.getProperty("flash", "1").trim());
        c.keylog = !"0".equals(p.getProperty("keylog", "1").trim());
        return c;
    }

    /** 从一条完整 url 里拆出 host / port / token（兼容旧配置格式）。 */
    public static void parseUrlInto(String url, NetConfig c) {
        try {
            int scheme = url.indexOf("://");
            String rest = (scheme >= 0) ? url.substring(scheme + 3) : url;
            int slash = rest.indexOf('/');
            String hostPort = (slash >= 0) ? rest.substring(0, slash) : rest;
            String path = (slash >= 0) ? rest.substring(slash) : "";

            int colon = hostPort.lastIndexOf(':');
            if (colon > 0) {
                c.host = hostPort.substring(0, colon);
                c.port = parseInt(hostPort.substring(colon + 1), c.port);
            } else if (hostPort.length() > 0) {
                c.host = hostPort;
            }

            int q = path.indexOf("t=");
            if (q >= 0) {
                String t = path.substring(q + 2);
                int amp = t.indexOf('&');
                if (amp >= 0) t = t.substring(0, amp);
                if (t.length() > 0) c.token = t;
            }
        } catch (Throwable t) {
            Log.w(TAG, "parseUrl: " + t);
        }
    }

    // ------------------------------------------------------------ 写

    /** 面板对应的图片路径。 */
    public String panelPath() {
        return "music".equals(mode) ? "/music.png" : "/dash.png";
    }

    /** 拼出取图 URL。 */
    public String imageUrl() {
        return baseUrl() + panelPath() + query();
    }

    public String baseUrl() {
        return "http://" + host + ":" + port;
    }

    private String query() {
        return token.length() > 0 ? ("?t=" + token) : "";
    }

    /**
     * 把当前配置写回 dashboard.conf，并保留所有非网络键。
     *
     * 刻意整份重写而不是原地替换某几行：文件很短，重写更不容易写出坏行；
     * 同时把注释一并写上，用户下次打开还看得懂。
     */
    public void save() {
        StringBuilder sb = new StringBuilder();
        sb.append("# H9Dash 配置\n");
        sb.append("# 这个文件由 App 自动维护（自动发现到 PC 就会更新 host/port）。\n");
        sb.append("# 手工改也认：改完在设备上重开一次 App 即可。\n");
        sb.append("#\n");
        sb.append("# 想固定地址就在路由器上给电脑做 MAC 绑定，然后把 auto_find 设为 0。\n");
        sb.append("\n");
        sb.append("# ---- 网络 ----\n");
        sb.append("host = ").append(host).append("\n");
        sb.append("port = ").append(port).append("\n");
        sb.append("token = ").append(token).append("\n");
        sb.append("\n");
        sb.append("# ---- 面板 ----\n");
        sb.append("# mode = dash   信息看板\n");
        sb.append("# mode = music  网易云歌词面板\n");
        sb.append("mode = ").append(mode).append("\n");
        sb.append("\n");
        sb.append("# ---- 刷新节奏 ----\n");
        sb.append("interval_min = ").append(intervalMin).append("\n");
        sb.append("flash = ").append(doFlash ? "1" : "0").append("\n");
        sb.append("flash_min = ").append(flashMin).append("\n");
        sb.append("\n");
        sb.append("# ---- 显示 ----\n");
        sb.append("rotate = ").append(rotate).append("\n");
        sb.append("orientation = ").append(orientation).append("\n");
        sb.append("status = ").append(showStatus ? "1" : "0").append("\n");
        sb.append("scale = ").append(scale).append("\n");
        sb.append("\n");
        sb.append("# ---- 按键 ----\n");
        sb.append("keylog = ").append(keylog ? "1" : "0").append("\n");
        sb.append("key_prev = ").append(keyPrev).append("\n");
        sb.append("key_next = ").append(keyNext).append("\n");

        writeFile(confFile(), sb.toString());
        Log.i(TAG, "saved conf: " + host + ":" + port + " mode=" + mode);
    }

    private void writeDefaultFile() {
        NetConfig c = new NetConfig();
        c.host = "192.168.1.100";
        c.port = 8765;
        c.save();
    }

    /** 供状态行显示：把宿主名和端口拼短一点。 */
    public String shortTarget() {
        return host + ":" + port;
    }

    // ------------------------------------------------------------ 工具

    private static int parseInt(String s, int def) {
        try {
            return Integer.parseInt(s.trim());
        } catch (Throwable t) {
            return def;
        }
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
}
