package com.topsir.dashboard;

import android.app.Activity;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.widget.ImageView;
import android.widget.Toast;

import java.io.InputStream;
import java.io.File;
import java.io.FileInputStream;
import java.util.Properties;
import java.net.HttpURLConnection;
import java.net.URL;

/** Minimal API-15-compatible full-screen image client. */
public final class MainActivity extends Activity {
    private static final String DEFAULT_URL = "http://192.168.1.2:8765/dash.png";
    private static final long REFRESH_MS = 30L * 60L * 1000L;

    private final Handler handler = new Handler();
    private ImageView imageView;
    private String dashboardUrl;

    private final Runnable refreshTask = new Runnable() {
        @Override public void run() {
            downloadAndShow();
            handler.postDelayed(this, REFRESH_MS);
        }
    };

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN, WindowManager.LayoutParams.FLAG_FULLSCREEN);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        imageView = new ImageView(this);
        imageView.setBackgroundColor(Color.WHITE);
        imageView.setScaleType(ImageView.ScaleType.FIT_CENTER);
        setContentView(imageView);
        dashboardUrl = loadUrlFromStorage();
        String intentUrl = getIntent().getStringExtra("url");
        if (intentUrl != null && intentUrl.length() > 0) dashboardUrl = intentUrl;
        if (dashboardUrl == null || dashboardUrl.length() == 0) dashboardUrl = DEFAULT_URL;
        handler.post(refreshTask);
    }

    private String loadUrlFromStorage() {
        File config = new File(Environment.getExternalStorageDirectory(), "h9-dashboard.properties");
        if (!config.exists()) return null;
        Properties properties = new Properties();
        FileInputStream input = null;
        try {
            input = new FileInputStream(config);
            properties.load(input);
            return properties.getProperty("url");
        } catch (Exception ignored) {
            return null;
        } finally {
            if (input != null) try { input.close(); } catch (Exception ignored) { }
        }
    }

    private void downloadAndShow() {
        final String url = dashboardUrl;
        new Thread(new Runnable() {
            @Override public void run() {
                HttpURLConnection connection = null;
                try {
                    connection = (HttpURLConnection) new URL(url + (url.contains("?") ? "&" : "?") + "ts=" + System.currentTimeMillis()).openConnection();
                    connection.setConnectTimeout(8000);
                    connection.setReadTimeout(15000);
                    connection.setUseCaches(false);
                    InputStream input = connection.getInputStream();
                    final Bitmap bitmap = BitmapFactory.decodeStream(input);
                    input.close();
                    if (bitmap == null) throw new IllegalStateException("Invalid PNG");
                    saveBitmap(bitmap);
                    runOnUiThread(new Runnable() { @Override public void run() { imageView.setImageBitmap(bitmap); } });
                } catch (final Exception error) {
                    final Bitmap cached = loadCachedBitmap();
                    runOnUiThread(new Runnable() { @Override public void run() {
                        if (cached != null) imageView.setImageBitmap(cached);
                        else Toast.makeText(MainActivity.this, "Dashboard unavailable", Toast.LENGTH_SHORT).show();
                    } });
                } finally {
                    if (connection != null) connection.disconnect();
                }
            }
        }).start();
    }

    private void saveBitmap(Bitmap bitmap) {
        java.io.FileOutputStream output = null;
        try {
            output = openFileOutput("last-dashboard.png", MODE_PRIVATE);
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, output);
        } catch (Exception ignored) {
        } finally {
            if (output != null) try { output.close(); } catch (Exception ignored) { }
        }
    }

    private Bitmap loadCachedBitmap() {
        try { return BitmapFactory.decodeFile(new File(getFilesDir(), "last-dashboard.png").getAbsolutePath()); }
        catch (Exception ignored) { return null; }
    }

    @Override protected void onDestroy() {
        handler.removeCallbacks(refreshTask);
        super.onDestroy();
    }
}
