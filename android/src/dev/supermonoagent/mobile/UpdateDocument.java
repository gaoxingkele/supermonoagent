package dev.supermonoagent.mobile;

import java.io.IOException;
import java.net.URI;
import java.net.URLDecoder;
import java.util.*;
import java.nio.charset.StandardCharsets;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Main-frame navigation capability, not an all-frame JavaScript interface. */
public final class UpdateDocument {
    private UpdateDocument() { }
    public static String asset(String origin,String url,String revision) {
        try{URI uri=URI.create(url);if(!Objects.equals(uri.getRawQuery(),"ui_revision="+revision))return null;
            return LocalAssets.assetPath(origin,new URI(uri.getScheme(),uri.getRawAuthority(),uri.getRawPath(),null,null).toString());
        }catch(Exception error){return null;}
    }
    public static boolean document(String origin,String url,String revision){return "mobile/index.html".equals(asset(origin,url,revision));}
    public static String action(String origin,String current,String request,boolean mainFrame,String ticket,String revision,long generation) {
        if(!mainFrame||!document(origin,current,revision)||ticket==null||ticket.isEmpty()||request.length()>1024)return null;
        try{URI uri=URI.create(request);if(!"sma-update".equals(uri.getScheme())||uri.getRawUserInfo()!=null||uri.getPort()!=-1||uri.getFragment()!=null||!(uri.getRawPath()==null||uri.getRawPath().isEmpty()))return null;
            String action=uri.getHost();if(!Arrays.asList("check","install","rollback","ready","fail").contains(action))return null;
            Map<String,String> query=new HashMap<>();for(String item:uri.getRawQuery().split("&")){String[] pair=item.split("=",2);if(pair.length!=2||query.put(pair[0],URLDecoder.decode(pair[1],"UTF-8"))!=null)return null;}
            return query.size()==3&&ticket.equals(query.get("ticket"))&&revision.equals(query.get("revision"))&&Long.toString(generation).equals(query.get("generation"))?action:null;
        }catch(Exception error){return null;}
    }
    private static final Pattern TAG=Pattern.compile("(?is)<!--.*?-->|<([a-z][a-z0-9]*)\\b(?:[^>'\"]|'[^']*'|\"[^\"]*\")*>");
    private static final Pattern ATTRIBUTE=Pattern.compile("(?is)(\\s)(src|href)(\\s*=\\s*)(['\"])(.*?)\\4");
    private static String resource(String origin,String value,String revision) throws Exception {
        URI base=URI.create(origin),resolved=URI.create(origin+"/mobile/index.html").resolve(value.trim()).normalize();
        int basePort=base.getPort()<0?(base.getScheme().equals("https")?443:80):base.getPort(),port=resolved.getPort()<0?(resolved.getScheme().equalsIgnoreCase("https")?443:80):resolved.getPort();
        RemoteUpdates.require(resolved.getRawUserInfo()==null&&base.getScheme().equalsIgnoreCase(resolved.getScheme())&&base.getHost().equalsIgnoreCase(resolved.getHost())&&basePort==port,"Foreign resource refused");
        String path=LocalAssets.assetPath(origin,origin+resolved.getRawPath());RemoteUpdates.require(path!=null&&!path.equals("mobile/index.html"),"Unlisted resource refused");
        return "/"+path+"?ui_revision="+revision+(resolved.getRawFragment()==null?"":"#"+resolved.getRawFragment());
    }
    public static String html(byte[] raw,String bridge,String ticket,String revision,long generation,String origin) throws Exception {
        String source=new String(raw,StandardCharsets.UTF_8),nonce=UUID.randomUUID().toString();
        Matcher tags=TAG.matcher(source);StringBuffer rewritten=new StringBuffer();boolean foundHead=false;
        while(tags.find()){
            String name=tags.group(1),tag=tags.group();if(name==null){tags.appendReplacement(rewritten,Matcher.quoteReplacement(tag));continue;}
            if(name.equalsIgnoreCase("head"))foundHead=true;
            Matcher attributes=ATTRIBUTE.matcher(tag);StringBuffer resourceTag=new StringBuffer();
            while(attributes.find()){
                boolean asset=attributes.group(2).equalsIgnoreCase("src")||name.equalsIgnoreCase("link");
                String attribute=asset?attributes.group(1)+attributes.group(2)+attributes.group(3)+attributes.group(4)+resource(origin,attributes.group(5),revision)+attributes.group(4):attributes.group();
                attributes.appendReplacement(resourceTag,Matcher.quoteReplacement(attribute));
            }attributes.appendTail(resourceTag);tags.appendReplacement(rewritten,Matcher.quoteReplacement(resourceTag.toString()));
        }tags.appendTail(rewritten);RemoteUpdates.require(foundHead,"Missing HTML head");String html=rewritten.toString();
        String settings="{ticket:"+RemoteUpdates.Json.render(ticket)+",revision:"+RemoteUpdates.Json.render(revision)+",generation:"+generation+"}";
        String policy="default-src 'none'; script-src 'self' 'nonce-"+nonce+"'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:; img-src data:; font-src 'self' data:; frame-src 'none'; child-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
        String injection="<meta http-equiv=\"Content-Security-Policy\" content=\""+policy+"\"><script nonce=\""+nonce+"\">("+bridge+")("+settings+");</script>";
        Matcher head=TAG.matcher(html);while(head.find())if("head".equalsIgnoreCase(head.group(1)))return html.substring(0,head.end())+injection+html.substring(head.end());throw new IOException("Missing HTML head");
    }
    /** Capture only explicit UI fields, never arbitrary page objects or connection settings. */
    public static byte[] state(byte[] raw) throws Exception {
        RemoteUpdates.require(raw.length<=512*1024,"UI state too large");Map<String,Object> input=RemoteUpdates.object(RemoteUpdates.Json.parse(raw)),safe=new LinkedHashMap<>();RemoteUpdates.require(Long.valueOf(1).equals(input.get("version")),"Unsupported UI state version");safe.put("version",1L);
        for(String key:Arrays.asList("hostId","activeSid")){Object value=input.get(key);RemoteUpdates.require(value instanceof String&&((String)value).length()<=256,"Invalid selected UI target");safe.put(key,value);}
        for(String key:Arrays.asList("drafts","modes","uncertainDrafts")){Object value=key.equals("uncertainDrafts")&&!input.containsKey(key)?Collections.emptyList():input.get(key);RemoteUpdates.require(value instanceof List&&((List<?>)value).size()<=256,"Invalid UI rows");List<Object> rows=new ArrayList<>();
            for(Object row:(List<?>)value){RemoteUpdates.require(row instanceof List&&((List<?>)row).size()==2,"Invalid UI row");List<?> pair=(List<?>)row;RemoteUpdates.require(pair.get(0) instanceof String&&pair.get(1) instanceof String,"Invalid UI string");
                String name=(String)pair.get(0),text=(String)pair.get(1);Object tuple=RemoteUpdates.Json.parse(name.getBytes(StandardCharsets.UTF_8));RemoteUpdates.require(tuple instanceof List&&((List<?>)tuple).size()==2,"Invalid tuple identity");
                for(Object part:(List<?>)tuple)RemoteUpdates.require(part instanceof String&&((String)part).length()<=256,"Invalid tuple component");
                RemoteUpdates.require(text.length()<=65536&&(!key.equals("modes")||text.equals("buffered")||text.equals("direct")),"Invalid UI value");rows.add(Arrays.asList(name,text));
            }safe.put(key,rows);
        }return RemoteUpdates.Json.encode(safe);
    }
}
