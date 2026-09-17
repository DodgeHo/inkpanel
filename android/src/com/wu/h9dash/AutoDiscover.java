package com.wu.h9dash;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.net.URL;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import org.json.JSONObject;

import android.util.Log;

/**
 * 自动发现 PC 上的 H9Dash 服务。
 *
 * 背景：设备端 dashboard.conf 里的 url 是写死的 IP，路由器 DHCP 一变动就失效，
 * 表现就是屏幕上"获取失败"，而我们又没法在墨水屏上舒服地敲 IP。所以让设备自己找。
 *
 * 做法：
 *   1. 枚举本机所有"像局域网"的 IPv4 地址，推出 /24 网段候选（按可能性排序）；
 *   2. 对每个候选发一个便宜的 TCP 探测（connect timeout 250ms）看端口开不开；
 *   3. 对端口开着的地址发 GET /health，校验返回 JSON 里的 service 字段 == "H9Dash"，
 *      顺便把 token 从返回的示例 URL 里抠出来 —— 这样连 token 都不用输；
 *   4. 找到就返回，把结果写进 dashboard.conf。
 *
 * 为什么不用 UDP 广播：Wi-Fi 上的 AP 隔离、或者路由器不转发定向广播，
 * 都会让广播静默失败，而且失败时无从调试。逐个 TCP 探测慢一点，但确定性强。
 *
 * 线程模型：resolve() 是阻塞的，必须在后台线程调用。内部用一个固定线程池并发扫描，
 * 单线程去顺序扫 254 个地址即使 250ms 超时也要一分多钟，并发之后几秒内出结果。
 */
public final class AutoDiscover {

    private static final String TAG = "H9Dash";
    public static final int DEFAULT_PORT = 8765;

    /** 扫一个 /24 最多 254 个地址，并发 32 路，实测几秒内出结果。 */
    private static final int THREADS = 32;
    private static final int TCP_TIMEOUT_MS = 250;
    private static final int HTTP_CONNECT_MS = 1500;
    private static final int HTTP_READ_MS = 2500;

    /** 一次发现的结果。 */
    public static final class Result {
        public final String host;      // 形如 "192.168.1.100"
        public final int port;         // 8765
        public final String token;     // 从 /health 里抠出来的，可能为空
        public final long elapsedMs;

        Result(String host, int port, String token, long elapsedMs) {
            this.host = host;
            this.port = port;
            this.token = token == null ? "" : token;
            this.elapsedMs = elapsedMs;
        }

        public String baseUrl() {
            return "http://" + host + ":" + port;
        }

        @Override
        public String toString() {
            return baseUrl() + " token=" + (token.length() > 8 ? token.substring(0, 8) + "..." : token);
        }
    }

    /** 发现失败时抛出，message 是给人看的中文原因。 */
    public static final class NotFound extends Exception {
        public NotFound(String msg) {
            super(msg);
        }
    }

    private AutoDiscover() {
    }

    /**
     * 同步扫描，找到就返回，找不到抛 NotFound。
     * 调用方必须在后台线程执行（内部会阻塞数秒）。
     */
    public static Result resolve(int port) throws NotFound {
        long t0 = System.currentTimeMillis();

        List<String> prefixes = candidatePrefixes();
        if (prefixes.isEmpty()) {
            throw new NotFound("设备没有可用的局域网地址，先连上 WiFi");
        }
        Log.i(TAG, "discover: 候选网段 " + prefixes);

        for (String prefix : prefixes) {
            Result r = scanPrefix(prefix, port, t0);
            if (r != null) return r;
        }
        long sec = (System.currentTimeMillis() - t0) / 1000;
        throw new NotFound("扫了 " + prefixes.size() + " 个网段（" + sec + "秒）没找到 PC 服务，"
                + "确认电脑上的服务开着、且和平板连的是同一个 WiFi");
    }

    /**
     * 列出可能的 /24 前缀，按命中概率排序：
     *   192.168 / 10. / 172.16-31 这类私有网段排前面，
     *   具体接口是 wlan 的排更前 —— 平板走 Wi-Fi 上网，服务大概率也在 Wi-Fi 侧。
     */
    private static List<String> candidatePrefixes() {
        List<String> wifi = new ArrayList<String>();
        List<String> other = new ArrayList<String>();

        try {
            Enumeration<NetworkInterface> ifaces = NetworkInterface.getNetworkInterfaces();
            while (ifaces != null && ifaces.hasMoreElements()) {
                NetworkInterface ni = ifaces.nextElement();
                if (!ni.isUp() || ni.isLoopback()) continue;
                String name = ni.getName() == null ? "" : ni.getName().toLowerCase();

                Enumeration<InetAddress> addrs = ni.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    InetAddress a = addrs.nextElement();
                    String ip = a.getHostAddress();
                    if (ip == null || !isPrivateV4(ip)) continue;

                    String prefix = ip.substring(0, ip.lastIndexOf('.'));
                    boolean looksWifi = name.contains("wlan") || name.contains("wifi")
                            || name.contains("eth0") && !name.contains("eth1");
                    if (looksWifi) {
                        if (!wifi.contains(prefix)) wifi.add(prefix);
                    } else {
                        if (!other.contains(prefix)) other.add(prefix);
                    }
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "ifaces: " + t);
        }

        List<String> out = new ArrayList<String>();
        out.addAll(wifi);
        out.addAll(other);
        return out;
    }

    /** 只认私有网段，公网/Tailscale/169.254 之类一律跳过，省得白扫。 */
    private static boolean isPrivateV4(String ip) {
        String[] p = ip.split("\\.");
        if (p.length != 4) return false;
        try {
            int a = Integer.parseInt(p[0]);
            int b = Integer.parseInt(p[1]);
            if (a == 10) return true;
            if (a == 192 && b == 168) return true;
            if (a == 172 && b >= 16 && b <= 31) return true;
            return false;
        } catch (Throwable t) {
            return false;
        }
    }

    /** 扫一个 /24：先并发探端口，再对开着的端口并发问 /health。 */
    private static Result scanPrefix(String prefix, int port, long t0) {
        ExecutorService pool = Executors.newFixedThreadPool(THREADS);
        try {
            final List<String> open = Collections.synchronizedList(new ArrayList<String>());
            final CountDownLatch latch = new CountDownLatch(254);
            final AtomicBoolean stop = new AtomicBoolean(false);

            for (int i = 1; i <= 254; i++) {
                final String host = prefix + "." + i;
                pool.execute(new Runnable() {
                    public void run() {
                        try {
                            if (!stop.get() && portOpen(host, DEFAULT_PORT, TCP_TIMEOUT_MS)) {
                                open.add(host);
                            }
                        } finally {
                            latch.countDown();
                        }
                    }
                });
            }
            latch.await(15, TimeUnit.SECONDS);

            if (open.isEmpty()) return null;
            Log.i(TAG, "discover: " + prefix + ".x 端口开放 " + open);

            // 对候选并发问 /health，第一个确认是 H9Dash 的就赢
            final Result[] hit = new Result[1];
            final CountDownLatch latch2 = new CountDownLatch(open.size());
            for (final String host : new ArrayList<String>(open)) {
                pool.execute(new Runnable() {
                    public void run() {
                        try {
                            if (hit[0] != null) return;
                            String body = get("http://" + host + ":" + DEFAULT_PORT + "/health", 2048);
                            if (body == null) return;
                            JSONObject o = new JSONObject(body);
                            if (!"H9Dash".equals(o.optString("service", ""))) return;
                            String token = extractToken(o);
                            synchronized (hit) {
                                if (hit[0] == null) {
                                    hit[0] = new Result(host, DEFAULT_PORT, token,
                                            System.currentTimeMillis() - t0);
                                }
                            }
                        } catch (Throwable t) {
                            // 端口开着但不是我们的服务，忽略
                        } finally {
                            latch2.countDown();
                        }
                    }
                });
            }
            latch2.await(12, TimeUnit.SECONDS);
            if (hit[0] != null) stop.set(true);
            return hit[0];
        } catch (Throwable t) {
            Log.w(TAG, "scan " + prefix + ": " + t);
            return null;
        } finally {
            pool.shutdownNow();
        }
    }

    private static boolean portOpen(String host, int port, int timeoutMs) {
        Socket s = new Socket();
        try {
            s.connect(new InetSocketAddress(host, port), timeoutMs);
            return true;
        } catch (Throwable t) {
            return false;
        } finally {
            try {
                s.close();
            } catch (Throwable t) {
            }
        }
    }

    /**
     * 从 /health 的返回里抠 token。
     * 服务端会给 music_url / dash_url 之类的完整示例地址，token 就在 ?t= 后面。
     */
    private static String extractToken(JSONObject o) {
        String[] keys = {"music_url", "dash_url", "url"};
        for (String k : keys) {
            String v = o.optString(k, "");
            int i = v.indexOf("t=");
            if (i >= 0) {
                String t = v.substring(i + 2);
                int amp = t.indexOf('&');
                if (amp >= 0) t = t.substring(0, amp);
                if (t.length() > 0) return t;
            }
        }
        return "";
    }

    private static String get(String urlStr, int maxBytes) {
        HttpURLConnection conn = null;
        InputStream in = null;
        try {
            conn = (HttpURLConnection) new URL(urlStr).openConnection();
            conn.setConnectTimeout(HTTP_CONNECT_MS);
            conn.setReadTimeout(HTTP_READ_MS);
            conn.setUseCaches(false);
            conn.setRequestMethod("GET");
            if (conn.getResponseCode() != 200) return null;
            in = conn.getInputStream();
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[1024];
            int n;
            while ((n = in.read(buf)) > 0) {
                bos.write(buf, 0, n);
                if (bos.size() > maxBytes) break;
            }
            return new String(bos.toByteArray(), "UTF-8");
        } catch (Throwable t) {
            return null;
        } finally {
            try {
                if (in != null) in.close();
            } catch (Throwable t) {
            }
            if (conn != null) conn.disconnect();
        }
    }
}
