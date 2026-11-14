package com.workbuddy.board;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.pm.ApplicationInfo;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.Window;
import android.webkit.JsResult;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * Python（Chaquopy）在 127.0.0.1 起本地服务，WebView 打开它；与桌面版共用 app/。
 */
public class MainActivity extends Activity {

    private static final String TAG = "WBBoard";
    private static final String BG = "#05070E";

    private WebView web;
    private FrameLayout root;
    private LinearLayout splash;
    private TextView splashTip;
    private volatile String baseUrl;
    private volatile boolean pythonBooted;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        paintBars();

        root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor(BG));

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        // 本地服务每次现渲染，禁用缓存。
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setUseWideViewPort(false);
        s.setLoadWithOverviewMode(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setSupportZoom(false);
        web.setBackgroundColor(Color.parseColor(BG));
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        web.setAlpha(0f);
        // 必须在 loadUrl 之前挂：晚挂收不到 onPageFinished，启动遮罩不消失。
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                revealPage();
            }

            @Override
            public void onReceivedError(WebView view, int errorCode, String description,
                                        String failingUrl) {
                Log.w(TAG, "webview error " + errorCode + " " + description + " @ " + failingUrl);
            }
        });
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));

        // WebView 默认不处理 window.confirm（直接返回 false，等于点了取消），要自己接。
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onJsConfirm(WebView view, String url, String message, final JsResult result) {
                if (isFinishing()) {
                    result.cancel();
                    return true;
                }
                new AlertDialog.Builder(MainActivity.this)
                        .setTitle("确认")
                        .setMessage(message)
                        .setPositiveButton(android.R.string.ok, new DialogInterface.OnClickListener() {
                            @Override
                            public void onClick(DialogInterface d, int w) {
                                result.confirm();
                            }
                        })
                        .setNegativeButton(android.R.string.cancel, new DialogInterface.OnClickListener() {
                            @Override
                            public void onClick(DialogInterface d, int w) {
                                result.cancel();
                            }
                        })
                        .setOnCancelListener(new DialogInterface.OnCancelListener() {
                            @Override
                            public void onCancel(DialogInterface d) {
                                result.cancel();
                            }
                        })
                        .show();
                return true;
            }
        });

        splash = buildSplash();
        root.addView(splash, new FrameLayout.LayoutParams(-1, -1));

        setContentView(root);
        startPython();
    }

    private void paintBars() {
        Window w = getWindow();
        w.setStatusBarColor(Color.parseColor(BG));
        w.setNavigationBarColor(Color.parseColor(BG));
        View decor = w.getDecorView();
        int flags = decor.getSystemUiVisibility();
        if (Build.VERSION.SDK_INT >= 23) {
            flags &= ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;   // 深色底 → 状态栏用浅色图标
        }
        if (Build.VERSION.SDK_INT >= 26) {
            flags &= ~View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
        }
        decor.setSystemUiVisibility(flags);
    }

    private LinearLayout buildSplash() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        box.setBackgroundColor(Color.parseColor(BG));

        ProgressBar bar = new ProgressBar(this);
        LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(dp(28), dp(28));
        box.addView(bar, bp);

        splashTip = new TextView(this);
        splashTip.setText(R.string.booting);
        splashTip.setTextColor(0xFFB9C4E0);
        splashTip.setTextSize(13);
        splashTip.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams tp = new LinearLayout.LayoutParams(-2, -2);
        tp.topMargin = dp(16);
        box.addView(splashTip, tp);
        return box;
    }

    private int dp(int v) {
        return Math.round(getResources().getDisplayMetrics().density * v);
    }

    private void startPython() {
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    copyPets();
                    if (!Python.isStarted()) {
                        Python.start(new AndroidPlatform(MainActivity.this));
                    }
                    pythonBooted = true;
                    String port = Python.getInstance()
                            .getModule("android_entry")
                            .callAttr("start", getFilesDir().getAbsolutePath())
                            .toString();
                    baseUrl = "http://127.0.0.1:" + port + "/";
                    Log.i(TAG, "local service ready at " + baseUrl);
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            web.loadUrl(baseUrl);
                            // 兜底：onPageFinished 没来时不至于卡在启动页。
                            root.postDelayed(new Runnable() {
                                @Override
                                public void run() {
                                    revealPage();
                                }
                            }, 5000);
                        }
                    });
                } catch (final Throwable t) {
                    Log.e(TAG, "python boot failed", t);
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            showError(t);
                        }
                    });
                }
            }
        }, "wb-python").start();
    }

    /** 页面出来了就把启动遮罩淡掉（幂等，重复调用无副作用）。 */
    private void revealPage() {
        if (web != null) {
            web.setAlpha(1f);
        }
        if (splash == null || splash.getVisibility() != View.VISIBLE) {
            return;
        }
        splash.animate().alpha(0f).setDuration(220).withEndAction(new Runnable() {
            @Override
            public void run() {
                if (splash != null) {
                    splash.setVisibility(View.GONE);
                }
            }
        }).start();
    }

    /** 宠物雪碧图跟着包走：先落到私有目录，Python 侧按 PET_DIR 直接读文件。 */
    private void copyPets() {
        File dst = new File(getFilesDir(), "pets");
        if (!dst.exists() && !dst.mkdirs()) {
            Log.w(TAG, "cannot create " + dst);
        }
        String[] names;
        try {
            names = getAssets().list("pets");
        } catch (IOException e) {
            Log.w(TAG, "no pets assets", e);
            return;
        }
        if (names == null) {
            return;
        }
        for (String n : names) {
            if (!n.endsWith(".png")) {
                continue;
            }
            File out = new File(dst, n);
            if (out.length() > 0) {
                continue;
            }
            InputStream in = null;
            OutputStream os = null;
            try {
                in = getAssets().open("pets/" + n);
                os = new FileOutputStream(out);
                byte[] buf = new byte[16384];
                int r;
                while ((r = in.read(buf)) > 0) {
                    os.write(buf, 0, r);
                }
            } catch (IOException e) {
                Log.w(TAG, "copy pet failed: " + n, e);
            } finally {
                closeQuietly(in);
                closeQuietly(os);
            }
        }
    }

    private static void closeQuietly(java.io.Closeable c) {
        if (c != null) {
            try {
                c.close();
            } catch (IOException ignored) {
            }
        }
    }

    private void showError(Throwable t) {
        splash.setVisibility(View.GONE);
        String detail = t.getClass().getSimpleName() + ": " + String.valueOf(t.getMessage());
        TextView tv = new TextView(this);
        tv.setText(getString(R.string.boot_failed) + "\n\n" + detail
                + "\n\n（" + (pythonBooted ? "服务未就绪" : "Python 未启动") + "，点一下重试）");
        tv.setTextColor(0xFFE8ECF8);
        tv.setTextSize(14);
        tv.setGravity(Gravity.CENTER);
        tv.setPadding(dp(24), dp(24), dp(24), dp(24));
        tv.setBackgroundColor(Color.parseColor(BG));
        tv.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                root.removeView(v);
                startPython();
            }
        });
        root.addView(tv, new FrameLayout.LayoutParams(-1, -1));
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            root.removeView(web);
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
