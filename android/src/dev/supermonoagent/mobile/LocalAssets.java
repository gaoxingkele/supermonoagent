package dev.supermonoagent.mobile;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

/** Exact packaged-file allowlist. Never decode caller-controlled filesystem paths. */
public final class LocalAssets {
    private static final Set<String> FILES = new HashSet<String>(Arrays.asList(
        "mobile/index.html", "mobile/app.js", "mobile/app.css", "mobile/mobile-core.js",
        "mobile/vendor/xterm.js", "mobile/vendor/xterm.css", "mobile/vendor/xterm-fit.js",
        "mobile/vendor/xterm-unicode11.js", "mobile/vendor/xterm-webgl.js", "mobile/vendor/terminal-client.js",
        "mobile/vendor/file-client.js", "mobile/vendor/file-upload.js",
        "mobile/agent-modes/codex.json", "mobile/agent-modes/claude-code.json", "mobile/agent-modes/grok.json",
        "mobile/agent-modes/kimi.json", "mobile/agent-modes/pi-dev.json", "mobile/agent-modes/cursor-cli.json"));
    private LocalAssets() { }

    /** Match Chromium's serialized origin, including default-port canonicalization. */
    public static String originForEndpoint(String value) {
        ConnectionEndpoint.validate(value);
        URI uri = URI.create(value);
        String scheme = uri.getScheme().toLowerCase(java.util.Locale.ROOT);
        String host = uri.getHost().toLowerCase(java.util.Locale.ROOT);
        if (host.indexOf(':') >= 0 && !host.startsWith("[")) host = "[" + host + "]";
        int port = uri.getPort();
        boolean defaultPort = port < 0 || ("http".equals(scheme) && port == 80)
                || ("https".equals(scheme) && port == 443);
        return scheme + "://" + host + (defaultPort ? "" : ":" + port);
    }

    public static String assetPath(String expectedOrigin, String value) {
        try {
            URI origin = new URI(expectedOrigin);
            URI uri = new URI(value);
            if (!("https".equals(origin.getScheme()) || "http".equals(origin.getScheme()))
                    || origin.getHost() == null || origin.getRawUserInfo() != null
                    || origin.getRawQuery() != null || origin.getRawFragment() != null
                    || !"".equals(origin.getRawPath())
                    || !origin.getScheme().equals(uri.getScheme())
                    || !origin.getRawAuthority().equals(uri.getRawAuthority())
                    || uri.getRawUserInfo() != null || uri.getRawQuery() != null) return null;
            String path = uri.getRawPath();
            if (path == null || !path.startsWith("/") || path.indexOf('%') >= 0) return null;
            path = path.substring(1);
            return FILES.contains(path) ? path : null;
        } catch (URISyntaxException error) { return null; }
    }

    public static boolean isDocument(String expectedOrigin, String value) {
        return "mobile/index.html".equals(assetPath(expectedOrigin, value));
    }

    public static String mime(String path) {
        if (path.endsWith(".json")) return "application/json";
        if (path.endsWith(".html")) return "text/html";
        if (path.endsWith(".css")) return "text/css";
        return "text/javascript";
    }
}
