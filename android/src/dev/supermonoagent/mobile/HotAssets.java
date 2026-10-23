package dev.supermonoagent.mobile;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.HashMap;
import java.util.Map;
import java.util.Properties;

/** USB developer updates only; no remote download or executable native code. */
public final class HotAssets {
    private static final int MAX_BYTES = 5 * 1024 * 1024;
    private final String revision;
    private final Map<String, byte[]> files;

    private HotAssets(String revision, Map<String, byte[]> files) {
        this.revision = revision;
        this.files = files;
    }

    /** Fail closed to the APK assets if the selected complete update cannot be verified. */
    public static HotAssets load(File externalFilesDirectory) {
        try {
            if (externalFilesDirectory == null) throw new IOException();
            File parent = externalFilesDirectory.getCanonicalFile();
            File root = child(parent, "ui-updates");
            String revision = new String(read(child(root, "active.txt"), 128), StandardCharsets.US_ASCII).trim();
            if (!revision.matches("[A-Za-z0-9_-]{1,96}")) throw new IOException();
            File directory = child(root, revision);
            Properties manifest = new Properties();
            manifest.load(new ByteArrayInputStream(read(child(directory, "manifest.properties"), 16384)));
            // API2 requires one complete 16-file snapshot. Legacy ten-file USB
            // overrides fall back as a whole; never mix old scripts with APK JSON.
            if (!revision.equals(manifest.getProperty("revision")) || manifest.size() != 17) throw new IOException();
            Map<String, byte[]> verified = new HashMap<>();
            int remaining = MAX_BYTES;
            for (String key : manifest.stringPropertyNames()) {
                if (key.equals("revision")) continue;
                if (!key.startsWith("file.")) throw new IOException();
                String path = key.substring(5);
                if (!path.equals(LocalAssets.assetPath("https://ui.local", "https://ui.local/" + path))) throw new IOException();
                String expected = manifest.getProperty(key);
                if (!expected.matches("[0-9a-f]{64}")) throw new IOException();
                byte[] bytes = read(child(directory, path), remaining);
                remaining -= bytes.length;
                byte[] digest = MessageDigest.getInstance("SHA-256").digest(bytes);
                StringBuilder hash = new StringBuilder();
                for (byte value : digest) hash.append(Character.forDigit((value >>> 4) & 15, 16)).append(Character.forDigit(value & 15, 16));
                if (!expected.equals(hash.toString())) throw new IOException();
                verified.put(path, bytes);
            }
            if (verified.size() != 16) throw new IOException();
            return new HotAssets(revision, verified);
        } catch (Exception error) {
            return new HotAssets("", new HashMap<String, byte[]>());
        }
    }

    public String getRevision() { return revision; }

    /** Bytes are a verified snapshot: later USB writes cannot mix revisions in this instance. */
    public InputStream open(String asset) {
        byte[] bytes = files.get(asset);
        return bytes == null ? null : new ByteArrayInputStream(bytes);
    }

    private static File child(File parent, String name) throws IOException {
        File candidate = new File(parent, name).getAbsoluteFile();
        File canonical = candidate.getCanonicalFile();
        if (!canonical.equals(candidate) || !canonical.toPath().startsWith(parent.toPath())) throw new IOException();
        return canonical;
    }

    private static byte[] read(File file, int limit) throws IOException {
        if (!file.isFile() || file.length() > limit) throw new IOException();
        try (InputStream input = Files.newInputStream(file.toPath()); java.io.ByteArrayOutputStream output = new java.io.ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            int count;
            while ((count = input.read(buffer)) != -1) {
                if (count > limit - output.size()) throw new IOException();
                output.write(buffer, 0, count);
            }
            return output.toByteArray();
        }
    }
}
