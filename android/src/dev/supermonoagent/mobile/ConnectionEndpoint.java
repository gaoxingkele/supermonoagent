package dev.supermonoagent.mobile;

import java.net.URI;
import java.net.URISyntaxException;

/** Syntax checks for local document identities; verified transport normalization is separate. */
public final class ConnectionEndpoint {
    private ConnectionEndpoint() { }
    public static void validate(String value) { parse(value); }
    private static URI parse(String value) {
        try {
            URI uri = new URI(value);
            if (!("http".equalsIgnoreCase(uri.getScheme()) || "https".equalsIgnoreCase(uri.getScheme()))
                    || uri.getHost() == null || uri.getHost().isEmpty()
                    || uri.getPort() > 65535 || uri.getPort() == 0
                    || uri.getRawUserInfo() != null || uri.getRawAuthority().contains("@")
                    || uri.getRawQuery() != null || uri.getRawFragment() != null) {
                throw new IllegalArgumentException("Enter an HTTP(S) server URL without credentials, query or fragment");
            }
            return uri;
        } catch (URISyntaxException error) {
            throw new IllegalArgumentException("Enter a valid HTTP(S) server URL");
        }
    }
    public static String normalize(String value) {
        URI uri=parse(value);
        String host=uri.getHost().toLowerCase(java.util.Locale.ROOT),path=uri.getRawPath();
        if("http".equalsIgnoreCase(uri.getScheme())&&host.equals("117.50.48.22")&&uri.getPort()==8817
                &&(path==null||path.isEmpty()||path.equals("/")||path.equals("/ws")||path.equals("/ws/")))return "https://117.50.48.22";
        boolean local=host.equals("localhost")||host.equals("::1")||host.equals("[::1]")||host.matches("127[.][0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}");
        if("http".equalsIgnoreCase(uri.getScheme())&&!local)throw new IllegalArgumentException("Network connections require HTTPS; HTTP is only allowed on loopback");
        return uri.toString();
    }
}
