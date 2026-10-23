"""Exercise the native credential envelope with real AES/GCM and endpoint scopes."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'android/src/dev/supermonoagent/mobile'


@pytest.fixture(scope='module')
def saved_tokens(tmp_path_factory):
    javac = shutil.which('javac') or next((str(p) for p in (ROOT / '.omx/tools/jdk').glob('*/bin/javac.exe')), None)
    android = ROOT / '.omx/tools/android-sdk/platforms/android-35/android.jar'
    if not javac or not android.exists():
        pytest.skip('Local Android SDK and JDK required')
    java = str(Path(javac).with_name('java.exe' if javac.endswith('.exe') else 'java'))
    directory = tmp_path_factory.mktemp('saved-native-tokens')
    harness = directory / 'SavedTokensCheck.java'
    harness.write_text(r'''package dev.supermonoagent.mobile;
import java.util.*;
import javax.crypto.spec.SecretKeySpec;
import javax.crypto.SecretKey;
public class SavedTokensCheck {
 static final String URL="https://example.test";
 static final SecretKey KEY=new SecretKeySpec(new byte[32],"AES");
 static void check(boolean value){if(!value)throw new AssertionError("Credential invariant failed");}
 interface Work{void run()throws Exception;}
 static void reject(Work task)throws Exception{try{task.run();}catch(Exception expected){return;}throw new AssertionError("Accepted invalid envelope");}
 public static void main(String[] args)throws Exception {
  String mode=args[0],scope=SavedConnectionTokens.canonicalEndpoint(URL);
  String token="saved-token-fixture-123";
  String sealed=SavedConnectionTokens.seal(scope,token,KEY);
  if(mode.equals("roundtrip")){
   for(String value:new String[]{token,"Unicode-令牌-🔑",String.join("",Collections.nCopies(4096,"x"))})
    check(value.equals(SavedConnectionTokens.unseal(scope,SavedConnectionTokens.seal(scope,value,KEY),KEY)));
  }else if(mode.equals("random")){
   Set<String> values=new HashSet<>();for(int i=0;i<20;i++)values.add(SavedConnectionTokens.seal(scope,token,KEY));check(values.size()==20);
   check(!sealed.contains(token));check(!new String(Base64.getDecoder().decode(sealed),java.nio.charset.StandardCharsets.UTF_8).contains(token));
  }else if(mode.equals("scope")){
   for(String other:new String[]{"https://other.test","https://example.test:8443","https://example.test/private","http://127.0.0.1"})
    reject(()->SavedConnectionTokens.unseal(SavedConnectionTokens.canonicalEndpoint(other),sealed,KEY));
  }else if(mode.equals("key")){
   byte[] other=new byte[32];other[0]=1;reject(()->SavedConnectionTokens.unseal(scope,sealed,new SecretKeySpec(other,"AES")));
  }else if(mode.equals("tamper")){
   byte[] data=Base64.getDecoder().decode(sealed);data[data.length-1]^=1;
   reject(()->SavedConnectionTokens.unseal(scope,Base64.getEncoder().encodeToString(data),KEY));
  }else if(mode.equals("format")){
   for(String bad:new String[]{"","not-base64!",Base64.getEncoder().encodeToString(new byte[32]),String.join("",Collections.nCopies(32769,"A"))})
    reject(()->SavedConnectionTokens.unseal(scope,bad,KEY));
   reject(()->SavedConnectionTokens.seal(scope,"",KEY));reject(()->SavedConnectionTokens.seal(scope,null,KEY));
   reject(()->SavedConnectionTokens.seal(scope,String.join("",Collections.nCopies(4097,"x")),KEY));
  }else if(mode.equals("edit-policy")){
   SavedConnectionTokens.EditState edit=new SavedConnectionTokens.EditState();
   check(edit.action(true,"")==SavedConnectionTokens.EditState.Action.KEEP);
   edit.beginLoad();edit.changed();edit.endLoad();
   check(edit.action(true,"")==SavedConnectionTokens.EditState.Action.KEEP);
   check(edit.action(true,token)==SavedConnectionTokens.EditState.Action.KEEP);
   edit.changed();check(edit.action(true,token)==SavedConnectionTokens.EditState.Action.SAVE);edit.committed();
   check(edit.action(true,token)==SavedConnectionTokens.EditState.Action.KEEP);
   edit.changed();check(edit.action(true,"")==SavedConnectionTokens.EditState.Action.REMOVE);edit.committed();
   edit.forget();check(edit.action(false,token)==SavedConnectionTokens.EditState.Action.REMOVE);edit.committed();
   edit.changed();check(edit.action(false,token)==SavedConnectionTokens.EditState.Action.KEEP);
   check(edit.action(true,token)==SavedConnectionTokens.EditState.Action.SAVE);
   edit.beginLoad();edit.changed();edit.endLoad();check(edit.action(true,"")==SavedConnectionTokens.EditState.Action.KEEP);
  }else if(mode.equals("canonical")){
   check(scope.equals(SavedConnectionTokens.canonicalEndpoint("HTTPS://EXAMPLE.TEST:443/")));
   check(SavedConnectionTokens.canonicalEndpoint("http://LOCALHOST:80/").equals("http://localhost"));
   check(SavedConnectionTokens.canonicalEndpoint("https://[::1]:443/").equals("https://[::1]"));
   check(SavedConnectionTokens.canonicalEndpoint("http://117.50.48.22:8817/ws").equals("https://117.50.48.22"));
   check(!scope.equals(SavedConnectionTokens.canonicalEndpoint(URL+"/ws")));
   reject(()->SavedConnectionTokens.canonicalEndpoint("https://user:pass@example.test"));
   reject(()->SavedConnectionTokens.canonicalEndpoint("https://example.test?token=fixture"));
   reject(()->SavedConnectionTokens.canonicalEndpoint("http://example.test"));
  }else throw new AssertionError("Unknown check");
  System.out.print("ok");
 }
}''', encoding='utf-8')
    subprocess.run([javac, '-encoding', 'UTF-8', '-classpath', str(android), '-d', str(directory),
                    str(PACKAGE / 'ConnectionEndpoint.java'), str(PACKAGE / 'SavedConnectionTokens.java'), str(harness)], check=True, capture_output=True)
    def run(mode):
        return subprocess.run([java, '-cp', str(directory)+os.pathsep+str(android),
                               'dev.supermonoagent.mobile.SavedTokensCheck', mode], capture_output=True, check=True, timeout=8).stdout.decode()
    return run


@pytest.mark.parametrize('check', ['roundtrip', 'random', 'scope', 'key', 'tamper', 'format', 'canonical', 'edit-policy'])
def test_native_saved_credentials_enforce_authenticated_endpoint_binding(saved_tokens, check):
    assert saved_tokens(check) == 'ok'
