package dev.supermonoagent.mobile;

import java.net.URI;
import java.net.URISyntaxException;

/** Strict parsing before handing a caller-supplied URL to Android's lenient Uri parser. */
public final class ConnectionEndpoint {
    private ConnectionEndpoint() { }

    public static void validate(String value) {
        try {
            URI uri = new URI(value);
            if (!("http".equalsIgnoreCase(uri.getScheme()) || "https".equalsIgnoreCase(uri.getScheme()))
                    || uri.getHost() == null || uri.getHost().isEmpty()
                    || uri.getPort() > 65535 || uri.getPort() == 0
                    || uri.getRawUserInfo() != null || uri.getRawQuery() != null || uri.getRawFragment() != null) {
                throw new IllegalArgumentException("Enter an HTTP(S) server URL without credentials, query or fragment");
            }
        } catch (URISyntaxException error) {
            throw new IllegalArgumentException("Enter a valid HTTP(S) server URL");
        }
    }
}
