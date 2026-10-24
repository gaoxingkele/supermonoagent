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

    /** IP/host input defaults to verified HTTPS; an embedded URL port is authoritative. */
    public static String fromAddress(String address,String port) {
        String value=address==null?"":address.trim();
        if(!value.contains("://")){
            int slash=value.indexOf('/');String authority=slash<0?value:value.substring(0,slash),path=slash<0?"":value.substring(slash);
            if(!authority.startsWith("[")&&authority.indexOf(':')!=authority.lastIndexOf(':'))authority="["+authority+"]";
            value="https://"+authority+path;
        }
        URI uri=parse(value);int chosen=uri.getPort();
        if(chosen<0){String text=port==null?"":port.trim();if(!text.isEmpty()&&!text.matches("[0-9]{1,5}"))throw new IllegalArgumentException("Enter a valid port");chosen=text.isEmpty()?(uri.getScheme().equalsIgnoreCase("https")?443:80):Integer.parseInt(text);}
        if(chosen<1||chosen>65535)throw new IllegalArgumentException("Enter a port from 1 to 65535");
        String host=uri.getHost();if(host.indexOf(':')>=0&&!host.startsWith("["))host="["+host+"]";
        boolean standard=uri.getScheme().equalsIgnoreCase("https")&&chosen==443||uri.getScheme().equalsIgnoreCase("http")&&chosen==80;
        return normalize(uri.getScheme()+"://"+host+(standard?"":":"+chosen)+(uri.getRawPath()==null?"":uri.getRawPath()));
    }
    public static int embeddedPort(String address){
        try{String value=address.trim();return parse(value.contains("://")?value:"https://"+value).getPort();}catch(Exception incomplete){return -1;}
    }
    public static String displayAddress(String endpoint){
        try{URI uri=URI.create(normalize(endpoint));String host=uri.getHost();if(host.indexOf(':')>=0&&!host.startsWith("["))host="["+host+"]";return (uri.getScheme().equalsIgnoreCase("https")?"":uri.getScheme()+"://")+host+(uri.getRawPath()==null?"":uri.getRawPath());}catch(Exception invalid){return endpoint;}
    }
    public static String displayPort(String endpoint){
        try{URI uri=URI.create(normalize(endpoint));return Integer.toString(uri.getPort()<0?(uri.getScheme().equalsIgnoreCase("https")?443:80):uri.getPort());}catch(Exception invalid){return "443";}
    }
}
