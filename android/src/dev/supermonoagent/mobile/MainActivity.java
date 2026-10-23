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
import org.json.JSONArray;
import org.json.JSONException;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.BufferedReader;
import java.io.InputStreamReader;

/** Verified local documents; updater commands use ticketed main-frame navigation only. */
public final class MainActivity extends Activity {
    private SharedPreferences preferences;
    private JSONObject defaults = new JSONObject();
    private LinearLayout root;
    private WebView terminal;
    private JSONObject connection;
    private volatile String localBase;
    private int connectionFormGeneration;
    private Runnable saveConnectionDraft;
    private HotAssets hotAssets;
    private boolean keyboardVisible;
    private RemoteUpdates.Store updateStore;
    private byte[] updateTrust;
    private String updateBridge="";
    private RemoteUpdates.Candidate updateCandidate,downloadedCandidate;
    private byte[] downloadedArchive;
    private final android.os.Handler updateHandler=new android.os.Handler(android.os.Looper.getMainLooper());
    private volatile long documentGeneration;
    private final RendererRecovery rendererRecovery=new RendererRecovery();
    private long retirementRecoveryGeneration;
    private final java.util.Set<WebView> destroyedRendererViews=java.util.Collections.newSetFromMap(new java.util.WeakHashMap<WebView,Boolean>());
    private String documentRevision="bundled",documentTicket="";
    private RemoteUpdates.Boot updateBoot;
    private boolean documentReady,documentInitializing,foreground,updatesBusy,waitingForComposition,restoreNavigationState,restoreRecoveredUI,skipNextUIRestore;
    private final Runnable foregroundUpdateCheck=new Runnable(){public void run(){if(foreground){checkUpdates(false);updateHandler.postDelayed(this,6*60*60*1000L);}}};
    private java.util.Map<String,byte[]> currentSnapshot;
    private java.nio.file.Path navigationState;
    private final java.net.URI updateFeed=java.net.URI.create(RemoteUpdates.DEFAULT_FEED);

    private final String[] renderers = {"auto", "dom", "webgl"};
    private static JSONObject notificationConnection;
    private JSONObject pendingNotificationStart, pendingNotificationRoute;
    private int notificationRouteAttempts;

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
        initializeUpdates();
        if (Build.VERSION.SDK_INT >= 33) getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::handleBack);
        showConnection();
        receiveTaskNotification(getIntent());
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
        if(terminal==null)return;WebView previous=terminal;terminal=null;root.removeView(previous);
        // Keep a terminating view alive until its renderer-gone callback confirms retirement.
        if(!rendererRecovery.isRetiring(previous))destroyRendererView(previous);
    }
    private void destroyRendererView(WebView view){
        if(view==null||!destroyedRendererViews.add(view))return;
        root.removeView(view);view.destroy();
    }

    private void showConnection() {
        if(saveConnectionDraft!=null){saveConnectionDraft.run();saveConnectionDraft=null;}
        invalidateDocument();
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
        String selectedUI=preferences.getBoolean("ui-bundled-only",false)?"bundled":hotAssets.getRevision().isEmpty()?"bundled":hotAssets.getRevision();
        try{if(!preferences.getBoolean("ui-bundled-only",false)&&updateStore!=null&&updateStore.active()!=null)selectedUI=updateStore.active();}catch(Exception ignored){}
        uiVersion.setText("UI revision: "+selectedUI);
        uiVersion.setTextColor(Color.LTGRAY);
        form.addView(uiVersion);
        button(form,"Check UI updates",view->checkUpdates(true));
        button(form,"Restore previous UI",view->rollbackUpdate());
        button(form,"Use bundled UI",view->confirmBundledUI());
        boolean personalDefaults = !defaults.optString("token").isEmpty();
        EditText url = input(form, "服务器地址", personalDefaults ? defaults.optString("endpoint", "https://117.50.48.22") : preferences.getString("url", defaults.optString("endpoint", "https://117.50.48.22")), false);
        EditText code = input(form, "房间号（可选，测试后自动列出）", personalDefaults ? defaults.optString("code") : preferences.getString("code", defaults.optString("code")), false);
        EditText token = input(form, "访问令牌", savedOrDefaultToken(url.getText().toString()), true);
        final SavedConnectionTokens.EditState tokenEdits=new SavedConnectionTokens.EditState();
        android.widget.CheckBox rememberToken = new android.widget.CheckBox(this);
        rememberToken.setText("保存 Token（返回页面和重启后自动填写）");
        rememberToken.setTextColor(Color.WHITE);
        rememberToken.setChecked(preferences.getBoolean("remember-server-token",true));
        form.addView(rememberToken);
        saveConnectionDraft=()->{
            preferences.edit().putBoolean("remember-server-token",rememberToken.isChecked()).apply();
            try {
                String target=url.getText().toString().trim(),value=token.getText().toString();
                SavedConnectionTokens.EditState.Action action=tokenEdits.action(rememberToken.isChecked(),value);
                if(action==SavedConnectionTokens.EditState.Action.SAVE)SavedConnectionTokens.save(this,target,value);
                else if(action==SavedConnectionTokens.EditState.Action.REMOVE&&!SavedConnectionTokens.remove(this,target))throw new IllegalStateException("Unable to forget credential");
                else if(action==SavedConnectionTokens.EditState.Action.KEEP)return;
                tokenEdits.committed();
            }catch(IllegalArgumentException incomplete){ }
            catch(Exception error){android.widget.Toast.makeText(this,"Token 未能保存，请重试。",android.widget.Toast.LENGTH_SHORT).show();}
        };
        token.addTextChangedListener(new android.text.TextWatcher(){
            public void beforeTextChanged(CharSequence text,int start,int count,int after){ }
            public void onTextChanged(CharSequence text,int start,int before,int count){tokenEdits.changed();}
            public void afterTextChanged(android.text.Editable text){ }
        });
        rememberToken.setOnCheckedChangeListener((button,checked)->{if(!checked)tokenEdits.forget();saveConnectionDraft.run();});
        Spinner renderer = new Spinner(this);
        renderer.setAdapter(new ArrayAdapter<String>(this, android.R.layout.simple_spinner_dropdown_item,
                new String[]{"自动选择渲染器", "默认渲染（DOM）", "WebGL 渲染（GPU）"}));
        String selected = preferences.getString("renderer", "auto");
        for (int i = 0; i < renderers.length; i++) if (renderers[i].equals(selected)) renderer.setSelection(i);
        form.addView(renderer);
        TextView help = new TextView(this);
        help.setText("测试连接会列出同一 Token 可访问的房间，默认全选。保存的 Token 使用系统密钥加密，仅自动填入对应服务器。");
        help.setTextColor(Color.LTGRAY);
        form.addView(help);
        TextView probeStatus = new TextView(this);
        probeStatus.setTextColor(Color.LTGRAY);
        probeStatus.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        Button probe = new Button(this);
        probe.setText("测试连接");
        form.addView(probe);
        form.addView(probeStatus);
        LinearLayout roomList = new LinearLayout(this);
        roomList.setOrientation(LinearLayout.VERTICAL);
        form.addView(roomList);
        final java.util.ArrayList<ConnectionProbe.Room> availableRooms = new java.util.ArrayList<>();
        final java.util.ArrayList<android.widget.CheckBox> roomChecks = new java.util.ArrayList<>();
        final String[] selectionScope = {null};
        final ConnectionProbe.ProbeResult[] verifiedDirect={null};
        final int[] probeScopeGeneration={0};
        Button connectButton = new Button(this);
        connectButton.setText("连接");
        form.addView(connectButton);
        android.text.TextWatcher invalidateRooms = new android.text.TextWatcher() {
            public void beforeTextChanged(CharSequence text,int start,int count,int after) { }
            public void onTextChanged(CharSequence text,int start,int before,int count) {
                probeScopeGeneration[0]++;verifiedDirect[0]=null;selectionScope[0]=null;availableRooms.clear();roomChecks.clear();roomList.removeAllViews();
            }
            public void afterTextChanged(android.text.Editable text) { }
        };
        url.addTextChangedListener(invalidateRooms);code.addTextChangedListener(invalidateRooms);token.addTextChangedListener(invalidateRooms);
        url.addTextChangedListener(new android.text.TextWatcher(){
            public void beforeTextChanged(CharSequence text,int start,int count,int after){ }
            public void onTextChanged(CharSequence text,int start,int before,int count){tokenEdits.beginLoad();try{token.setText(savedOrDefaultToken(text.toString()));}finally{tokenEdits.endLoad();}}
            public void afterTextChanged(android.text.Editable text){ }
        });
        probe.setOnClickListener(view -> {
            saveConnectionDraft.run();
            final String endpoint = url.getText().toString().trim();
            final String relayCode = code.getText().toString().trim();
            final String accessToken = token.getText().toString();
            final int probeGeneration=probeScopeGeneration[0];
            verifiedDirect[0]=null;selectionScope[0]=null;availableRooms.clear();roomChecks.clear();roomList.removeAllViews();
            probe.setEnabled(false);
            connectButton.setEnabled(false);
            probeStatus.setText("正在测试连接并查询可用房间…");
            new Thread(() -> {
                boolean direct = false;
                try { String host=Uri.parse(ConnectionEndpoint.normalize(endpoint)).getHost();direct=relayCode.isEmpty()&&(host.equals("localhost")||host.equals("::1")||host.equals("[::1]")||host.matches("127[.][0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}")); } catch(Exception ignored) { }
                final ConnectionProbe.DiscoveryResult discovered = direct ? null : ConnectionProbe.discover(endpoint, accessToken);
                // A discovery rejection is never proof of direct access: authenticate a real HELLO.
                final ConnectionProbe.ProbeResult directResult = direct||(relayCode.isEmpty()&&!discovered.success)?ConnectionProbe.verifyDirect(endpoint,accessToken):null;
                final String result = direct?directResult.message:discovered.success?discovered.message:directResult!=null&&directResult.success?directResult.message:relayCode.isEmpty()?discovered.message:ConnectionProbe.test(endpoint, relayCode, accessToken);
                runOnUiThread(() -> {
                    if (isFinishing() || isDestroyed() || formGeneration != connectionFormGeneration) return;
                    probe.setEnabled(true);
                    connectButton.setEnabled(true);
                    boolean unchanged = probeGeneration==probeScopeGeneration[0]&&endpoint.equals(url.getText().toString().trim())
                            && relayCode.equals(code.getText().toString().trim())
                            && accessToken.equals(token.getText().toString());
                    probeStatus.setText(unchanged ? result : "连接参数已修改，请重新测试。");
                    if(unchanged&&directResult!=null&&directResult.success){verifiedDirect[0]=directResult;probeStatus.setText(result+" 已验证直接连接。");}
                    else if(unchanged && discovered!=null && discovered.success) {
                        selectionScope[0]=endpoint+"\n"+accessToken;
                        availableRooms.addAll(discovered.rooms);
                        for(ConnectionProbe.Room room:availableRooms) {
                            android.widget.CheckBox check = new android.widget.CheckBox(this);
                            check.setText((room.machine.isEmpty()?room.code:room.machine)+"（"+room.code+"）"+(room.project.isEmpty()?"":" · "+room.project));
                            check.setTextColor(Color.WHITE);check.setChecked(true);roomChecks.add(check);roomList.addView(check);
                        }
                        probeStatus.setText("测试成功，发现 "+availableRooms.size()+" 个可用房间，已默认全选。");
                    }
                });
            }, "connection-probe").start();
        });
        connectButton.setOnClickListener(view -> {
            try {
                saveConnectionDraft.run();
                Uri endpoint = validEndpoint(url.getText().toString().trim());
                String mode = renderers[renderer.getSelectedItemPosition()];
                String relayCode = code.getText().toString().trim();
                JSONArray selectedRooms = null;
                if(selectionScope[0]!=null && selectionScope[0].equals(url.getText().toString().trim()+"\n"+token.getText().toString())) {
                    selectedRooms = new JSONArray();
                    for(int i=0;i<availableRooms.size();i++)if(roomChecks.get(i).isChecked()) {
                        ConnectionProbe.Room room=availableRooms.get(i);JSONObject item=new JSONObject();
                        item.put("code",room.code);item.put("machine",room.machine);item.put("project",room.project);selectedRooms.put(item);
                    }
                    if(selectedRooms.length()==0){probeStatus.setText("请至少勾选一个房间。");return;}
                    relayCode=selectedRooms.getJSONObject(0).getString("code");
                } else if(relayCode.isEmpty()&&!isLoopbackEndpoint(endpoint)&&(verifiedDirect[0]==null||!verifiedDirect[0].authorizesDirect(url.getText().toString().trim(),relayCode,token.getText().toString()))) {
                    probeStatus.setText("请先点击测试连接并勾选房间。");return;
                }
                preferences.edit().putString("url", endpoint.toString()).putString("code", relayCode)
                        .putString("renderer", mode).apply();
                connection = new JSONObject();
                connection.put("endpoint", endpoint.toString());
                connection.put("code", relayCode);
                connection.put("token", token.getText().toString());
                connection.put("renderer", mode);
                if(selectedRooms!=null)connection.put("selectedRooms",selectedRooms);
                localBase = LocalAssets.originForEndpoint(endpoint.toString());
                saveConnectionDraft=null;
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
        button(form, "任务完成通知", view -> {
            try {
                JSONObject target=new JSONObject();target.put("endpoint",validEndpoint(url.getText().toString().trim()).toString());
                target.put("code",code.getText().toString().trim());target.put("token",token.getText().toString());
                target.put("renderer",renderers[renderer.getSelectedItemPosition()]);showTaskNotifications(target);
            } catch(Exception error) { url.setError("请先填写有效的连接参数"); }
        });
    }

    private void showTaskNotifications(JSONObject target) {
        if(target==null){new AlertDialog.Builder(this).setMessage("请先连接一台电脑，再启用任务通知。").setPositiveButton("确定",null).show();return;}
        LinearLayout content=new LinearLayout(this);content.setOrientation(LinearLayout.VERTICAL);content.setPadding(24,12,24,12);
        TextView info=new TextView(this);info.setText(TaskNotifications.status(this)+"\n\n复用当前中继，不需要付费推送。启用后显示常驻连接通知；系统省电、强制停止或断网可能延迟提醒。只检测明确的任务状态，不把停止刷屏当作完成。");content.addView(info);
        android.widget.CheckBox legacy=new android.widget.CheckBox(this);legacy.setText("兼容旧版 Windows 后台（保留现有终端）");legacy.setChecked(preferences.getBoolean("task-notification-legacy",true));content.addView(legacy);
        new AlertDialog.Builder(this).setTitle("任务完成通知").setView(content)
            .setPositiveButton("启用通知",(dialog,which)->{
                try{JSONObject config=new JSONObject(target.toString());preferences.edit().putBoolean("task-notification-legacy",legacy.isChecked()).apply();
                    if(legacy.isChecked()&&!config.optString("code").isEmpty())config.put("notificationCode",config.optString("code")+"-events");
                    requestTaskNotifications(config);
                }catch(Exception error){android.widget.Toast.makeText(this,"通知配置无效",android.widget.Toast.LENGTH_LONG).show();}
            }).setNeutralButton("停止通知",(dialog,which)->{TaskNotifications.stop(this);notificationConnection=null;})
            .setNegativeButton("取消",null).show();
    }
    private void requestTaskNotifications(JSONObject config) {
        if(Build.VERSION.SDK_INT>=33&&checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)!=android.content.pm.PackageManager.PERMISSION_GRANTED){pendingNotificationStart=config;requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS},72);return;}
        try{TaskNotifications.start(this,config);notificationConnection=new JSONObject(config.toString());android.widget.Toast.makeText(this,"通知连接已启动，可在通知栏查看状态",android.widget.Toast.LENGTH_LONG).show();}
        catch(Exception error){android.widget.Toast.makeText(this,"无法启动通知连接，请重新打开应用后重试",android.widget.Toast.LENGTH_LONG).show();}
    }
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] grants) {
        super.onRequestPermissionsResult(request,permissions,grants);
        if(request!=72)return;JSONObject config=pendingNotificationStart;pendingNotificationStart=null;
        if(grants.length>0&&grants[0]==android.content.pm.PackageManager.PERMISSION_GRANTED&&config!=null)requestTaskNotifications(config);
        else android.widget.Toast.makeText(this,"未授予通知权限，终端仍可正常使用",android.widget.Toast.LENGTH_LONG).show();
    }
    private void receiveTaskNotification(Intent intent) {
        if(intent==null||intent.getStringExtra("task_session")==null)return;
        try{
            String endpoint=intent.getStringExtra("task_endpoint"),code=intent.getStringExtra("task_code");
            if(connection==null&&notificationConnection!=null)connection=new JSONObject(notificationConnection.toString());
            if(connection==null||!connection.optString("endpoint").equals(endpoint)||!connection.optString("code").equals(code==null?"":code)){
                new AlertDialog.Builder(this).setTitle("任务通知").setMessage("请先连接通知对应的电脑。不会自动创建终端或发送指令。").setPositiveButton("确定",null).show();return;
            }
            pendingNotificationRoute=new JSONObject();pendingNotificationRoute.put("endpoint",endpoint);pendingNotificationRoute.put("code",code);pendingNotificationRoute.put("session",intent.getStringExtra("task_session"));notificationRouteAttempts=0;
            if(terminal==null){localBase=LocalAssets.originForEndpoint(endpoint);openTerminal();}else deliverTaskNotification();
        }catch(Exception ignored){}
    }
    private void deliverTaskNotification() {
        if(pendingNotificationRoute==null||!foreground)return;
        if(++notificationRouteAttempts>15){pendingNotificationRoute=null;new AlertDialog.Builder(this).setTitle("任务已结束").setMessage("对应终端已关闭或暂不可用。不会创建新会话，请在终端列表中检查。").setPositiveButton("确定",null).show();return;}
        if(!trustedDocument()||!documentReady){updateHandler.postDelayed(this::deliverTaskNotification,1000);return;}
        final JSONObject route=pendingNotificationRoute;
        terminal.evaluateJavascript("!!window.MobileTerminal?.openNotification?.("+route.toString()+")",result->{if(pendingNotificationRoute!=route)return;if("true".equals(result))pendingNotificationRoute=null;else updateHandler.postDelayed(this::deliverTaskNotification,1000);});
    }

    static Uri validEndpoint(String value) {
        return Uri.parse(ConnectionEndpoint.normalize(value));
    }

    private static boolean isLoopbackEndpoint(Uri endpoint) {
        String host=endpoint.getHost();
        return host!=null&&(host.equalsIgnoreCase("localhost")||host.equals("::1")||host.equals("[::1]")||host.matches("127[.][0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}"));
    }

    private String savedOrDefaultToken(String endpoint) {
        if(!preferences.getBoolean("remember-server-token",true))return "";
        String saved=SavedConnectionTokens.load(this,endpoint);
        if(!saved.isEmpty())return saved;
        try {
            String preset=defaults.optString("endpoint","https://117.50.48.22");
            if(SavedConnectionTokens.canonicalEndpoint(endpoint).equals(SavedConnectionTokens.canonicalEndpoint(preset)))return defaults.optString("token");
        }catch(Exception invalid){ }
        return "";
    }

    private boolean trustedDocument() {
        return terminal != null && terminal.getUrl() != null && localBase != null
                && UpdateDocument.document(localBase, terminal.getUrl(),documentRevision);
    }

    private boolean navigation(Uri target) {
        if (localBase == null || !(localBase.equals(target.getScheme() + "://" + target.getAuthority()))) return true;
        if ("/settings".equals(target.getPath())) { showConnection(); return true; }
        if ("/reload".equals(target.getPath())) { openTerminal(); return true; }
        if ("/notifications".equals(target.getPath())) { showTaskNotifications(connection); return true; }
        return !UpdateDocument.document(localBase, target.toString(),documentRevision);
    }

    private WebResourceResponse blocked() {
        return new WebResourceResponse("text/plain", "UTF-8", 404, "Not Found", null, new ByteArrayInputStream(new byte[0]));
    }

    private void openTerminal() { openTerminal(false); }
    private void openTerminal(boolean updating) {
        if(rendererRecovery.waitingForRenderer()){updateMessage("The previous renderer is still retiring. Reconnect after retirement completes.");return;}
        if(connection==null&&!updating){showConnection();return;}
        connectionFormGeneration++;
        try{
            hotAssets=HotAssets.load(getExternalFilesDir(null));
            if(connection!=null&&connection.has("selectedRooms")&&!supportsRoomSelection(verifiedSnapshotForOpen())) {
                if(updateStore!=null)updateStore.useBundled();
                preferences.edit().putBoolean("ui-bundled-only",true).commit();
            }
            final java.util.Map<String,byte[]> snapshot=verifiedSnapshotForOpen();currentSnapshot=snapshot;
            String active=updateStore==null||preferences.getBoolean("ui-bundled-only",false)?null:updateStore.active();
            documentRevision=active==null?(hotAssets.getRevision().isEmpty()?"bundled":hotAssets.getRevision()):active;
            if(preferences.getBoolean("ui-bundled-only",false)||updateStore!=null&&active==null&&updateStore.highest()>0)documentRevision="bundled";
            updateBoot=updateStore==null?null:updateStore.beginBoot();
            documentTicket=java.util.UUID.randomUUID().toString();documentReady=false;documentInitializing=false;
            restoreNavigationState=!skipNextUIRestore&&(updating||restoreRecoveredUI);skipNextUIRestore=false;
            final long generation=++documentGeneration;
            final String revision=documentRevision,ticket=documentTicket;
            final RemoteUpdates.Boot boot=updateBoot;
            JSONObject pageConnection=connection==null?new JSONObject():new JSONObject(connection.toString());
            if(connection==null){String endpoint=preferences.getString("url","https://ui.supermono.local");try{ConnectionEndpoint.validate(endpoint);}catch(Exception invalid){endpoint="https://ui.supermono.local";}pageConnection.put("endpoint",endpoint);pageConnection.put("code",preferences.getString("code",""));pageConnection.put("token","");pageConnection.put("renderer","dom");}
            pageConnection.put("uiRevision",revision);localBase=LocalAssets.originForEndpoint(pageConnection.getString("endpoint"));
            navigationState=getFilesDir().toPath().resolve("remote-ui").resolve("navigation-"+RemoteUpdates.hash((pageConnection.optString("endpoint")+"\n"+pageConnection.optString("code")).getBytes("UTF-8"))+".json");
            if(!updating||terminal==null){destroyTerminal();root.removeAllViews();terminal=new WebView(this);root.addView(terminal,new LinearLayout.LayoutParams(-1,-1));}
            final WebView view=terminal;
            rendererRecovery.attach(view,generation,boot!=null&&!boot.ticket.isEmpty());
            terminal.setBackgroundColor(Color.rgb(12,16,23));WebSettings settings=terminal.getSettings();
            settings.setJavaScriptEnabled(true);settings.setDomStorageEnabled(true);settings.setTextZoom(100);settings.setSupportZoom(false);settings.setBuiltInZoomControls(false);settings.setDisplayZoomControls(false);settings.setCacheMode(WebSettings.LOAD_NO_CACHE);settings.setAllowFileAccess(false);settings.setAllowContentAccess(false);settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);settings.setSupportMultipleWindows(false);settings.setMediaPlaybackRequiresUserGesture(true);CookieManager.getInstance().setAcceptThirdPartyCookies(terminal,false);
            final String base=localBase;
            terminal.setWebViewClient(new WebViewClient(){
                @Override public WebResourceResponse shouldInterceptRequest(WebView web,WebResourceRequest request){
                    String asset=UpdateDocument.asset(base,request.getUrl().toString(),revision);
                    if(asset==null||!"GET".equals(request.getMethod())||(asset.endsWith(".html")&&!request.isForMainFrame()))return blocked();
                    try{byte[] bytes=snapshot.get(asset);if(bytes==null)return blocked();
                        if(asset.equals("mobile/index.html"))bytes=UpdateDocument.html(bytes,updateBridge,ticket,revision,generation,base).getBytes("UTF-8");
                        java.util.Map<String,String> headers=new java.util.HashMap<>();headers.put("Cache-Control","no-store");headers.put("X-Content-Type-Options","nosniff");headers.put("X-Frame-Options","DENY");
                        return new WebResourceResponse(LocalAssets.mime(asset),"UTF-8",200,"OK",headers,new ByteArrayInputStream(bytes));
                    }catch(Exception error){return blocked();}
                }
                @Override public boolean shouldOverrideUrlLoading(WebView web,WebResourceRequest request){
                    if("sma-update".equals(request.getUrl().getScheme())){
                        String action=UpdateDocument.action(base,web.getUrl(),request.getUrl().toString(),request.isForMainFrame(),ticket,revision,generation);
                        if(web==terminal&&generation==documentGeneration&&action!=null)updateAction(action,generation,boot);return true;
                    }
                    return !request.isForMainFrame()||navigation(request.getUrl());
                }
                @Override public boolean shouldOverrideUrlLoading(WebView web,String url){return true;}
                @Override public void onReceivedError(WebView web,WebResourceRequest request,WebResourceError error){
                    if(web==terminal&&request.isForMainFrame()&&generation==documentGeneration)startupFailure(generation,boot,"UI document failed to load");
                }
                @Override public void onReceivedHttpError(WebView web,WebResourceRequest request,WebResourceResponse response){
                    if(web==terminal&&request.isForMainFrame()&&generation==documentGeneration)startupFailure(generation,boot,"UI document failed to load");
                }
                @Override public boolean onRenderProcessGone(WebView web,RenderProcessGoneDetail detail){
                    rendererGone(web,generation,boot);return true;
                }
            });
            terminal.loadUrl(base+"/mobile/index.html?ui_revision="+revision+"#"+Uri.encode(pageConnection.toString()));
            if(!foreground)terminal.onPause();
            if(boot!=null&&!boot.ticket.isEmpty())updateHandler.postDelayed(()->{if(generation==documentGeneration&&!documentReady)startupFailure(generation,boot,"UI startup deadline exceeded",true);},20000);
        }catch(Exception error){try{failPendingStartup(updateBoot);}catch(Exception ignored){}showLoadError("Unable to load verified UI. Restore the previous UI or use bundled assets.");}
    }

    private java.util.Map<String,byte[]> verifiedSnapshotForOpen() throws Exception {
        try{return effectiveSnapshot();}catch(Exception invalidSnapshot){
            // Only failed snapshot verification can consume a previous pointer here.
            // A WebView lifecycle error must never downgrade an already verified good UI.
            if(updateStore==null||updateStore.active()==null)throw invalidSnapshot;
            updateStore.rollback();return effectiveSnapshot();
        }
    }
    private void initializeUpdates(){
        try{updateTrust=RemoteUpdates.read(getAssets().open("update-trust.json"),RemoteUpdates.MANIFEST_LIMIT);updateBridge=new String(RemoteUpdates.read(getAssets().open("update-bridge.js"),128*1024),"UTF-8");
            updateStore=new RemoteUpdates.Store(getFilesDir().toPath().resolve("remote-ui"),updateTrust,updateFeed);restoreRecoveredUI=updateStore.recoveredOnStart();
        }catch(Exception error){updateStore=null;}
    }
    private static boolean supportsRoomSelection(java.util.Map<String,byte[]> snapshot) throws Exception {
        byte[] script=snapshot.get("mobile/app.js");
        return script!=null&&new String(script,"UTF-8").contains("SMA_ROOM_SELECTION_V1");
    }

    private java.util.Map<String,byte[]> effectiveSnapshot() throws Exception {
        boolean forced=preferences.getBoolean("ui-bundled-only",false);
        if(!forced&&updateStore!=null&&updateStore.active()!=null)return updateStore.snapshot(updateStore.active());
        boolean bundled=forced||updateStore!=null&&updateStore.highest()>0;
        java.util.Map<String,byte[]> files=new java.util.LinkedHashMap<>();
        for(String asset:RemoteUpdates.FILES){java.io.InputStream stream=!bundled&&hotAssets!=null?hotAssets.open(asset):null;if(stream==null)stream=getAssets().open(asset);files.put(asset,RemoteUpdates.read(stream,RemoteUpdates.FILE_LIMIT));}return files;
    }
    private void updateAction(String action,long generation,RemoteUpdates.Boot boot){
        if(action.equals("check")){checkUpdates(true);return;}if(action.equals("install")){confirmUpdate();return;}if(action.equals("rollback")){rollbackUpdate();return;}
        if(action.equals("fail")){if(!documentReady)startupFailure(generation,boot,"UI script startup failed");return;}
        if(action.equals("ready")&&!documentReady&&!documentInitializing){documentInitializing=true;
            String state="null";try{if(restoreNavigationState&&navigationState!=null&&java.nio.file.Files.exists(navigationState))state=new String(UpdateDocument.state(RemoteUpdates.read(java.nio.file.Files.newInputStream(navigationState),512*1024)),"UTF-8");}catch(Exception ignored){}
            final String restore=state;terminal.evaluateJavascript("(()=>{try{if(window.NativeUpdates){NativeUpdates.restore("+restore+");NativeUpdates.uiReady();}return true;}catch(e){return false;}})()",result->{
                if(generation!=documentGeneration)return;if(!"true".equals(result)){documentReady=false;documentInitializing=false;startupFailure(generation,boot,"UI state restore failed");return;}
                try{if(boot!=null&&!boot.ticket.isEmpty()&&!updateStore.acknowledge(boot.ticket,boot.revision,boot.generation))throw new IOException("Stale startup ticket");if(!rendererRecovery.ready(terminal,generation))throw new IOException("Obsolete renderer readiness");updateBoot=null;documentReady=true;documentInitializing=false;restoreRecoveredUI=false;if(!foreground){lifecycle("pause");if(terminal!=null)terminal.onPause();}}catch(Exception error){documentReady=false;documentInitializing=false;startupFailure(generation,boot,"Unable to commit UI startup");}
            });
        }
    }
    private void failPendingStartup(RemoteUpdates.Boot boot) throws Exception {
        if(updateBoot==boot)updateBoot=null;
        if(updateStore!=null&&boot!=null&&!boot.ticket.isEmpty())updateStore.fail(boot.ticket);
    }
    private void startupFailure(long generation,RemoteUpdates.Boot boot,String reason){startupFailure(generation,boot,reason,false);}
    private void startupFailure(long generation,RemoteUpdates.Boot boot,String reason,boolean hung){
        if(generation!=documentGeneration||isFinishing()||isDestroyed())return;
        final WebView failed=terminal;
        RendererRecovery.Action action=rendererRecovery.failure(failed,generation,hung);
        if(action==RendererRecovery.Action.IGNORE){if(rendererRecovery.matches(failed,generation)&&!documentReady)showLoadError(reason+". Server terminal processes continue running.");return;}
        try{failPendingStartup(boot);}catch(Exception error){showLoadError("Unable to restore the previous UI. Server terminals continue running.");return;}
        documentGeneration++;documentReady=false;documentInitializing=false;
        if(action==RendererRecovery.Action.RELOAD_HEALTHY){openTerminal(true);return;}
        retirementRecoveryGeneration=documentGeneration;
        // Never create a new WebView in a renderer that has only been ASKED to terminate.
        if(terminal==failed){root.removeView(failed);terminal=null;}
        retireFailedRenderer(failed,generation);
    }
    private void retireFailedRenderer(WebView failed,long generation){
        if(Build.VERSION.SDK_INT>=29){
            try{android.webkit.WebViewRenderProcess process=failed.getWebViewRenderProcess();if(process!=null&&process.terminate())return;}catch(RuntimeException ignored){}
            if(rendererRecovery.isRetiring(failed)){
                rendererRecovery.finishWithoutTermination(failed,generation);destroyRendererView(failed);
                showLoadError("Renderer termination is unavailable. The previous verified UI is preserved; reopen the app to retry.");
            }
            return;
        }
        // API26-28: no platform termination handle. This is explicit best-effort recreation.
        RendererRecovery.Action action=rendererRecovery.finishWithoutTermination(failed,generation);destroyRendererView(failed);
        if(action==RendererRecovery.Action.RETIRED_RECOVER&&documentGeneration==retirementRecoveryGeneration&&!isFinishing()&&!isDestroyed())openTerminal(true);
    }
    private void rendererGone(WebView web,long generation,RemoteUpdates.Boot boot){
        RendererRecovery.Action action=rendererRecovery.rendererGone(web,generation);
        if(action==RendererRecovery.Action.RETIRED_RECOVER||action==RendererRecovery.Action.RETIRED_CANCELLED){
            destroyRendererView(web);
            if(action==RendererRecovery.Action.RETIRED_RECOVER&&documentGeneration==retirementRecoveryGeneration&&!isFinishing()&&!isDestroyed())openTerminal(true);
            return;
        }
        if(action==RendererRecovery.Action.IGNORE||generation!=documentGeneration||web!=terminal){if(web!=terminal)destroyRendererView(web);return;}
        terminal=null;destroyRendererView(web);
        try{
            if(action==RendererRecovery.Action.FAIL_PENDING)failPendingStartup(boot);
            // RECREATE_GOOD retains the current verified pointer. No second rollback.
            documentGeneration++;documentReady=false;documentInitializing=false;
            if(!isFinishing()&&!isDestroyed())openTerminal(true);
        }catch(Exception error){showLoadError("UI renderer stopped. Server terminal processes continue running.");}
    }
    private void checkUpdates(boolean manual){
        if(updateStore==null){if(manual)updateMessage("UI updates unavailable: install a bootstrap APK with its public trust anchor.");return;}
        if(updatesBusy)return;long now=System.currentTimeMillis();if(!manual&&now-preferences.getLong("ui-update-check",0)<6*60*60*1000L)return;
        preferences.edit().putLong("ui-update-check",now).apply();updatesBusy=true;
        new Thread(()->{try{byte[] envelope=RemoteUpdates.download(updateFeed,RemoteUpdates.MANIFEST_LIMIT);RemoteUpdates.Candidate candidate=updateStore.observe(envelope,System.currentTimeMillis()/1000);
            runOnUiThread(()->{updatesBusy=false;updateCandidate=candidate;if(!foreground)return;try{if(updateStore.available(candidate)){if(manual)confirmUpdate();else android.widget.Toast.makeText(this,"UI update available. Open Update to review it.",android.widget.Toast.LENGTH_LONG).show();}else if(manual)updateMessage("UI is current. Highest signed release is "+candidate.sequence+".");}catch(Exception error){if(manual)updateMessage("Release cannot be offered.");}});
        }catch(Exception error){runOnUiThread(()->{updatesBusy=false;if(manual&&foreground)updateMessage("Unable to check UI updates. HTTPS and signature verification remain enabled.");});}},"ui-update-check").start();
    }
    private void confirmUpdate(){
        final RemoteUpdates.Candidate candidate=updateCandidate;if(candidate==null)return;
        if(candidate.nativeApi!=RemoteUpdates.NATIVE_API){updateMessage("This UI requires a newer native app. Install a compatible APK first.");return;}
        new AlertDialog.Builder(this).setTitle("UI update available").setMessage("Release "+candidate.revision+". Your terminal processes keep running. Apply when your input is ready.")
            .setPositiveButton("Download and update",(dialog,which)->downloadUpdate(candidate)).setNegativeButton("Later",null).show();
    }
    private void downloadUpdate(RemoteUpdates.Candidate candidate){
        if(updatesBusy)return;updatesBusy=true;final long downloadGeneration=documentGeneration;
        if(downloadedArchive!=null&&downloadedCandidate!=null&&candidate.payloadHash.equals(downloadedCandidate.payloadHash)){showDownloadedUpdate(candidate,downloadedArchive,downloadGeneration);return;}
        new Thread(()->{try{byte[] archive=RemoteUpdates.download(candidate.url,RemoteUpdates.ARCHIVE_LIMIT);RemoteUpdates.unpack(candidate,archive);runOnUiThread(()->{downloadedCandidate=candidate;downloadedArchive=archive;showDownloadedUpdate(candidate,archive,downloadGeneration);});}
            catch(Exception error){runOnUiThread(()->{updatesBusy=false;updateMessage("Update rejected. Current UI is unchanged.");});}},"ui-update-download").start();
    }
    private void showDownloadedUpdate(RemoteUpdates.Candidate candidate,byte[] archive,long generation){
        if(isFinishing()||isDestroyed()||generation!=documentGeneration){updatesBusy=false;return;}
        new AlertDialog.Builder(this).setTitle("UI download verified").setMessage("Reload the UI now? Unsent drafts will be saved first; server terminals continue running.").setPositiveButton("Save drafts and reload",(dialog,which)->prepareUpdate(candidate,archive)).setNegativeButton("Later",(dialog,which)->updatesBusy=false).show();
    }
    private void confirmBundledUI(){
        new AlertDialog.Builder(this).setTitle("Use UI bundled with this APK?")
            .setMessage("First save the old developer UI drafts from EVERY window somewhere else. Its private draft maps cannot be migrated automatically. Confirming switches to the packaged UI; unsaved old drafts may be lost. USB snapshot files and their active pointer stay unchanged, so a developer can restore them later. The downloaded update remains unactivated.")
            .setPositiveButton("I saved drafts; use bundled UI",(dialog,which)->{try{invalidateDocument();if(updateStore!=null)updateStore.useBundled();preferences.edit().putBoolean("ui-bundled-only",true).commit();restoreRecoveredUI=false;skipNextUIRestore=true;openTerminal(true);}catch(Exception error){updateMessage("Unable to select the bundled UI. USB files are unchanged.");}})
            .setNegativeButton("Keep current UI",null).show();
    }
    private interface UpdateMutation {void run() throws Exception;}
    private void prepareUpdate(RemoteUpdates.Candidate candidate,byte[] archive){
        prepareNavigation(()->{updateStore.install(candidate.envelope,archive,currentSnapshot==null?effectiveSnapshot():currentSnapshot,System.currentTimeMillis()/1000);preferences.edit().putBoolean("ui-bundled-only",false).commit();},documentGeneration);
    }
    private void prepareNavigation(UpdateMutation mutation,long expectedGeneration){
        if(expectedGeneration!=documentGeneration||isFinishing()||isDestroyed()){updatesBusy=false;return;}
        if(!foreground){updateHandler.postDelayed(()->prepareNavigation(mutation,expectedGeneration),300);return;}
        final long generation=documentGeneration;
        android.webkit.ValueCallback<String> captured=value->{
            if(generation!=documentGeneration){updatesBusy=false;return;}
            if("\"unsupported\"".equals(value)){updatesBusy=false;new AlertDialog.Builder(this).setTitle("Developer UI cannot safely migrate drafts").setMessage("The downloaded update has NOT been activated. This older USB UI cannot export all window drafts. You can explicitly switch to the bundled UI after saving them elsewhere.").setPositiveButton("Review bundled recovery",(dialog,which)->confirmBundledUI()).setNegativeButton("Keep current UI",null).show();return;}
            if(value==null||value.equals("null")){if(!waitingForComposition){waitingForComposition=true;android.widget.Toast.makeText(this,"Finish composition or the pending send before updating.",android.widget.Toast.LENGTH_LONG).show();}updateHandler.postDelayed(()->prepareNavigation(mutation,expectedGeneration),300);return;}
            try{waitingForComposition=false;byte[] safe=UpdateDocument.state(value.getBytes("UTF-8"));
                final java.nio.file.Path statePath=navigationState==null?getFilesDir().toPath().resolve("remote-ui/navigation-empty.json"):navigationState;
                new Thread(()->{try{java.nio.file.Files.createDirectories(statePath.getParent());java.nio.file.Path temp=statePath.resolveSibling(statePath.getFileName()+".tmp");try(java.io.FileOutputStream stream=new java.io.FileOutputStream(temp.toFile())){stream.write(safe);stream.getFD().sync();}java.nio.file.Files.move(temp,statePath,java.nio.file.StandardCopyOption.REPLACE_EXISTING,java.nio.file.StandardCopyOption.ATOMIC_MOVE);
                    if(isFinishing()||isDestroyed()||generation!=documentGeneration)throw new IOException("Obsolete UI activation");mutation.run();runOnUiThread(()->{updatesBusy=false;if(!isFinishing()&&!isDestroyed()&&generation==documentGeneration)openTerminal(true);});
                }catch(Exception error){runOnUiThread(()->{updatesBusy=false;if(terminal!=null&&generation==documentGeneration)terminal.evaluateJavascript("MobileTerminal?.cancelUIReload?.()",null);updateMessage("Update activation rejected. Current UI is unchanged.");});}},"ui-update-install").start();
            }catch(Exception error){updatesBusy=false;if(terminal!=null)terminal.evaluateJavascript("MobileTerminal?.cancelUIReload?.()",null);updateMessage("UI state could not be safely saved. Update postponed.");}
        };
        if(terminal!=null&&trustedDocument())terminal.evaluateJavascript("window.MobileTerminal?.prepareUIReload ? MobileTerminal.prepareUIReload() : 'unsupported'",captured);
        else captured.onReceiveValue("{\"version\":1,\"hostId\":\"home\",\"activeSid\":\"\",\"drafts\":[],\"modes\":[]}");
    }
    private void rollbackUpdate(){
        if(updateStore==null)return;
        new AlertDialog.Builder(this).setMessage("Restore the previous local UI? Terminal processes keep running.")
            .setPositiveButton("Restore",(dialog,which)->{if(!updatesBusy){updatesBusy=true;prepareNavigation(()->updateStore.rollback(),documentGeneration);}}).setNegativeButton("Cancel",null).show();
    }
    private void invalidateDocument(){
        documentGeneration++;rendererRecovery.cancel();documentReady=false;documentInitializing=false;
        try{if(updateStore!=null&&updateBoot!=null&&!updateBoot.ticket.isEmpty())updateStore.fail(updateBoot.ticket);}catch(Exception ignored){}
        updateBoot=null;
    }
    private void updateMessage(String message){if(!isFinishing()&&!isDestroyed())new AlertDialog.Builder(this).setMessage(message).setPositiveButton("OK",null).show();}

    private void showLoadError(String message) {
        invalidateDocument();
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
        if(intent.getStringExtra("task_session")!=null){receiveTaskNotification(intent);return;}
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
            preferences.edit().putBoolean("ui-bundled-only",false).apply();
            if (connection != null) openTerminal(); else showConnection();
        }
    }

    @Override protected void onPause() {
        if(saveConnectionDraft!=null)saveConnectionDraft.run();
        foreground=false;
        updateHandler.removeCallbacks(foregroundUpdateCheck);
        lifecycle("pause");
        if (terminal != null) terminal.onPause();
        super.onPause();
    }
    @Override protected void onResume() {
        super.onResume();
        foreground=true;
        updateHandler.removeCallbacks(foregroundUpdateCheck);
        foregroundUpdateCheck.run();
        if (terminal != null) terminal.onResume();
        lifecycle("resume");
        deliverTaskNotification();
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
        invalidateDocument();foreground=false;
        updateHandler.removeCallbacksAndMessages(null);
        destroyTerminal();
        connection = null;
        localBase = null;
        super.onDestroy();
    }
}
