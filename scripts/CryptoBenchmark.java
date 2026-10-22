import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Arrays;
import javax.crypto.AEADBadTagException;
import javax.crypto.Cipher;
import javax.crypto.Mac;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/** Primitive benchmark only. This is not a deployed encryption protocol. */
public final class CryptoBenchmark {
    private static byte[] derive(byte[] token, byte[] salt, String context) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(salt, "HmacSHA256"));
        byte[] prk = mac.doFinal(token);
        mac.init(new SecretKeySpec(prk, "HmacSHA256"));
        mac.update(context.getBytes(StandardCharsets.UTF_8));
        mac.update((byte) 1);
        return mac.doFinal(); // One RFC 5869 expand block, exactly 32 bytes.
    }

    private static byte[] nonce(long sequence) {
        byte[] iv = new byte[12];
        // A separate key is derived for this benchmark's connection and direction.
        for (int i = 0; i < 8; i++) iv[11 - i] = (byte) (sequence >>> (i * 8));
        return iv;
    }

    private static String stats(long[] times, int bytes) {
        long total = 0;
        for (long time : times) total += time;
        Arrays.sort(times);
        return String.format(java.util.Locale.ROOT,
                "{\"median_us\":%.3f,\"p95_us\":%.3f,\"MiB_per_s\":%.3f}",
                times[times.length / 2] / 1000.0,
                times[(int) ((times.length - 1) * .95)] / 1000.0,
                bytes * (double) times.length * 1e9 / total / 1048576.0);
    }

    private static boolean rejects(Cipher cipher, byte[] key, byte[] iv, byte[] aad, byte[] encrypted) throws Exception {
        try {
            cipher.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"), new GCMParameterSpec(128, iv));
            cipher.updateAAD(aad);
            cipher.doFinal(encrypted);
            return false;
        } catch (AEADBadTagException expected) {
            return true;
        }
    }

    public static void main(String[] args) throws Exception {
        SecureRandom random = new SecureRandom();
        byte[] token = new byte[32], salt = new byte[32];
        random.nextBytes(token); random.nextBytes(salt);
        byte[] key = derive(token, salt, "SMA/bench/v1/host-a/user-a/client-to-host");
        byte[] other = derive(token, salt, "SMA/bench/v1/host-a/user-b/client-to-host");
        byte[] aad = "SMA-benchmark-only".getBytes(StandardCharsets.US_ASCII);
        Cipher encrypt = Cipher.getInstance("AES/GCM/NoPadding");
        Cipher decrypt = Cipher.getInstance("AES/GCM/NoPadding");
        StringBuilder report = new StringBuilder("{\"scope\":\"JCE primitives; no network, UI or TLS\",\"provider\":\"")
                .append(encrypt.getProvider().getName()).append("\",\"algorithm\":\"AES-256-GCM + HKDF-SHA256\",\"results\":[");
        long sequence = 0;
        boolean first = true;
        for (int bytes : new int[]{64, 256, 1024, 8192, 32768}) {
            int count = bytes <= 1024 ? 3000 : 1000;
            byte[] plain = new byte[bytes]; random.nextBytes(plain);
            long[] enc = new long[count], dec = new long[count];
            for (int i = -200; i < count; i++) {
                byte[] iv = nonce(++sequence);
                long start = System.nanoTime();
                encrypt.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key, "AES"), new GCMParameterSpec(128, iv));
                encrypt.updateAAD(aad);
                byte[] encrypted = encrypt.doFinal(plain);
                long middle = System.nanoTime();
                decrypt.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"), new GCMParameterSpec(128, iv));
                decrypt.updateAAD(aad);
                byte[] recovered = decrypt.doFinal(encrypted);
                long end = System.nanoTime();
                if (!Arrays.equals(plain, recovered)) throw new AssertionError("Round-trip mismatch");
                if (i >= 0) { enc[i] = middle - start; dec[i] = end - middle; }
            }
            if (!first) report.append(','); first = false;
            report.append("{\"bytes\":").append(bytes).append(",\"iterations\":").append(count)
                    .append(",\"encrypt\":").append(stats(enc, bytes))
                    .append(",\"decrypt\":").append(stats(dec, bytes)).append('}');
        }
        byte[] iv = nonce(++sequence);
        encrypt.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key, "AES"), new GCMParameterSpec(128, iv));
        encrypt.updateAAD(aad);
        byte[] encrypted = encrypt.doFinal("test".getBytes(StandardCharsets.US_ASCII));
        boolean wrongContext = rejects(decrypt, other, iv, aad, encrypted);
        encrypted[0] ^= 1;
        boolean tamper = rejects(decrypt, key, iv, aad, encrypted);
        if (!wrongContext || !tamper) throw new AssertionError("Authentication was not enforced");
        long start = System.nanoTime();
        for (int i = 0; i < 1000; i++) derive(token, salt, "SMA/bench/v1/" + i);
        double hkdf = (System.nanoTime() - start) / 1000.0 / 1000.0;
        report.append("],\"wrong_context_rejected\":true,\"tamper_rejected\":true,\"hkdf_mean_us\":")
                .append(String.format(java.util.Locale.ROOT, "%.3f", hkdf)).append('}');
        System.out.println(report);
    }
}
