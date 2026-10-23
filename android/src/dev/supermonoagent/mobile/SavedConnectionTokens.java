package dev.supermonoagent.mobile;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.Base64;
import java.util.Locale;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Endpoint-bound saved credentials; plaintext never enters SharedPreferences. */
public final class SavedConnectionTokens {
    private static final String FILE = "saved-server-credentials";
    private static final String ALIAS = "supermonoagent.saved-server-tokens.v1";
    private static final int MAX_TOKEN = 4096, MAX_ENVELOPE = 32768;
    private SavedConnectionTokens() { }

    /** Programmatic autofill (including a failed decrypt) is never a user save/delete request. */
    public static final class EditState {
        public enum Action { KEEP, SAVE, REMOVE }
        private boolean loading,edited,forget;
        public void beginLoad(){loading=true;edited=false;forget=false;}
        public void endLoad(){loading=false;}
        public void changed(){if(!loading)edited=true;}
        public void forget(){forget=true;}
        public Action action(boolean remember,String token){
            if(forget)return Action.REMOVE;
            if(!remember||!edited)return Action.KEEP;
            return token==null||token.isEmpty()?Action.REMOVE:Action.SAVE;
        }
        public void committed(){edited=false;forget=false;}
    }

    public static String canonicalEndpoint(String value) {
        URI uri = URI.create(ConnectionEndpoint.normalize(value));
        String scheme = uri.getScheme().toLowerCase(Locale.ROOT);
        String host = uri.getHost().toLowerCase(Locale.ROOT);
        if (host.indexOf(':') >= 0 && !host.startsWith("[")) host = "[" + host + "]";
        int port = uri.getPort();
        boolean standard = port < 0 || (scheme.equals("https") && port == 443) || (scheme.equals("http") && port == 80);
        String path = uri.getRawPath();
        if (path == null) path = "";
        while (path.endsWith("/")) path = path.substring(0, path.length() - 1);
        return scheme + "://" + host + (standard ? "" : ":" + port) + path;
    }

    private static String entry(String scope) throws Exception {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(MessageDigest.getInstance("SHA-256")
                .digest(scope.getBytes(StandardCharsets.UTF_8)));
    }

    private static synchronized SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (store.containsAlias(ALIAS)) return (SecretKey) store.getKey(ALIAS, null);
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256).setRandomizedEncryptionRequired(true).build());
        return generator.generateKey();
    }

    static String seal(String scope, String token, SecretKey key) throws Exception {
        if (token == null || token.isEmpty() || token.length() > MAX_TOKEN) throw new IllegalArgumentException("Invalid token length");
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key);
        cipher.updateAAD(scope.getBytes(StandardCharsets.UTF_8));
        byte[] iv = cipher.getIV(), encrypted = cipher.doFinal(token.getBytes(StandardCharsets.UTF_8));
        if (iv.length != 12) throw new IllegalStateException("Invalid credential nonce");
        byte[] envelope = new byte[1 + iv.length + encrypted.length];
        envelope[0] = 1;
        System.arraycopy(iv, 0, envelope, 1, iv.length);
        System.arraycopy(encrypted, 0, envelope, 13, encrypted.length);
        return Base64.getEncoder().encodeToString(envelope);
    }

    static String unseal(String scope, String envelope, SecretKey key) throws Exception {
        if (envelope == null || envelope.length() > MAX_ENVELOPE) throw new IllegalArgumentException("Invalid credential size");
        byte[] bytes = Base64.getDecoder().decode(envelope);
        if (bytes.length < 30 || bytes[0] != 1) throw new IllegalArgumentException("Invalid credential format");
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key, new GCMParameterSpec(128, Arrays.copyOfRange(bytes, 1, 13)));
        cipher.updateAAD(scope.getBytes(StandardCharsets.UTF_8));
        String token = new String(cipher.doFinal(bytes, 13, bytes.length - 13), StandardCharsets.UTF_8);
        if (token.isEmpty() || token.length() > MAX_TOKEN) throw new IllegalArgumentException("Invalid token length");
        return token;
    }

    public static String load(Context context, String endpoint) {
        try {
            String scope = canonicalEndpoint(endpoint), name = entry(scope);
            String envelope = context.getSharedPreferences(FILE, Context.MODE_PRIVATE).getString(name, "");
            return envelope.isEmpty() ? "" : unseal(scope, envelope, key());
        } catch (Exception error) {
            // A transient keystore failure must not erase the saved credential.
            return "";
        }
    }

    public static void save(Context context, String endpoint, String token) throws Exception {
        // Deletion requires explicit remove(); an empty failed/autofilled load cannot erase data.
        if (token == null || token.isEmpty()) return;
        String scope = canonicalEndpoint(endpoint);
        String envelope = seal(scope, token, key());
        if (!context.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit().putString(entry(scope), envelope).commit())
            throw new IllegalStateException("Unable to save credential");
    }

    public static boolean remove(Context context, String endpoint) {
        try {
            String name = entry(canonicalEndpoint(endpoint));
            return context.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit().remove(name).commit();
        } catch (Exception ignored) { return false; }
    }
}
