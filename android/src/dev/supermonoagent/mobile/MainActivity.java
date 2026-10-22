package dev.supermonoagent.mobile;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.webkit.CookieManager;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;
import org.json.JSONObject;
import org.json.JSONException;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.BufferedReader;
import java.io.InputStreamReader;

/** Packaged mobile document, system WebView, and no native JavaScript bridge. */
public final class MainActivity extends Activity {
    private SharedPreferences preferences;
    private JSONObject defaults = new JSONObject();
    private LinearLayout root;
    private WebView terminal;
    private JSONObject connection;
    private volatile String localBase;
    private int connectionFormGeneration;
    private HotAssets hotAssets;
    private boolean keyboardVisible;
    private final String[] renderers = {"auto", "dom", "webgl"};

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        WebView.setWebContentsDebuggingEnabled(getIntent().getBooleanExtra("ui_probe", false));
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        if (Build.VERSION.SDK_INT >= 30) getWindow().setDecorFitsSystemWindows(false);
        preferences = getSharedPreferences("connection", MODE_PRIVATE);
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(
                getAssets().open("connection-defaults.json"), "UTF-8"))) {
            StringBuilder json = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) json.append(line);
            defaults = new JSONObject(json.toString());
        } catch (IOException | JSONException error) {
            // A generic build can still connect using values entered by its owner.
        }
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(12, 16, 23));
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                android.graphics.Insets ime = insets.getInsets(WindowInsets.Type.ime());
                keyboardVisible = insets.isVisible(WindowInsets.Type.ime());
                view.setPadding(bars.left, bars.top, bars.right, Math.max(bars.bottom, ime.bottom));
                return WindowInsets.CONSUMED;
            }
            view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets.consumeSystemWindowInsets();
        });
        // API 26–29 uses adjustResize; track keyboard visibility for the Back action.
        root.getViewTreeObserver().addOnGlobalLayoutListener(() -> {
            if (Build.VERSION.SDK_INT < 30) {
                android.graphics.Rect visible = new android.graphics.Rect();
                root.getWindowVisibleDisplayFrame(visible);
                keyboardVisible = root.getRootView().getHeight() - visible.bottom > 150 * getResources().getDisplayMetrics().density;
            }
        });
        setContentView(root);
        if (Build.VERSION.SDK_INT >= 33) getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::handleBack);
        showConnection();
    }

    private EditText input(LinearLayout form, String hint, String value, boolean secret) {
        EditText field = new EditText(this);
        field.setSingleLine(true);
        field.setHint(hint);
        field.setText(value);
        field.setTextColor(Color.WHITE);
        field.setHintTextColor(Color.LTGRAY);
        field.setInputType(secret ? 129 : 17);
        form.addView(field);
        return field;
    }

    private void button(LinearLayout parent, String label, View.OnClickListener action) {
        Button button = new Button(this);
        button.setText(label);
        button.setOnClickListener(action);
        parent.addView(button);
    }

    private void destroyTerminal() {
        if (terminal == null) return;
        WebView previous = terminal;
        terminal = null;
        root.removeView(previous);
        // A renderer-gone WebView must only be removed/destroyed, never reused.
        previous.destroy();
    }

    private void showConnection() {
        final int formGeneration = ++connectionFormGeneration;
        destroyTerminal();
        connection = null;
        localBase = null;
        root.removeAllViews();
        LinearLayout form = new LinearLayout(this);
        form.setOrientation(LinearLayout.VERTICAL);
        int padding = (int)(20 * getResources().getDisplayMetrics().density);
        form.setPadding(padding, padding, padding, padding);
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.addView(form);
        root.addView(scroll, new LinearLayout.LayoutParams(-1, -1));
        TextView title = new TextView(this);
        title.setText("SuperMonoAgent\n连接远程终端");
        title.setTextColor(Color.WHITE);
        title.setTextSize(24);
        form.addView(title);
        TextView version = new TextView(this);
        try {
            android.content.pm.PackageInfo info = getPackageManager().getPackageInfo(getPackageName(), 0);
            version.setText("版本 " + info.versionName + " · 构建 " + info.versionCode);
        } catch (android.content.pm.PackageManager.NameNotFoundException error) {
            version.setText("版本信息不可用");
        }
        version.setTextColor(Color.LTGRAY);
        version.setTextSize(14);
        form.addView(version);
        hotAssets = HotAssets.load(getExternalFilesDir(null));
        TextView uiVersion = new TextView(this);
        uiVersion.setText("界面版本：" + (hotAssets.getRevision().isEmpty() ? "随安装包" : hotAssets.getRevision()));
        uiVersion.setTextColor(Color.LTGRAY);
        form.addView(uiVersion);
        EditText url = input(form, "服务地址 https://relay.example.com", preferences.getString("url", defaults.optString("endpoint")), false);
        EditText code = input(form, "连接码（中继模式）", preferences.getString("code", defaults.optString("code")), false);
        EditText token = input(form, "访问令牌（仅本次连接保留）", defaults.optString("token"), true);
        Spinner renderer = new Spinner(this);
        renderer.setAdapter(new ArrayAdapter<String>(this, android.R.layout.simple_spinner_dropdown_item,
                new String[]{"自动选择渲染器", "默认渲染（DOM）", "WebGL 渲染（GPU）"}));
        String selected = preferences.getString("renderer", "auto");
        for (int i = 0; i < renderers.length; i++) if (renderers[i].equals(selected)) renderer.setSelection(i);
        form.addView(renderer);
        TextView help = new TextView(this);
        help.setText("界面内置于应用，直接连接后台或中继。局域网支持 HTTP；公网建议 HTTPS。"
                + (defaults.optString("token").isEmpty() ? "令牌不会保存到偏好设置。" : "个人安装包已内置连接凭据，请勿转发。"));
        help.setTextColor(Color.LTGRAY);
        form.addView(help);
        TextView probeStatus = new TextView(this);
        probeStatus.setTextColor(Color.LTGRAY);
        probeStatus.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        Button probe = new Button(this);
        probe.setText("测试连接");
        form.addView(probe);
        form.addView(probeStatus);
        probe.setOnClickListener(view -> {
            final String endpoint = url.getText().toString().trim();
            final String relayCode = code.getText().toString().trim();
            final String accessToken = token.getText().toString();
            probe.setEnabled(false);
            probeStatus.setText("正在测试地址、连接码和令牌…");
            new Thread(() -> {
                final String result = ConnectionProbe.test(endpoint, relayCode, accessToken);
                runOnUiThread(() -> {
                    if (isFinishing() || isDestroyed() || formGeneration != connectionFormGeneration) return;
                    probe.setEnabled(true);
                    boolean unchanged = endpoint.equals(url.getText().toString().trim())
                            && relayCode.equals(code.getText().toString().trim())
                            && accessToken.equals(token.getText().toString());
                    probeStatus.setText(unchanged ? result : "连接参数已修改，请重新测试。");
                });
            }, "connection-probe").start();
        });
        button(form, "连接", view -> {
            try {
                Uri endpoint = validEndpoint(url.getText().toString().trim());
                String mode = renderers[renderer.getSelectedItemPosition()];
                String relayCode = code.getText().toString().trim();
                preferences.edit().putString("url", endpoint.toString()).putString("code", relayCode)
                        .putString("renderer", mode).apply();
                connection = new JSONObject();
                connection.put("endpoint", endpoint.toString());
                connection.put("code", relayCode);
                connection.put("token", token.getText().toString());
                connection.put("renderer", mode);
                localBase = LocalAssets.originForEndpoint(endpoint.toString());
                openTerminal();
            } catch (IllegalArgumentException | JSONException error) { url.setError(error.getMessage()); }
        });
        TextView changes = new TextView(this);
        changes.setTextColor(Color.LTGRAY);
        changes.setPadding(0, padding, 0, 0);
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(getAssets().open("CHANGELOG.txt"), "UTF-8"))) {
            StringBuilder text = new StringBuilder("最近更新\n");
            String line;
            while ((line = reader.readLine()) != null) text.append(line).append('\n');
            changes.setText(text.toString());
        } catch (IOException error) {
            changes.setText("最近更新：适配手机与折叠屏，改善连接和终端显示。");
        }
        form.addView(changes);
    }

    static Uri validEndpoint(String value) {
        ConnectionEndpoint.validate(value);
        return Uri.parse(value);
    }

    private boolean trustedDocument() {
        return terminal != null && terminal.getUrl() != null && localBase != null
                && LocalAssets.isDocument(localBase, terminal.getUrl());
    }

    private boolean navigation(Uri target) {
        if (localBase == null || !(localBase.equals(target.getScheme() + "://" + target.getAuthority()))) return true;
        if ("/settings".equals(target.getPath())) { showConnection(); return true; }
        if ("/reload".equals(target.getPath())) { openTerminal(); return true; }
        return !LocalAssets.isDocument(localBase, target.toString());
    }

    private WebResourceResponse blocked() {
        return new WebResourceResponse("text/plain", "UTF-8", 404, "Not Found", null, new ByteArrayInputStream(new byte[0]));
    }

    private void openTerminal() {
        if (connection == null) { showConnection(); return; }
        connectionFormGeneration++;
        hotAssets = HotAssets.load(getExternalFilesDir(null));
        connection.remove("uiRevision");
        try { connection.put("uiRevision", hotAssets.getRevision().isEmpty() ? "bundled" : hotAssets.getRevision()); }
        catch (JSONException error) { throw new IllegalStateException(error); }
        destroyTerminal();
        root.removeAllViews();
        terminal = new WebView(this);
        terminal.setBackgroundColor(Color.rgb(12, 16, 23));
        WebSettings settings = terminal.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setTextZoom(100);
        settings.setSupportZoom(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setSupportMultipleWindows(false);
        settings.setMediaPlaybackRequiresUserGesture(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(terminal, false);
        terminal.setWebViewClient(new WebViewClient() {
            @Override public WebResourceResponse shouldInterceptRequest(WebView web, WebResourceRequest request) {
                String url = request.getUrl().toString();
                String base = localBase;
                if (base == null || !url.startsWith(base + "/") || !"GET".equals(request.getMethod())) return blocked();
                String asset = LocalAssets.assetPath(base, url);
                if (asset == null) {
                    android.util.Log.w("SuperMonoAgent", "Blocked local path " + request.getUrl().getPath());
                    return blocked();
                }
                try {
                    java.util.Map<String, String> headers = new java.util.HashMap<String, String>();
                    headers.put("Cache-Control", "no-store");
                    headers.put("X-Content-Type-Options", "nosniff");
                    java.io.InputStream stream = hotAssets == null ? null : hotAssets.open(asset);
                    if (stream == null) stream = getAssets().open(asset);
                    return new WebResourceResponse(LocalAssets.mime(asset), "UTF-8", 200, "OK", headers, stream);
                } catch (IOException error) {
                    android.util.Log.w("SuperMonoAgent", "Missing packaged asset " + asset, error);
                    return blocked();
                }
            }
            @Override public boolean shouldOverrideUrlLoading(WebView web, WebResourceRequest request) {
                return !request.isForMainFrame() || navigation(request.getUrl());
            }
            @Override public boolean shouldOverrideUrlLoading(WebView web, String url) { return navigation(Uri.parse(url)); }
            @Override public void onReceivedError(WebView web, WebResourceRequest request, WebResourceError error) {
                if (web == terminal && request.isForMainFrame()) showLoadError("终端界面加载失败，请重试。");
            }
            @Override public void onReceivedHttpError(WebView web, WebResourceRequest request, WebResourceResponse response) {
                android.util.Log.w("SuperMonoAgent", "Resource HTTP " + response.getStatusCode()
                        + " path=" + request.getUrl().getPath() + " main=" + request.isForMainFrame());
                if (web == terminal && request.isForMainFrame()) showLoadError("终端资源加载失败（" + response.getStatusCode() + "）。");
            }
            @Override public boolean onRenderProcessGone(WebView web, RenderProcessGoneDetail detail) {
                if (web == terminal) showLoadError("终端渲染进程已退出，后台会话仍在运行。");
                return true;
            }
        });
        root.addView(terminal, new LinearLayout.LayoutParams(-1, -1));
        terminal.loadUrl(localBase + "/mobile/index.html#" + Uri.encode(connection.toString()));
    }

    private void showLoadError(String message) {
        destroyTerminal();
        root.removeAllViews();
        TextView text = new TextView(this);
        text.setText(message);
        text.setTextColor(Color.WHITE);
        text.setPadding(20, 24, 20, 24);
        root.addView(text);
        button(root, "重新加载", view -> openTerminal());
        button(root, "连接设置", view -> showConnection());
    }

    private void lifecycle(String action) {
        if (trustedDocument()) terminal.evaluateJavascript(
                "if(window.MobileTerminal && typeof window.MobileTerminal." + action + "==='function') window.MobileTerminal." + action + "();", null);
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (intent.getBooleanExtra("ui_probe", false)) WebView.setWebContentsDebuggingEnabled(true);
        if (intent.getBooleanExtra("ui_probe", false) && intent.getBooleanExtra("hide_keyboard", false)) {
            if (terminal != null) terminal.clearFocus();
            ((InputMethodManager)getSystemService(INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(root.getWindowToken(), 0);
        }
        if (intent.getBooleanExtra("ui_probe", false) && intent.getBooleanExtra("show_settings", false)) {
            showConnection();
            return;
        }
        if (intent.getBooleanExtra("reload_ui", false)) {
            if (connection != null) openTerminal(); else showConnection();
        }
    }

    @Override protected void onPause() {
        lifecycle("pause");
        if (terminal != null) terminal.onPause();
        super.onPause();
    }
    @Override protected void onResume() {
        super.onResume();
        if (terminal != null) terminal.onResume();
        lifecycle("resume");
    }
    @Override public void onBackPressed() { handleBack(); }
    private void handleBack() {
        if (keyboardVisible) {
            ((InputMethodManager)getSystemService(INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(root.getWindowToken(), 0);
            return;
        }
        if (connection == null) { finish(); return; }
        new AlertDialog.Builder(this).setMessage("返回连接设置？后台终端会话会继续运行。")
                .setPositiveButton("连接设置", (dialog, which) -> showConnection()).setNegativeButton("留在终端", null).show();
    }
    @Override protected void onDestroy() {
        destroyTerminal();
        connection = null;
        localBase = null;
        super.onDestroy();
    }
}
