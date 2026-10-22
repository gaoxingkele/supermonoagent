package dev.supermonoagent.mobile;

import android.app.*;
import android.content.*;
import android.content.pm.ServiceInfo;
import android.os.*;
import org.json.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/** Explicitly enabled, visible metadata monitor. No credential persistence or terminal feed. */
public final class TaskNotifications extends Service {
    public static final String ACTION_OPEN = "dev.supermonoagent.mobile.OPEN_TASK";
    private static final String STOP = "dev.supermonoagent.mobile.STOP_TASK_MONITOR";
    private static final String STATE = "terminal-monitor", EVENTS = "terminal-results";
    private static final int FOREGROUND = 4100;
    private static volatile String currentStatus = "未开启";
    private volatile boolean running;
    private volatile EventSocket active;
    private Thread worker;
    private volatile int generation;
    private static final class NotificationsBlocked extends Exception { }
    public static void start(Context context, JSONObject config) {
        context.startForegroundService(new Intent(context, TaskNotifications.class).putExtra("config", config.toString()));
    }
    public static void stop(Context context) {
        context.stopService(new Intent(context, TaskNotifications.class)); currentStatus = "已关闭";
    }
    public static String status(Context context) { return currentStatus; }
    @Override public IBinder onBind(Intent intent) { return null; }
    @Override public void onCreate() {
        super.onCreate();
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel(STATE, "终端任务监测", NotificationManager.IMPORTANCE_LOW));
        NotificationChannel results = new NotificationChannel(EVENTS, "终端任务结果", NotificationManager.IMPORTANCE_DEFAULT);
        results.setLockscreenVisibility(Notification.VISIBILITY_PRIVATE); manager.createNotificationChannel(results);
    }
    private Notification monitor(String text) {
        PendingIntent stop = PendingIntent.getService(this, 4101, new Intent(this, TaskNotifications.class).setAction(STOP),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, STATE).setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle("SuperMonoAgent 任务通知").setContentText(text).setOngoing(true)
                .setVisibility(Notification.VISIBILITY_PRIVATE).setOnlyAlertOnce(true)
                .setContentIntent(PendingIntent.getActivity(this, 4100, new Intent(this, MainActivity.class),
                        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE))
                .addAction(new Notification.Action.Builder(null, "停止监测", stop).build()).build();
    }
    private void state(String value) {
        currentStatus = value;
        try { getSystemService(NotificationManager.class).notify(FOREGROUND, monitor(value)); } catch (SecurityException ignored) { }
    }
    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null || STOP.equals(intent.getAction())) { stopSelf(); return START_NOT_STICKY; }
        JSONObject config;
        try {
            config = new JSONObject(intent.getStringExtra("config")); ConnectionEndpoint.validate(config.optString("endpoint"));
            if (config.optString("token").isEmpty()) throw new IllegalArgumentException();
        } catch (Exception error) { currentStatus = "连接参数无效"; stopSelf(); return START_NOT_STICKY; }
        stopWorker(); running = true; final int mine = ++generation;
        currentStatus = "正在连接任务通知";
        if (Build.VERSION.SDK_INT >= 34) startForeground(FOREGROUND, monitor(currentStatus), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(FOREGROUND, monitor(currentStatus));
        worker = new Thread(() -> runMonitor(config, mine), "terminal-task-notifications"); worker.start();
        return START_NOT_STICKY;
    }
    private void stopWorker() {
        running = false; EventSocket socket = active; if (socket != null) socket.close();
        if (worker != null) worker.interrupt();
    }
    @Override public void onDestroy() {
        stopWorker(); generation++; currentStatus = "已关闭";
        stopForeground(STOP_FOREGROUND_REMOVE); super.onDestroy();
    }
    private boolean alive(int mine) { return running && generation == mine && !Thread.currentThread().isInterrupted(); }
    private void runMonitor(JSONObject config, int mine) {
        int failures = 0;
        while (alive(mine)) {
            try (EventSocket socket = new EventSocket(config.optString("notificationEndpoint", config.optString("endpoint")))) {
                if (!alive(mine)) break; active = socket; listen(socket, config, mine);
            } catch (UnsupportedOperationException error) {
                if (alive(mine)) { state("电脑端尚不支持任务通知，请升级电脑端"); running = false; } break;
            } catch (NotificationsBlocked error) {
                if (!alive(mine)) break;
                state("通知权限或任务结果通知已关闭，请在系统设置中开启");
                try { Thread.sleep(30000); } catch (InterruptedException stopped) { Thread.currentThread().interrupt(); break; }
                continue;
            } catch (EventSocket.Closed error) {
                if (error.code == 4001 || error.code == 4003 || error.code == 1008) {
                    if (alive(mine)) { state("认证失败，监测已停止；请检查连接参数"); running = false; } break;
                }
            } catch (Exception error) {
                // Never log exceptions: supplied URLs and credentials may be embedded.
            } finally { if (generation == mine) active = null; }
            if (!alive(mine)) break; state("连接中断，正在重连");
            try { Thread.sleep(Math.min(60000L, 1000L << Math.min(failures++, 6)) + (long) (Math.random() * 1000)); }
            catch (InterruptedException stopped) { Thread.currentThread().interrupt(); break; }
        }
    }
    private void listen(EventSocket socket, JSONObject config, int mine) throws Exception {
        String token = config.optString("token"), code = config.optString("notificationCode", config.optString("code"));
        String scope = digest(config.optString("notificationEndpoint", config.optString("endpoint")) + "\n" + code + "\n" + token);
        SharedPreferences saved = getSharedPreferences("task-notification-cursors", MODE_PRIVATE);
        String after = saved.getString(scope, "");
        JSONArray recent = new JSONArray(saved.getString(scope + ":recent", "[]"));
        boolean ready = code.isEmpty(), subscribed = false;
        if (!ready) socket.json(new JSONObject().put("op", "phone").put("code", code).put("token", token).toString());
        else socket.protocol(1, new JSONObject().put("token", token).put("multiplex", true).toString());
        while (alive(mine)) {
            EventSocket.Message message = socket.receive();
            if (message.opcode == 1) {
                JSONObject relay = new JSONObject(new String(message.body, StandardCharsets.UTF_8));
                if (relay.has("error")) throw new EventSocket.Closed(4003);
                if (!ready && "ready".equals(relay.optString("op"))) {
                    ready = true; socket.protocol(1, new JSONObject().put("token", token).put("multiplex", true).toString());
                }
                continue;
            }
            byte[] bytes = message.body; if (bytes.length < 5) throw new IllegalArgumentException();
            long length = 0; for (int n = 0; n < 4; n++) length |= (long) (bytes[n] & 255) << (8 * n);
            if (length != bytes.length - 4) throw new IllegalArgumentException();
            int opcode = bytes[4] & 255;
            if (opcode == 10) { bytes[4] = 11; socket.send(2, bytes); continue; }
            if (opcode != 2 && opcode != 19) continue;
            JSONObject payload = new JSONObject(new String(bytes, 5, bytes.length - 5, StandardCharsets.UTF_8));
            if (opcode == 2 && !subscribed) {
                if (payload.has("error")) throw new EventSocket.Closed(4003);
                JSONObject capability = payload.optJSONObject("capabilities");
                if (capability == null || !capability.optBoolean("task_events")) throw new UnsupportedOperationException();
                JSONObject request = new JSONObject();
                if (saved.contains(scope)) request.put("after", after); else request.put("replay", false);
                socket.protocol(18, request.toString()); subscribed = true;
                state("正在监测任务结果 · 可在通知中停止");
            } else if (opcode == 19 && subscribed) {
                if ("cursor".equals(payload.optString("type"))) {
                    after = payload.optString("cursor"); saved.edit().putString(scope, after).apply(); continue;
                }
                String id = payload.optString("event_id"); if (id.isEmpty() || id.length() > 128) continue;
                boolean duplicate = id.equals(after);
                for (int n = 0; n < recent.length(); n++) duplicate |= id.equals(recent.optString(n));
                if (duplicate) continue;
                String status = payload.optString("status");
                if (!"completed".equals(status) && !"failed".equals(status) && !"waiting".equals(status)) continue;
                if (!showEvent(payload, config, scope)) throw new NotificationsBlocked();
                after = id;
                JSONArray next = new JSONArray();
                for (int n = Math.max(0, recent.length() - 127); n < recent.length(); n++) next.put(recent.get(n));
                next.put(id); recent = next;
                saved.edit().putString(scope, after).putString(scope + ":recent", recent.toString()).apply();
            }
        }
    }
    private boolean showEvent(JSONObject event, JSONObject config, String scope) {
        NotificationManager manager = getSystemService(NotificationManager.class);
        NotificationChannel channel = manager.getNotificationChannel(EVENTS);
        if (!manager.areNotificationsEnabled() || (channel != null && channel.getImportance() == NotificationManager.IMPORTANCE_NONE)) return false;
        String status = event.optString("status");
        String label = "failed".equals(status) ? "任务失败" : "waiting".equals(status) ? "等待确认" : "任务完成";
        String title = clean(event.optString("title", "终端"), 80), summary = clean(event.optString("summary", label), 600);
        String machine = clean(event.optString("machine", config.optString("hostName")), 40);
        String project = clean(event.optString("project"), 40);
        if (!project.isEmpty()) title = project + " · " + title;
        if (!machine.isEmpty()) title = machine + " · " + title;
        String key = scope + ":" + event.optString("event_id");
        Intent intent = new Intent(this, MainActivity.class).setAction(ACTION_OPEN)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .setData(android.net.Uri.parse("supermonoagent://task/" + digest(key)))
                .putExtra("task_session", event.optString("session")).putExtra("task_window", event.optString("window"))
                .putExtra("task_host_id", config.optString("hostId")).putExtra("task_endpoint", config.optString("endpoint"))
                .putExtra("task_code", config.optString("code"));
        PendingIntent tap = PendingIntent.getActivity(this, key.hashCode(), intent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification notification = new Notification.Builder(this, EVENTS).setSmallIcon(android.R.drawable.stat_notify_more)
                .setContentTitle(title + " · " + label).setContentText(summary).setStyle(new Notification.BigTextStyle().bigText(summary))
                .setVisibility(Notification.VISIBILITY_PRIVATE).setAutoCancel(true).setContentIntent(tap)
                .setPublicVersion(new Notification.Builder(this, EVENTS).setSmallIcon(android.R.drawable.stat_notify_more)
                        .setContentTitle("SuperMonoAgent").setContentText("收到终端任务结果").build()).build();
        try { manager.notify("task:" + scope, key.hashCode(), notification); return true; }
        catch (SecurityException denied) { return false; }
    }
    private static String clean(String text, int maximum) {
        String value = text.replaceAll("[\\p{Cntrl}&&[^\\n\\t]]", "").trim();
        return value.length() > maximum ? value.substring(0, maximum) + "…" : value;
    }
    private static String digest(String value) {
        try {
            byte[] bytes = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder result = new StringBuilder();
            for (byte item : bytes) result.append(String.format(java.util.Locale.ROOT, "%02x", item & 255)); return result.toString();
        } catch (Exception impossible) { throw new IllegalStateException(impossible); }
    }
}
