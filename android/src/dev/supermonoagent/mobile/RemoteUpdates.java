package dev.supermonoagent.mobile;

import java.io.*;
import java.net.*;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.*;
import java.nio.file.*;
import java.security.*;
import java.security.interfaces.RSAPublicKey;
import java.security.spec.X509EncodedKeySpec;
import java.util.*;
import java.util.zip.*;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.SSLParameters;

/** Public, signed asset snapshots. No account, cookie, credential or native-code updates. */
public final class RemoteUpdates {
    public static final int NATIVE_API=1, MANIFEST_LIMIT=128*1024, FILE_LIMIT=4*1024*1024,
            TOTAL_LIMIT=5*1024*1024, ARCHIVE_LIMIT=8*1024*1024;
    public static final String DEFAULT_FEED="https://117.50.48.22/updates/stable/manifest.json";
    public static final List<String> FILES=Collections.unmodifiableList(Arrays.asList(
        "mobile/index.html","mobile/app.js","mobile/app.css","mobile/mobile-core.js",
        "mobile/vendor/xterm.js","mobile/vendor/xterm.css","mobile/vendor/xterm-fit.js",
        "mobile/vendor/xterm-unicode11.js","mobile/vendor/xterm-webgl.js","mobile/vendor/terminal-client.js"));
    private RemoteUpdates() { }
    static void require(boolean condition,String reason) throws IOException {if(!condition)throw new IOException(reason);}
    @SuppressWarnings("unchecked") public static Map<String,Object> object(Object value) throws IOException {
        require(value instanceof Map,"Expected object");return (Map<String,Object>)value;
    }
    static String string(Object value) throws IOException {require(value instanceof String,"Expected string");return (String)value;}
    static long integer(Object value) throws IOException {require(value instanceof Long,"Expected integer");return (Long)value;}
    public static String hash(byte[] value) throws Exception {
        StringBuilder result=new StringBuilder();for(byte b:MessageDigest.getInstance("SHA-256").digest(value))result.append(String.format(Locale.ROOT,"%02x",b&255));return result.toString();
    }
    static String digest(Object value) throws IOException {String text=string(value);require(text.matches("[a-f0-9]{64}"),"Invalid digest");return text;}
    public static String revision(Object value) throws IOException {
        String text=string(value);require(text.matches("[A-Za-z0-9][A-Za-z0-9._-]{0,79}")&&!text.contains("..")&&!text.equals("bundled"),"Invalid revision");return text;
    }
    public static byte[] read(InputStream stream,int limit) throws IOException {
        try(InputStream input=stream;ByteArrayOutputStream output=new ByteArrayOutputStream()){
            byte[] buffer=new byte[8192];int count;while((count=input.read(buffer))!=-1){require(output.size()+count<=limit,"Size limit exceeded");output.write(buffer,0,count);}return output.toByteArray();
        }
    }
    public static URI https(URI uri) throws IOException {
        require("https".equalsIgnoreCase(uri.getScheme())&&uri.getHost()!=null&&uri.getUserInfo()==null&&uri.getFragment()==null,"HTTPS URL required");return uri;
    }
    public static boolean sameOrigin(URI a,URI b){return a.getScheme().equalsIgnoreCase(b.getScheme())&&a.getHost().equalsIgnoreCase(b.getHost())&&(a.getPort()<0?443:a.getPort())==(b.getPort()<0?443:b.getPort());}
    public static byte[] download(URI uri,int limit) throws Exception {
        URI origin=https(uri);for(int redirects=0;redirects<6;redirects++){
            https(uri);String host=uri.getHost();if(host.startsWith("["))host=host.substring(1,host.length()-1);int port=uri.getPort()<0?443:uri.getPort();
            // A dedicated TLS socket never consults global CookieHandler or Authenticator.
            // System certificate and HTTPS hostname verification remain mandatory.
            try(Socket plain=new Socket()){
                plain.connect(new InetSocketAddress(host,port),10000);plain.setSoTimeout(15000);
                try(SSLSocket socket=(SSLSocket)((SSLSocketFactory)SSLSocketFactory.getDefault()).createSocket(plain,host,port,true)){
                    SSLParameters parameters=socket.getSSLParameters();parameters.setEndpointIdentificationAlgorithm("HTTPS");socket.setSSLParameters(parameters);socket.setSoTimeout(15000);socket.startHandshake();
                    String target=uri.getRawPath();if(target==null||target.isEmpty())target="/";if(uri.getRawQuery()!=null)target+="?"+uri.getRawQuery();
                    String authority=(host.contains(":")?"["+host+"]":host)+(port==443?"":":"+port);
                    socket.getOutputStream().write(("GET "+target+" HTTP/1.1\r\nHost: "+authority+"\r\nAccept: application/octet-stream\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));socket.getOutputStream().flush();
                    InputStream input=socket.getInputStream();long deadline=System.nanoTime()+30_000_000_000L;String statusLine=line(input,deadline);require(statusLine.matches("HTTP/1\\.[01] [0-9]{3}( .*)?"),"Invalid HTTP status");int status=Integer.parseInt(statusLine.substring(9,12));
                    Map<String,String> headers=new HashMap<>();int headerBytes=statusLine.length();while(true){String header=line(input,deadline);headerBytes+=header.length();require(headerBytes<=32768,"HTTP headers too large");if(header.isEmpty())break;int colon=header.indexOf(':');require(colon>0,"Invalid HTTP header");String key=header.substring(0,colon).toLowerCase(Locale.ROOT);require(!headers.containsKey(key),"Duplicate HTTP header");headers.put(key,header.substring(colon+1).trim());}
                    if(status>=300&&status<400){String location=headers.get("location");require(location!=null,"Invalid redirect");URI next=https(uri.resolve(location));require(sameOrigin(origin,next),"Cross-origin redirect refused");uri=next;continue;}
                    require(status==200,"Update feed unavailable");require(!headers.containsKey("content-encoding")||headers.get("content-encoding").equalsIgnoreCase("identity"),"Encoded update refused");
                    ByteArrayOutputStream output=new ByteArrayOutputStream();String transfer=headers.get("transfer-encoding"),length=headers.get("content-length");require(transfer==null||length==null,"Ambiguous HTTP body");
                    if(transfer!=null){require(transfer.equalsIgnoreCase("chunked"),"Unsupported HTTP transfer");while(true){String chunk=line(input,deadline).split(";",2)[0];require(chunk.matches("[0-9a-fA-F]{1,8}"),"Invalid HTTP chunk");long count=Long.parseLong(chunk,16);require(count<=limit-output.size(),"Download size limit");if(count==0){require(line(input,deadline).isEmpty(),"HTTP trailers refused");break;}copy(input,output,(int)count,limit,deadline);require(line(input,deadline).isEmpty(),"Invalid chunk terminator");}}
                    else if(length!=null){require(length.matches("[0-9]{1,10}"),"Invalid content length");long count=Long.parseLong(length);require(count<=limit,"Download size limit");copy(input,output,(int)count,limit,deadline);}
                    else{byte[] buffer=new byte[8192];int count;while((count=input.read(buffer))!=-1){require(System.nanoTime()<deadline&&output.size()+count<=limit,"Download deadline/size limit");output.write(buffer,0,count);}}
                    return output.toByteArray();
                }
            }
        }throw new IOException("Too many redirects");
    }
    private static String line(InputStream input,long deadline) throws IOException {
        ByteArrayOutputStream bytes=new ByteArrayOutputStream();int previous=-1;while(true){require(System.nanoTime()<deadline&&bytes.size()<8192,"HTTP deadline/header limit");int value=input.read();require(value!=-1,"Truncated HTTP header");if(previous==13&&value==10){byte[] raw=bytes.toByteArray();return new String(raw,0,raw.length-1,StandardCharsets.US_ASCII);}require(value==9||value==13||value>=32&&value<127,"Invalid HTTP header byte");bytes.write(value);previous=value;}
    }
    private static void copy(InputStream input,ByteArrayOutputStream output,int count,int limit,long deadline) throws IOException {
        byte[] buffer=new byte[8192];while(count>0){require(System.nanoTime()<deadline&&output.size()<=limit,"Download deadline/size limit");int n=input.read(buffer,0,Math.min(buffer.length,count));require(n>0,"Truncated HTTP body");output.write(buffer,0,n);count-=n;}
    }
    public static final class Candidate {
        public final String revision,sha256,payloadHash;public final long sequence,size;public final int nativeApi;
        public final URI url;public final Map<String,Object> files;public final byte[] envelope;
        Candidate(String rev,long seq,Map<String,Object> artifact,byte[] raw,URI feed) throws Exception {
            revision=rev;sequence=seq;sha256=digest(artifact.get("sha256"));size=integer(artifact.get("size"));payloadHash=hash(Base64.getDecoder().decode(string(object(Json.parse(raw)).get("payload"))));
            require(size>0&&size<=ARCHIVE_LIMIT,"Invalid archive size");long api=integer(artifact.get("native_api"));require(api>0&&api<=Integer.MAX_VALUE,"Invalid native API");nativeApi=(int)api;
            String relative=string(artifact.get("url"));url=https(feed.resolve(relative));require(sameOrigin(feed,url),"Artifact origin refused");
            files=object(artifact.get("files"));require(files.size()==FILES.size()&&files.keySet().containsAll(FILES),"Unexpected asset set");
            long total=0;for(String path:FILES){Map<String,Object> item=object(files.get(path));digest(item.get("sha256"));long n=integer(item.get("size"));require(n>=0&&n<=FILE_LIMIT,"Invalid file size");total+=n;}require(total<=TOTAL_LIMIT,"Snapshot too large");envelope=raw.clone();
        }
    }
    public static Candidate verify(byte[] raw,byte[] trust,URI feed,long now,boolean fresh) throws Exception {
        require(raw.length<=MANIFEST_LIMIT&&trust.length<=MANIFEST_LIMIT,"Manifest too large");https(feed);
        Map<String,Object> envelope=object(Json.parse(raw)),anchor=object(Json.parse(trust));
        require(integer(envelope.get("schema"))==1&&integer(anchor.get("schema"))==1,"Unknown schema");
        String id=string(envelope.get("key_id"));Map<String,Object> keys=object(anchor.get("keys"));require(keys.containsKey(id),"Untrusted signing key");
        byte[] payload=Base64.getDecoder().decode(string(envelope.get("payload"))),signature=Base64.getDecoder().decode(string(envelope.get("signature")));
        require(payload.length<=MANIFEST_LIMIT,"Payload too large");PublicKey key=KeyFactory.getInstance("RSA").generatePublic(new X509EncodedKeySpec(Base64.getDecoder().decode(string(keys.get(id)))));
        require(key instanceof RSAPublicKey&&((RSAPublicKey)key).getModulus().bitLength()>=3072,"Weak signing key");
        Signature verifier=Signature.getInstance("SHA256withRSA");verifier.initVerify(key);verifier.update(payload);require(verifier.verify(signature),"Invalid update signature");
        Map<String,Object> catalog=object(Json.parse(payload));require(integer(catalog.get("schema"))==1&&"stable".equals(catalog.get("channel")),"Unknown channel/schema");
        long sequence=integer(catalog.get("sequence")),created=integer(catalog.get("created")),expires=integer(catalog.get("expires"));
        require(sequence>0&&sequence<=9007199254740991L&&created>0&&expires>created,"Invalid release metadata");
        if(fresh)require(created<=now+300&&expires>now,"Expired or future release");
        return new Candidate(revision(catalog.get("revision")),sequence,object(object(catalog.get("artifacts")).get("android")),raw,feed);
    }
    /** Inspect central metadata too: ZipInputStream alone does not expose Unix symlink bits. */
    private static void central(byte[] bytes) throws Exception {
        ByteBuffer zip=ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);int end=-1;
        for(int i=bytes.length-22;i>=Math.max(0,bytes.length-65557);i--)if(zip.getInt(i)==0x06054b50&&i+22+(zip.getShort(i+20)&65535)==bytes.length){end=i;break;}
        require(end>=0,"Missing ZIP directory");int count=zip.getShort(end+10)&65535,offset=zip.getInt(end+16),length=zip.getInt(end+12);
        require(zip.getShort(end+4)==0&&zip.getShort(end+6)==0&&(zip.getShort(end+8)&65535)==count&&count==FILES.size()&&offset>=0&&length>=0&&offset+length==end,"Invalid ZIP directory");
        Set<String> names=new HashSet<>();int pos=offset;
        for(int i=0;i<count;i++){
            require(pos+46<=end&&zip.getInt(pos)==0x02014b50,"Invalid ZIP entry");int name=zip.getShort(pos+28)&65535,extra=zip.getShort(pos+30)&65535,comment=zip.getShort(pos+32)&65535,flags=zip.getShort(pos+8)&65535;
            require(pos+46+name+extra+comment<=end&&(flags&1)==0,"Invalid ZIP metadata");long attributes=zip.getInt(pos+38)&0xffffffffL;int type=(int)(attributes>>>16)&0170000;
            require(type==0||type==0100000,"Non-regular ZIP entry");String path=new String(bytes,pos+46,name,StandardCharsets.UTF_8);
            require(FILES.contains(path)&&names.add(path.toLowerCase(Locale.ROOT)),"Unexpected/duplicate ZIP entry");pos+=46+name+extra+comment;
        }require(pos==end,"Invalid ZIP directory length");
    }
    public static Map<String,byte[]> unpack(Candidate candidate,byte[] archive) throws Exception {
        require(candidate.nativeApi==NATIVE_API,"Native shell upgrade required");require(archive.length==candidate.size&&hash(archive).equals(candidate.sha256),"Archive hash/size mismatch");central(archive);
        Map<String,byte[]> result=new LinkedHashMap<>();int total=0;
        try(ZipInputStream zip=new ZipInputStream(new ByteArrayInputStream(archive))){ZipEntry entry;while((entry=zip.getNextEntry())!=null){
            String path=entry.getName();require(FILES.contains(path)&&!entry.isDirectory()&&!result.containsKey(path),"Unexpected ZIP path");
            ByteArrayOutputStream output=new ByteArrayOutputStream();byte[] buffer=new byte[8192];int count;
            while((count=zip.read(buffer))!=-1){require(output.size()+count<=FILE_LIMIT&&total+count<=TOTAL_LIMIT,"Expanded snapshot too large");output.write(buffer,0,count);total+=count;}
            byte[] bytes=output.toByteArray();Map<String,Object> info=object(candidate.files.get(path));require(bytes.length==integer(info.get("size"))&&hash(bytes).equals(info.get("sha256")),"Asset hash/size mismatch");result.put(path,bytes);
        }}require(result.size()==FILES.size(),"Missing ZIP files");return result;
    }
    public static final class Boot {
        public final String ticket,revision,generation;
        Boot(String ticket,String revision,String generation){this.ticket=ticket;this.revision=revision;this.generation=generation;}
    }
    public static final class Store {
        private static final java.util.concurrent.ConcurrentHashMap<String,Object> LOCKS=new java.util.concurrent.ConcurrentHashMap<>();
        private final Object lock;
        private final Path root;private final byte[] trust;private final URI feed;private Map<String,Object> state;private final java.util.function.LongSupplier clock;private final boolean recoveredDuringStart;
        public Store(Path root,byte[] trust,URI feed) throws Exception {
            this(root,trust,feed,System::currentTimeMillis);
        }
        public Store(Path root,byte[] trust,URI feed,java.util.function.LongSupplier clock) throws Exception {
            this.root=root.toAbsolutePath().normalize();this.lock=LOCKS.computeIfAbsent(this.root.toString(),key->new Object());this.trust=trust.clone();this.feed=https(feed);this.clock=clock;Files.createDirectories(this.root.resolve("snapshots"));
            Path pointer=this.root.resolve("state.json");state=Files.exists(pointer)?object(Json.parse(read(Files.newInputStream(pointer),MANIFEST_LIMIT))):new LinkedHashMap<String,Object>();
            if(!state.containsKey("highest"))state.put("highest",0L);
            recoveredDuringStart=Boolean.TRUE.equals(state.get("pending"));if(recoveredDuringStart)rollback();
        }
        public boolean recoveredOnStart(){return recoveredDuringStart;}
        public synchronized long highest() throws Exception{synchronized(lock){reload();return integer(state.get("highest"));}}
        public synchronized String active() throws Exception {synchronized(lock){reload();return (String)state.get("active");}}
        public synchronized boolean available(Candidate candidate) throws Exception {synchronized(lock){reload();return candidate.sequence>=highest()&&!candidate.revision.equals(active())&&!Objects.equals(candidate.sequence,state.get("failed_sequence"));}}
        public synchronized Candidate observe(byte[] envelope,long now) throws Exception {synchronized(lock){reload();
            Candidate candidate=verify(envelope,trust,feed,now,true);long highest=highest();require(candidate.sequence>=highest,"Release downgrade");
            if(candidate.sequence==highest)require(candidate.payloadHash.equals(state.get("payload_hash")),"Conflicting release sequence");
            else{state.put("highest",candidate.sequence);state.put("payload_hash",candidate.payloadHash);save();}return candidate;
        }}
        private void reload() throws Exception {Path pointer=root.resolve("state.json");if(Files.exists(pointer))state=object(Json.parse(read(Files.newInputStream(pointer),MANIFEST_LIMIT)));}
        private void save() throws Exception {atomic(root.resolve("state.json"),Json.encode(state));}
        private static void atomic(Path destination,byte[] bytes) throws Exception {
            Path temporary=destination.resolveSibling(destination.getFileName()+"."+UUID.randomUUID()+".tmp");
            try(FileOutputStream stream=new FileOutputStream(temporary.toFile())){stream.write(bytes);stream.getFD().sync();}
            try{Files.move(temporary,destination,StandardCopyOption.ATOMIC_MOVE,StandardCopyOption.REPLACE_EXISTING);}catch(AtomicMoveNotSupportedException error){Files.move(temporary,destination,StandardCopyOption.REPLACE_EXISTING);}
        }
        private void writeSnapshot(String revision,Map<String,byte[]> files,byte[] envelope) throws Exception {
            require(files.size()==FILES.size()&&files.keySet().containsAll(FILES),"Unexpected backup files");int total=0;for(byte[] bytes:files.values()){require(bytes!=null&&bytes.length<=FILE_LIMIT,"Oversized backup file");total+=bytes.length;}require(total<=TOTAL_LIMIT,"Oversized backup snapshot");
            require(!Files.exists(root.resolve("snapshots").resolve(revision)),"Revision already exists");Path stage=root.resolve("snapshots").resolve("stage-"+UUID.randomUUID());Files.createDirectory(stage);
            Map<String,Object> hashes=new LinkedHashMap<>();for(String path:FILES){byte[] bytes=files.get(path);require(bytes!=null,"Incomplete backup");Path file=stage.resolve(path);Files.createDirectories(file.getParent());Files.write(file,bytes);hashes.put(path,hash(bytes));}
            Files.write(stage.resolve("hashes.json"),Json.encode(hashes));if(envelope!=null)Files.write(stage.resolve("envelope.json"),envelope);
            Files.move(stage,root.resolve("snapshots").resolve(revision),StandardCopyOption.ATOMIC_MOVE);
        }
        public synchronized void install(byte[] envelope,byte[] archive,Map<String,byte[]> effective,long now) throws Exception {synchronized(lock){reload();
            Candidate candidate=observe(envelope,now);Map<String,byte[]> files=unpack(candidate,archive);
            if(candidate.revision.equals(active())&&Objects.equals(candidate.sequence,state.get("active_sequence")))return;
            require(available(candidate),"Downgrade/quarantined release");
            String previous=active();if(previous==null){previous="backup-"+UUID.randomUUID();writeSnapshot(previous,effective,null);}
            else snapshot(previous);
            writeSnapshot(candidate.revision,files,envelope);state.put("previous",previous);state.put("previous_sequence",state.get("active_sequence"));state.put("active",candidate.revision);state.put("active_sequence",candidate.sequence);state.put("install_generation",UUID.randomUUID().toString());state.put("pending",true);state.remove("ticket");save();
        }}
        public synchronized Map<String,byte[]> snapshot(String revision) throws Exception {
            if(revision==null)return null;require(revision.matches("[A-Za-z0-9][A-Za-z0-9._-]{0,79}")&&!revision.contains(".."),"Invalid local revision");
            Path directory=root.resolve("snapshots").resolve(revision);require(!Files.isSymbolicLink(directory)&&!Files.isSymbolicLink(directory.getParent()),"Unsafe snapshot directory");Map<String,Object> hashes=object(Json.parse(read(Files.newInputStream(directory.resolve("hashes.json")),MANIFEST_LIMIT)));
            require(hashes.size()==FILES.size()&&hashes.keySet().containsAll(FILES),"Incomplete local snapshot");Map<String,byte[]> result=new LinkedHashMap<>();int total=0;
            Candidate candidate=Files.exists(directory.resolve("envelope.json"))?verify(read(Files.newInputStream(directory.resolve("envelope.json")),MANIFEST_LIMIT),trust,feed,0,false):null;
            if(candidate!=null)require(candidate.revision.equals(revision)&&candidate.nativeApi==NATIVE_API,"Cached snapshot identity/API mismatch");
            for(String path:FILES){Path file=directory.resolve(path);require(!Files.isSymbolicLink(file)&&file.toRealPath().startsWith(directory.toRealPath()),"Unsafe local snapshot");byte[] bytes=read(Files.newInputStream(file),FILE_LIMIT);total+=bytes.length;require(total<=TOTAL_LIMIT&&hash(bytes).equals(digest(hashes.get(path))),"Corrupt local snapshot");if(candidate!=null){Map<String,Object> info=object(candidate.files.get(path));require(hash(bytes).equals(info.get("sha256"))&&bytes.length==integer(info.get("size")),"Corrupt signed snapshot");}result.put(path,bytes);}return result;
        }
        public synchronized Boot beginBoot() throws Exception {synchronized(lock){reload();
            if(!Boolean.TRUE.equals(state.get("pending")))return new Boot("",active(),"");String ticket=UUID.randomUUID().toString();state.put("ticket",ticket);state.put("boot_deadline",clock.getAsLong()+20000);save();return new Boot(ticket,active(),string(state.get("install_generation")));
        }}
        public synchronized boolean acknowledge(String ticket,String revision,String generation) throws Exception {synchronized(lock){reload();
            if(!Boolean.TRUE.equals(state.get("pending"))||!Objects.equals(state.get("ticket"),ticket)||!Objects.equals(state.get("install_generation"),generation)||!Objects.equals(active(),revision))return false;
            if(clock.getAsLong()>integer(state.get("boot_deadline"))){rollback();return false;}
            state.put("pending",false);state.remove("ticket");save();return true;
        }}
        public synchronized boolean fail(String ticket) throws Exception {synchronized(lock){reload();if(!Boolean.TRUE.equals(state.get("pending"))||!Objects.equals(state.get("ticket"),ticket))return false;rollback();return true;}}
        /** Explicit native user recovery. Keep signed snapshots/high water and all USB files. */
        public synchronized void useBundled() throws Exception {synchronized(lock){reload();state.remove("active");state.remove("previous");state.remove("active_sequence");state.remove("previous_sequence");state.remove("ticket");state.put("pending",false);save();}}
        public synchronized void rollback() throws Exception {synchronized(lock){reload();
            String failed=active(),previous=(String)state.get("previous");if(previous!=null){try{snapshot(previous);}catch(Exception corrupt){previous=null;}}
            if(failed!=null&&state.get("active_sequence") instanceof Long&&(!(state.get("failed_sequence") instanceof Long)||(Long)state.get("active_sequence")>(Long)state.get("failed_sequence"))){state.put("failed",failed);state.put("failed_sequence",state.get("active_sequence"));}if(previous==null)state.remove("active");else state.put("active",previous);state.put("active_sequence",state.get("previous_sequence"));
            state.remove("previous");state.remove("ticket");state.put("pending",false);save();
        }}
    }
    /** Small bounded JSON reader/writer shared by Android and the real JVM harness. */
    public static final class Json {
        private final String text;private int at=0;private Json(String text){this.text=text;}
        public static Object parse(byte[] bytes) throws Exception {
            String text=StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString();Json parser=new Json(text);Object result=parser.value(0);parser.space();require(parser.at==text.length(),"Trailing JSON");return result;
        }
        private void space(){while(at<text.length()&&" \r\n\t".indexOf(text.charAt(at))>=0)at++;}
        private char next() throws IOException {require(at<text.length(),"Truncated JSON");return text.charAt(at++);}
        private String quoted() throws IOException {
            require(next()=='"',"Expected JSON string");StringBuilder result=new StringBuilder();while(true){char c=next();if(c=='"')return result.toString();require(c>=32,"Control in JSON");if(c=='\\'){c=next();switch(c){case '"':case '\\':case '/':break;case 'b':c='\b';break;case 'f':c='\f';break;case 'n':c='\n';break;case 'r':c='\r';break;case 't':c='\t';break;case 'u':require(at+4<=text.length(),"Truncated escape");try{c=(char)Integer.parseInt(text.substring(at,at+4),16);}catch(NumberFormatException e){throw new IOException("Invalid escape");}at+=4;break;default:throw new IOException("Invalid escape");}}result.append(c);}
        }
        private Object value(int depth) throws Exception {
            require(depth<24,"JSON nesting limit");space();require(at<text.length(),"Truncated JSON");char c=text.charAt(at);
            if(c=='"')return quoted();if(c=='{'){at++;Map<String,Object> map=new LinkedHashMap<>();space();if(at<text.length()&&text.charAt(at)=='}'){at++;return map;}while(true){space();String key=quoted();require(!map.containsKey(key),"Duplicate JSON key");space();require(next()==':',"Expected colon");map.put(key,value(depth+1));space();c=next();if(c=='}')return map;require(c==',',"Expected comma");}}
            if(c=='['){at++;List<Object> list=new ArrayList<>();space();if(at<text.length()&&text.charAt(at)==']'){at++;return list;}while(true){list.add(value(depth+1));space();c=next();if(c==']')return list;require(c==',',"Expected comma");}}
            for(String word:Arrays.asList("true","false","null"))if(text.startsWith(word,at)){at+=word.length();return word.equals("null")?null:Boolean.valueOf(word);}
            int start=at;if(c=='-')at++;while(at<text.length()&&Character.isDigit(text.charAt(at)))at++;String number=text.substring(start,at);require(number.matches("-?(0|[1-9][0-9]*)"),"Invalid JSON integer");return Long.valueOf(number);
        }
        public static byte[] encode(Object value) throws IOException{return render(value).getBytes(StandardCharsets.UTF_8);}
        public static String render(Object value) throws IOException {
            if(value==null)return "null";if(value instanceof Boolean||value instanceof Long||value instanceof Integer)return value.toString();
            if(value instanceof String){StringBuilder out=new StringBuilder("\"");for(char c:((String)value).toCharArray()){if(c=='"'||c=='\\')out.append('\\').append(c);else if(c<32)out.append(String.format(Locale.ROOT,"\\u%04x",(int)c));else out.append(c);}return out.append('"').toString();}
            if(value instanceof Map){List<String> items=new ArrayList<>();for(Map.Entry<?,?> item:((Map<?,?>)value).entrySet())items.add(render(item.getKey())+":"+render(item.getValue()));return "{"+String.join(",",items)+"}";}
            if(value instanceof List){List<String> items=new ArrayList<>();for(Object item:(List<?>)value)items.add(render(item));return "["+String.join(",",items)+"]";}throw new IOException("Unsupported JSON value");
        }
    }
}
