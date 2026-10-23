package dev.supermonoagent.mobile;

import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.Base64;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import javax.net.ssl.SSLException;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

/** A read-only protocol handshake. Run off the UI thread; never sends terminal commands. */
public final class ConnectionProbe {
    private static final int TIMEOUT = 5000;
    private static final int MAX_FRAME = 256 * 1024;
    private static final SecureRandom RANDOM = new SecureRandom();

    private ConnectionProbe() { }

    public static final class Room {
        public final String code, machine, project;
        private Room(String code, String machine, String project) {
            this.code = code; this.machine = machine; this.project = project;
        }
    }

    public static final class DiscoveryResult {
        public final boolean success;
        public final String message;
        public final List<Room> rooms;
        private DiscoveryResult(boolean success, String message, List<Room> rooms) {
            this.success = success; this.message = message;
            this.rooms = Collections.unmodifiableList(new ArrayList<>(rooms));
        }
    }

    /** Discover only this credential's online terminal rooms, without subscribing to output. */
    public static DiscoveryResult discover(String endpoint, String token) {
        try {
            final String credential = token == null ? "" : token;
            if (credential.isEmpty() || credential.length() > 4096) throw new IllegalArgumentException();
            return connected(new URI(ConnectionEndpoint.normalize(endpoint)),
                    (socket, target) -> discoverRooms(socket, target, credential));
        } catch (Exception error) {
            String message;
            if (error instanceof SocketTimeoutException) message = "测试失败：连接超时，请检查网络及主机是否在线。";
            else if (error instanceof SSLException) message = "测试失败：TLS 证书或安全连接验证失败。";
            else if (error instanceof IllegalArgumentException) message = "测试失败：请检查安全服务器地址和 Token。";
            else if (error instanceof ProbeFailure) message = "测试失败：" + error.getMessage();
            else message = "测试失败：无法获取房间，请检查网络和中继服务。";
            return new DiscoveryResult(false, message, Collections.<Room>emptyList());
        }
    }

    private static DiscoveryResult discoverRooms(Socket socket, URI endpoint, String token) throws Exception {
        InputStream input = socket.getInputStream(); OutputStream output = socket.getOutputStream();
        long deadline = System.nanoTime() + TIMEOUT * 1_000_000L;
        upgrade(socket, endpoint, input, output, deadline);
        writeFrame(output, 1, ("{\"op\":\"watch\",\"rooms\":[],\"tokens\":[" + quote(token) + "]}").getBytes(StandardCharsets.UTF_8));
        Frame frame = readData(socket, input, output, deadline);
        if (frame.opcode != 1) throw new ProbeFailure("中继目录响应无效。", 0);
        Map<?, ?> directory = object(frame.body);
        Object capabilities = directory.get("capabilities"), hosts = directory.get("hosts"), revision = directory.get("revision");
        if (!"directory".equals(directory.get("op")) || !(capabilities instanceof Map)
                || !Boolean.TRUE.equals(((Map<?, ?>) capabilities).get("token_discovery")))
            throw new ProbeFailure("中继尚不支持按 Token 自动发现房间。", 0);
        if (!(revision instanceof JsonNumber) || !((JsonNumber) revision).literal.matches("0|[1-9][0-9]{0,15}")
                || Long.parseLong(((JsonNumber) revision).literal) > 9007199254740991L
                || !(hosts instanceof List) || ((List<?>) hosts).size() > 64)
            throw new ProbeFailure("中继房间列表无效。", 0);
        List<Room> rooms = new ArrayList<>(); Set<String> seen = new HashSet<>();
        for (Object value : (List<?>) hosts) {
            if (!(value instanceof Map)) throw new ProbeFailure("中继房间列表无效。", 0);
            Map<?, ?> item = (Map<?, ?>) value;
            if (!Boolean.TRUE.equals(item.get("online"))) continue;
            Object code = item.get("code");
            if (!(code instanceof String) || ((String) code).isEmpty() || ((String) code).length() > 128
                    || !(item.get("token_index") instanceof JsonNumber)
                    || !"0".equals(((JsonNumber) item.get("token_index")).literal) || !seen.add((String) code))
                throw new ProbeFailure("中继房间列表无效。", 0);
            for (int i = 0; i < ((String) code).length(); i++) {
                char character = ((String) code).charAt(i);
                if (Character.isISOControl(character)) throw new ProbeFailure("中继房间列表无效。", 0);
                if (Character.isHighSurrogate(character)) {
                    if (++i == ((String) code).length() || !Character.isLowSurrogate(((String) code).charAt(i)))
                        throw new ProbeFailure("中继房间列表无效。", 0);
                } else if (Character.isLowSurrogate(character)) throw new ProbeFailure("中继房间列表无效。", 0);
            }
            rooms.add(new Room((String) code, label(item.get("machine")), label(item.get("project"))));
        }
        writeFrame(output, 8, new byte[] {3, (byte) 232});
        return new DiscoveryResult(!rooms.isEmpty(), rooms.isEmpty()
                ? "未发现可用房间，请确认 Token 正确且电脑已连接中继。"
                : "测试成功：发现 " + rooms.size() + " 个可用房间。", rooms);
    }

    private static String label(Object value) {
        if (!(value instanceof String)) return "";
        String result = ((String) value).replaceAll("[\\p{Cntrl}]", " ");
        return result.substring(0, result.offsetByCodePoints(0, Math.min(result.codePointCount(0, result.length()), 40)));
    }

    private interface Exchange<T> { T run(Socket socket, URI endpoint) throws Exception; }

    public static final class ProbeResult {
        public final boolean success;
        public final String message;
        private final String endpoint,token;
        private ProbeResult(boolean success,String message,String endpoint,String token){this.success=success;this.message=message;this.endpoint=endpoint;this.token=token;}
        public boolean authorizesDirect(String endpoint,String code,String token){
            if(!success||this.endpoint==null||code==null||!code.isEmpty())return false;
            try{return this.endpoint.equals(ConnectionEndpoint.normalize(endpoint))&&this.token.equals(token);}catch(IllegalArgumentException invalid){return false;}
        }
    }

    public static String test(String endpoint, String code, String token) {
        return testResult(endpoint,code,token).message;
    }
    public static ProbeResult verifyDirect(String endpoint,String token){return testResult(endpoint,"",token);}
    private static ProbeResult testResult(String endpoint, String code, String token) {
        String message;
        try {
            String target=ConnectionEndpoint.normalize(endpoint),room=code==null?"":code,credential=token==null?"":token;
            return new ProbeResult(true,probe(new URI(target),room,credential),room.isEmpty()?target:null,credential);
        } catch (SocketTimeoutException error) {
            message="测试失败：连接超时，请检查网络、端口及主机是否在线。";
        } catch (SSLException error) {
            message="测试失败：TLS 证书或安全连接验证失败。";
        } catch (IllegalArgumentException error) {
            message="测试失败：服务器地址无效，请填写 http:// 或 https:// 地址。";
        } catch (ProbeFailure error) {
            message="测试失败：" + error.getMessage();
        } catch (Exception error) {
            // Exception messages may contain caller input, URLs or credentials.
            message="测试失败：无法连接服务器，请检查地址、端口和网络。";
        }
        return new ProbeResult(false,message,null,null);
    }

    private static String probe(URI endpoint, String code, String token) throws Exception {
        return connected(endpoint, (socket, target) -> exchange(socket, target, code, token));
    }

    private static <T> T connected(URI endpoint, Exchange<T> exchange) throws Exception {
        boolean tls = "https".equalsIgnoreCase(endpoint.getScheme());
        int port = endpoint.getPort() == -1 ? (tls ? 443 : 80) : endpoint.getPort();
        String host = endpoint.getHost();
        if (host.startsWith("[")) host = host.substring(1, host.length() - 1);
        try (Socket plain = new Socket()) {
            plain.connect(new InetSocketAddress(host, port), TIMEOUT);
            plain.setSoTimeout(TIMEOUT);
            if (tls) {
                try (SSLSocket socket = (SSLSocket) ((SSLSocketFactory) SSLSocketFactory.getDefault())
                        .createSocket(plain, host, port, true)) {
                    SSLParameters parameters = socket.getSSLParameters();
                    parameters.setEndpointIdentificationAlgorithm("HTTPS");
                    socket.setSSLParameters(parameters);
                    socket.setSoTimeout(TIMEOUT);
                    socket.startHandshake();
                    return exchange.run(socket, endpoint);
                }
            }
            return exchange.run(plain, endpoint);
        }
    }

    private static String exchange(Socket socket, URI endpoint, String code, String token) throws Exception {
        InputStream input = socket.getInputStream();
        OutputStream output = socket.getOutputStream();
        long deadline = System.nanoTime() + TIMEOUT * 1_000_000L;
        upgrade(socket, endpoint, input, output, deadline);
        if (!code.isEmpty()) {
            writeFrame(output, 1, ("{\"op\":\"phone\",\"code\":" + quote(code) + ",\"token\":"
                    + quote(token) + ",\"reliable\":true}").getBytes(StandardCharsets.UTF_8));
            Frame frame = readData(socket, input, output, deadline);
            if (frame.opcode != 1 || !"ready".equals(object(frame.body).get("op")))
                throw new ProbeFailure("中继未就绪，请检查连接码、Token 和主机在线状态。", 0);
        }
        writeFrame(output, 2, pack(1, ("{\"token\":" + quote(token)
                + ",\"multiplex\":true,\"reliable\":true}").getBytes(StandardCharsets.UTF_8)));
        while (true) {
            Frame frame = readData(socket, input, output, deadline);
            if (frame.opcode == 1) {
                if (object(frame.body).containsKey("error"))
                    throw new ProbeFailure("认证失败或主机离线，请检查连接码和 Token。", 0);
                continue;
            }
            byte[] body = frame.body;
            if (body.length < 5 || length(body) != body.length - 4)
                throw new ProbeFailure("服务器终端协议数据无效。", 0);
            if (body[4] == 10) {
                body[4] = 11;
                writeFrame(output, 2, body);
                continue;
            }
            if (body[4] != 2) continue;
            byte[] json = new byte[body.length - 5];
            System.arraycopy(body, 5, json, 0, json.length);
            Map<?, ?> state = object(json);
            if (state.containsKey("error")) throw new ProbeFailure("主机认证失败，请检查 Token。", 0);
            Object capabilities = state.get("capabilities");
            if (!(capabilities instanceof Map) || !Boolean.TRUE.equals(((Map<?, ?>) capabilities).get("multiplex")))
                throw new ProbeFailure("主机协议版本不兼容，请升级电脑端。", 0);
            writeFrame(output, 8, new byte[] {3, (byte) 232});
            return code.isEmpty() ? "测试成功：服务器连接、Token 认证和终端协议均正常。"
                    : "测试成功：中继连接、连接码、Token 认证及主机终端协议均正常。";
        }
    }

    private static void upgrade(Socket socket, URI endpoint, InputStream input, OutputStream output, long deadline) throws Exception {
        byte[] nonce = new byte[16];
        RANDOM.nextBytes(nonce);
        String key = Base64.getEncoder().encodeToString(nonce);
        String path = endpoint.getRawPath() == null ? "" : endpoint.getRawPath();
        while (path.endsWith("/")) path = path.substring(0, path.length() - 1);
        path += "/ws";
        // Credentials are sent only in the first HELLO frame, never in the URL.
        String request = "GET " + path + " HTTP/1.1\r\nHost: " + endpoint.getRawAuthority()
                + "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: " + key
                + "\r\nSec-WebSocket-Version: 13\r\n\r\n";
        output.write(request.getBytes(StandardCharsets.US_ASCII));
        output.flush();
        String headers = readHeaders(socket, input, deadline);
        String[] lines = headers.split("\r\n");
        if (!lines[0].matches("HTTP/1\\.[01] 101(?: .*)?")) {
            if (lines[0].matches("HTTP/1\\.[01] (401|403)(?: .*)?"))
                throw new ProbeFailure("服务器拒绝访问，请检查 Token。", 0);
            if (lines[0].matches("HTTP/1\\.[01] 404(?: .*)?"))
                throw new ProbeFailure("WebSocket 地址返回 404，请检查服务器地址与部署路径。", 0);
            throw new ProbeFailure("服务器未接受 WebSocket 连接，请检查地址与反向代理配置。", 0);
        }
        Map<String, String> fields = new HashMap<>();
        for (int index = 1; index < lines.length; index++) {
            int colon = lines[index].indexOf(':');
            if (colon > 0) fields.put(lines[index].substring(0, colon).toLowerCase(Locale.ROOT), lines[index].substring(colon + 1).trim());
        }
        String accept = Base64.getEncoder().encodeToString(MessageDigest.getInstance("SHA-1")
                .digest((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").getBytes(StandardCharsets.US_ASCII)));
        if (!accept.equals(fields.get("sec-websocket-accept"))
                || !"websocket".equalsIgnoreCase(fields.get("upgrade"))
                || !containsToken(fields.get("connection"), "upgrade"))
            throw new ProbeFailure("WebSocket 握手校验失败。", 0);
    }

    private static boolean containsToken(String value, String expected) {
        if (value != null) for (String token : value.split(",")) if (expected.equalsIgnoreCase(token.trim())) return true;
        return false;
    }

    private static int read(Socket socket, InputStream input, long deadline) throws IOException {
        long remaining = (deadline - System.nanoTime()) / 1_000_000L;
        if (remaining <= 0) throw new SocketTimeoutException();
        socket.setSoTimeout((int) Math.min(TIMEOUT, remaining));
        int value = input.read();
        if (value < 0) throw new ProbeFailure("服务器响应不完整，请检查中继服务及主机状态。", 0);
        return value;
    }

    private static String readHeaders(Socket socket, InputStream input, long deadline) throws IOException {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        int tail = 0;
        while (buffer.size() < 16384) {
            int value = read(socket, input, deadline);
            buffer.write(value);
            tail = (tail << 8) | value;
            if (tail == 0x0d0a0d0a) return new String(buffer.toByteArray(), StandardCharsets.US_ASCII);
        }
        throw new ProbeFailure("服务器响应头过大。", 0);
    }

    private static Frame readData(Socket socket, InputStream input, OutputStream output, long deadline) throws IOException {
        ByteArrayOutputStream fragments = null;
        int fragmentOpcode = 0;
        while (true) {
            int first = read(socket, input, deadline), second = read(socket, input, deadline);
            int opcode = first & 15;
            boolean finalFrame = (first & 128) != 0;
            if ((first & 0x70) != 0 || (second & 128) != 0 || (opcode >= 8 && !finalFrame))
                throw new ProbeFailure("服务器返回不支持的 WebSocket 帧。", 0);
            long size = second & 127;
            if (size == 126) size = (read(socket, input, deadline) << 8) | read(socket, input, deadline);
            else if (size == 127) {
                size = 0;
                for (int index = 0; index < 8; index++) {
                    int value = read(socket, input, deadline);
                    if (index < 4 && value != 0) throw new ProbeFailure("服务器响应过大。", 0);
                    size = (size << 8) | value;
                }
            }
            if (size > MAX_FRAME || (opcode >= 8 && size > 125)) throw new ProbeFailure("服务器响应过大。", 0);
            byte[] body = new byte[(int) size];
            for (int index = 0; index < body.length; index++) body[index] = (byte) read(socket, input, deadline);
            if (opcode == 8) {
                int status = body.length >= 2 ? ((body[0] & 255) << 8) | (body[1] & 255) : 1000;
                if (status == 4001 || status == 4003 || status == 1008)
                    throw new ProbeFailure("认证失败，请检查 Token 和连接码。", status);
                if (status == 4004) throw new ProbeFailure("未找到在线主机，请检查连接码及电脑端中继连接。", status);
                throw new ProbeFailure("服务器关闭了连接，请检查连接码、Token 和主机状态。", status);
            }
            if (opcode == 9) { writeFrame(output, 10, body); continue; }
            if (opcode == 10) continue;
            if (opcode == 0) {
                if (fragments == null || fragments.size() + body.length > MAX_FRAME)
                    throw new ProbeFailure("Invalid WebSocket fragments", 0);
                fragments.write(body, 0, body.length);
                if (finalFrame) return new Frame(fragmentOpcode, fragments.toByteArray());
                continue;
            }
            if ((opcode != 1 && opcode != 2) || fragments != null)
                throw new ProbeFailure("Invalid WebSocket frame", 0);
            if (finalFrame) return new Frame(opcode, body);
            fragmentOpcode = opcode;
            fragments = new ByteArrayOutputStream();
            fragments.write(body, 0, body.length);
        }
    }

    private static void writeFrame(OutputStream output, int opcode, byte[] body) throws IOException {
        if (body.length > MAX_FRAME) throw new ProbeFailure("连接参数过长。", 0);
        output.write(128 | opcode);
        if (body.length < 126) output.write(128 | body.length);
        else if (body.length <= 65535) { output.write(254); output.write(body.length >> 8); output.write(body.length); }
        else { output.write(255); for (int shift = 56; shift >= 0; shift -= 8) output.write((int) ((long) body.length >>> shift)); }
        byte[] mask = new byte[4];
        RANDOM.nextBytes(mask);
        output.write(mask);
        byte[] masked = new byte[body.length];
        for (int index = 0; index < body.length; index++) masked[index] = (byte) (body[index] ^ mask[index % 4]);
        output.write(masked);
        output.flush();
    }

    private static byte[] pack(int opcode, byte[] json) {
        byte[] result = new byte[json.length + 5];
        int length = json.length + 1;
        for (int index = 0; index < 4; index++) result[index] = (byte) (length >>> (8 * index));
        result[4] = (byte) opcode;
        System.arraycopy(json, 0, result, 5, json.length);
        return result;
    }

    private static long length(byte[] body) {
        long value = 0;
        for (int index = 0; index < 4; index++) value |= (long) (body[index] & 255) << (8 * index);
        return value;
    }

    private static String quote(String value) {
        StringBuilder result = new StringBuilder("\"");
        for (int index = 0; index < value.length(); index++) {
            char ch = value.charAt(index);
            if (ch == '"' || ch == '\\') result.append('\\').append(ch);
            else if (ch < 32) result.append(String.format(Locale.ROOT, "\\u%04x", (int) ch));
            else result.append(ch);
        }
        return result.append('"').toString();
    }

    private static Map<?, ?> object(byte[] bytes) throws ProbeFailure {
        Json reader = new Json(new String(bytes, StandardCharsets.UTF_8));
        Object value = reader.value(0);
        reader.space();
        if (!(value instanceof Map) || reader.position != reader.text.length()) throw new ProbeFailure("服务器 JSON 响应无效。", 0);
        return (Map<?, ?>) value;
    }

    /** Minimal bounded JSON reader keeps this network helper testable without Android runtime stubs. */
    private static final class Json {
        final String text;
        int position;
        Json(String text) { this.text = text; }
        void space() { while (position < text.length() && " \r\n\t".indexOf(text.charAt(position)) >= 0) position++; }
        char take() throws ProbeFailure { if (position == text.length()) throw invalid(); return text.charAt(position++); }
        ProbeFailure invalid() { return new ProbeFailure("服务器 JSON 响应无效。", 0); }
        String string() throws ProbeFailure {
            if (take() != '"') throw invalid();
            StringBuilder result = new StringBuilder();
            while (true) {
                char ch = take();
                if (ch == '"') return result.toString();
                if (ch < 32) throw invalid();
                if (ch == '\\') {
                    ch = take();
                    int escape = "\"\\/bfnrt".indexOf(ch);
                    if (escape >= 0) ch = "\"\\/\b\f\n\r\t".charAt(escape);
                    else if (ch == 'u') {
                        int code = 0;
                        for (int index = 0; index < 4; index++) { int digit = Character.digit(take(), 16); if (digit < 0) throw invalid(); code = code * 16 + digit; }
                        ch = (char) code;
                    } else throw invalid();
                }
                result.append(ch);
            }
        }
        Object value(int depth) throws ProbeFailure {
            if (depth > 32) throw invalid();
            space();
            if (position == text.length()) throw invalid();
            char ch = text.charAt(position);
            if (ch == '"') return string();
            if (ch == '{' || ch == '[') {
                position++;
                boolean object = ch == '{';
                char end = object ? '}' : ']';
                Map<String, Object> values = new HashMap<>();
                List<Object> elements = new ArrayList<>();
                Object container = object ? values : elements;
                space();
                if (position < text.length() && text.charAt(position) == end) { position++; return container; }
                while (true) {
                    space();
                    String key = object ? string() : "";
                    space();
                    if (object && take() != ':') throw invalid();
                    Object item = value(depth + 1);
                    if (object) values.put(key, item); else elements.add(item);
                    space();
                    char next = take();
                    if (next == end) return container;
                    if (next != ',') throw invalid();
                }
            }
            int start = position;
            while (position < text.length() && ",]} \r\n\t".indexOf(text.charAt(position)) < 0) position++;
            String literal = text.substring(start, position);
            if ("true".equals(literal)) return Boolean.TRUE;
            if ("false".equals(literal)) return Boolean.FALSE;
            if ("null".equals(literal)) return null;
            if (literal.length() <= 128 && literal.matches("-?(0|[1-9][0-9]*)(\\.[0-9]+)?([eE][+-]?[0-9]+)?")) return new JsonNumber(literal);
            throw invalid();
        }
    }

    private static final class JsonNumber {
        final String literal;
        JsonNumber(String literal) { this.literal = literal; }
    }

    private static final class Frame {
        final int opcode;
        final byte[] body;
        Frame(int opcode, byte[] body) { this.opcode = opcode; this.body = body; }
    }

    private static final class ProbeFailure extends IOException {
        ProbeFailure(String message, int status) { super(message); }
    }
}
