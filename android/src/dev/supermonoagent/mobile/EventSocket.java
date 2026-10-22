package dev.supermonoagent.mobile;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.util.*;
import javax.net.ssl.*;

/** Bounded RFC 6455 client for notification metadata only. No terminal input or subscriptions. */
public final class EventSocket implements AutoCloseable {
    static final int LIMIT = 256 * 1024;
    private static final SecureRandom RANDOM = new SecureRandom();
    private final Socket socket;
    private final InputStream input;
    private final OutputStream output;
    public static final class Closed extends IOException {
        public final int code;
        Closed(int code) { super("WebSocket closed"); this.code = code; }
    }
    public static final class Message {
        public final int opcode;
        public final byte[] body;
        Message(int opcode, byte[] body) { this.opcode = opcode; this.body = body; }
    }
    public EventSocket(String address) throws Exception {
        URI uri = new URI(ConnectionEndpoint.normalize(address));
        boolean tls = "https".equalsIgnoreCase(uri.getScheme());
        int port = uri.getPort() < 0 ? (tls ? 443 : 80) : uri.getPort();
        String host = uri.getHost();
        if (host.startsWith("[")) host = host.substring(1, host.length() - 1);
        Socket connection = new Socket();
        try {
            connection.connect(new InetSocketAddress(host, port), 8000);
            connection.setSoTimeout(15000);
            if (tls) {
                SSLSocket secure = (SSLSocket) ((SSLSocketFactory) SSLSocketFactory.getDefault())
                        .createSocket(connection, host, port, true);
                connection = secure;
                SSLParameters parameters = secure.getSSLParameters();
                parameters.setEndpointIdentificationAlgorithm("HTTPS");
                secure.setSSLParameters(parameters);
                secure.setSoTimeout(15000);
                secure.startHandshake();
            }
            socket = connection;
            input = socket.getInputStream();
            output = socket.getOutputStream();
            byte[] random = new byte[16]; RANDOM.nextBytes(random);
            String key = Base64.getEncoder().encodeToString(random);
            String path = uri.getRawPath() == null ? "" : uri.getRawPath();
            while (path.endsWith("/")) path = path.substring(0, path.length() - 1);
            String request = "GET " + path + "/ws HTTP/1.1\r\nHost: " + uri.getRawAuthority()
                    + "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: " + key
                    + "\r\nSec-WebSocket-Version: 13\r\n\r\n";
            output.write(request.getBytes(StandardCharsets.US_ASCII)); output.flush();
            ByteArrayOutputStream headers = new ByteArrayOutputStream(); int tail = 0;
            long deadline = System.nanoTime() + 15000000000L;
            while (tail != 0x0d0a0d0a) {
                if (headers.size() >= 16384 || System.nanoTime() > deadline) throw new IOException("Invalid upgrade");
                int value = readByte(); headers.write(value); tail = (tail << 8) | value;
            }
            String[] lines = new String(headers.toByteArray(), StandardCharsets.US_ASCII).split("\r\n");
            if (lines[0].matches("HTTP/1\\.[01] (401|403)(?: .*)?")) throw new Closed(4003);
            if (!lines[0].matches("HTTP/1\\.[01] 101(?: .*)?")) throw new IOException("Upgrade refused");
            Map<String, String> fields = new HashMap<>();
            for (int n = 1; n < lines.length; n++) {
                int colon = lines[n].indexOf(':');
                if (colon > 0) fields.put(lines[n].substring(0, colon).toLowerCase(Locale.ROOT), lines[n].substring(colon + 1).trim());
            }
            String accept = Base64.getEncoder().encodeToString(MessageDigest.getInstance("SHA-1")
                    .digest((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").getBytes(StandardCharsets.US_ASCII)));
            if (!accept.equals(fields.get("sec-websocket-accept")) || !"websocket".equalsIgnoreCase(fields.get("upgrade"))
                    || !token(fields.get("connection"), "upgrade") || fields.containsKey("sec-websocket-extensions"))
                throw new IOException("Invalid upgrade");
        } catch (Exception error) { try { connection.close(); } catch (IOException ignored) { } throw error; }
    }
    private static boolean token(String values, String expected) {
        if (values != null) for (String item : values.split(",")) if (item.trim().equalsIgnoreCase(expected)) return true;
        return false;
    }
    private int readByte() throws IOException { int value = input.read(); if (value < 0) throw new EOFException(); return value; }
    public synchronized void send(int opcode, byte[] body) throws IOException {
        if (body.length > LIMIT || (opcode >= 8 && body.length > 125)) throw new IOException("Frame too large");
        output.write(128 | opcode);
        if (body.length < 126) output.write(128 | body.length);
        else if (body.length <= 65535) { output.write(254); output.write(body.length >>> 8); output.write(body.length); }
        else { output.write(255); for (int shift = 56; shift >= 0; shift -= 8) output.write((int) ((long) body.length >>> shift)); }
        byte[] mask = new byte[4]; RANDOM.nextBytes(mask); output.write(mask);
        byte[] encoded = new byte[body.length];
        for (int n = 0; n < body.length; n++) encoded[n] = (byte) (body[n] ^ mask[n % 4]);
        output.write(encoded); output.flush();
    }
    public void json(String json) throws IOException { send(1, json.getBytes(StandardCharsets.UTF_8)); }
    public void protocol(int opcode, String json) throws IOException { send(2, pack(opcode, json.getBytes(StandardCharsets.UTF_8))); }
    public static byte[] pack(int opcode, byte[] json) {
        byte[] body = new byte[json.length + 5]; int size = json.length + 1;
        for (int n = 0; n < 4; n++) body[n] = (byte) (size >>> (8 * n));
        body[4] = (byte) opcode; System.arraycopy(json, 0, body, 5, json.length); return body;
    }
    public Message receive() throws IOException {
        ByteArrayOutputStream combined = null; int type = 0;
        boolean awaitingPong = false;
        while (true) {
            int first;
            try { first = readByte(); }
            catch (SocketTimeoutException idle) {
                if (combined != null || awaitingPong) throw idle;
                send(9, new byte[0]); awaitingPong = true; continue;
            }
            int second = readByte(), opcode = first & 15;
            boolean end = (first & 128) != 0;
            if ((first & 112) != 0 || (second & 128) != 0) throw new IOException("Invalid server frame");
            long length = second & 127;
            if (length == 126) length = (readByte() << 8) | readByte();
            else if (length == 127) {
                length = 0;
                for (int n = 0; n < 8; n++) { int value = readByte(); if (n < 4 && value != 0) throw new IOException("Frame too large"); length = (length << 8) | value; }
            }
            if (length > LIMIT || (opcode >= 8 && (!end || length > 125))) throw new IOException("Invalid frame size");
            byte[] body = new byte[(int) length];
            for (int at = 0; at < body.length;) { int count = input.read(body, at, body.length - at); if (count < 0) throw new EOFException(); at += count; }
            if (opcode == 8) {
                if (body.length == 1) throw new IOException("Invalid close");
                throw new Closed(body.length < 2 ? 1000 : ((body[0] & 255) << 8) | (body[1] & 255));
            }
            if (opcode == 9) { send(10, body); continue; }
            if (opcode == 10) { awaitingPong = false; continue; }
            if (opcode == 1 || opcode == 2) {
                if (combined != null) throw new IOException("Interleaved fragments");
                if (end) return new Message(opcode, body);
                combined = new ByteArrayOutputStream(); type = opcode;
            } else if (opcode != 0 || combined == null) throw new IOException("Invalid continuation");
            if (combined.size() + body.length > LIMIT) throw new IOException("Message too large");
            combined.write(body, 0, body.length);
            if (end) return new Message(type, combined.toByteArray());
        }
    }
    public void close() { try { socket.close(); } catch (IOException ignored) { } }
}
