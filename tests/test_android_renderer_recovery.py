"""Actual Java retirement policy and Store regressions; no Android/ADB/device operations."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]
PACKAGE=ROOT/'android/src/dev/supermonoagent/mobile'
HARNESS=r'''
import dev.supermonoagent.mobile.*;
import java.nio.file.*;
import java.net.URI;
import java.util.*;
public class RecoveryCheck {
 static void check(boolean value,String why){if(!value)throw new AssertionError(why);}
 static void action(RendererRecovery.Action actual,RendererRecovery.Action expected){check(actual==expected,"Wrong action: "+actual+" expected "+expected);}
 static void statePolicy(){
  RendererRecovery policy=new RendererRecovery();Object bad=new Object(),good=new Object();
  policy.attach(bad,1,true);
  action(policy.failure(bad,1,false),RendererRecovery.Action.RELOAD_HEALTHY);
  action(policy.failure(bad,1,false),RendererRecovery.Action.IGNORE);
  check(!policy.waitingForRenderer(),"A JS throw must not terminate a healthy renderer");
  policy.attach(bad,2,false); // Same healthy WebView, previous verified snapshot.
  action(policy.rendererGone(bad,1),RendererRecovery.Action.IGNORE);
  action(policy.rendererGone(bad,2),RendererRecovery.Action.RECREATE_GOOD);
  policy.attach(good,3,true);check(policy.ready(good,3),"Healthy ack rejected");
  action(policy.failure(good,3,true),RendererRecovery.Action.IGNORE);
  action(policy.rendererGone(good,3),RendererRecovery.Action.RECREATE_GOOD);
  policy.attach(bad,4,true);
  action(policy.failure(bad,4,true),RendererRecovery.Action.WAIT_FOR_RENDERER);
  action(policy.failure(bad,4,true),RendererRecovery.Action.IGNORE);
  check(policy.waitingForRenderer()&&policy.isRetiring(bad),"Retirement latch missing");
  try{policy.attach(good,5,false);throw new AssertionError("Created recovery view before renderer died");}catch(IllegalStateException expected){}
  action(policy.rendererGone(bad,3),RendererRecovery.Action.IGNORE);check(policy.waitingForRenderer(),"Old generation released retirement");
  action(policy.rendererGone(good,4),RendererRecovery.Action.IGNORE);check(policy.waitingForRenderer(),"Wrong view released retirement");
  action(policy.rendererGone(bad,4),RendererRecovery.Action.RETIRED_RECOVER);
  policy.attach(good,5,false);
  action(policy.rendererGone(bad,4),RendererRecovery.Action.IGNORE);
  action(policy.rendererGone(good,5),RendererRecovery.Action.RECREATE_GOOD);
  policy.attach(bad,6,true);action(policy.failure(bad,6,true),RendererRecovery.Action.WAIT_FOR_RENDERER);
  policy.cancel();check(policy.waitingForRenderer(),"Settings cancellation released a still-dying renderer");
  action(policy.rendererGone(bad,6),RendererRecovery.Action.RETIRED_CANCELLED);check(!policy.waitingForRenderer(),"Cancelled renderer never retired");
  policy.attach(good,7,false);action(policy.rendererGone(bad,6),RendererRecovery.Action.IGNORE);
  policy.attach(bad,8,true);action(policy.rendererGone(bad,8),RendererRecovery.Action.FAIL_PENDING);
  action(policy.rendererGone(bad,8),RendererRecovery.Action.IGNORE);
  policy.attach(bad,9,true);action(policy.failure(bad,9,true),RendererRecovery.Action.WAIT_FOR_RENDERER);
  action(policy.finishWithoutTermination(bad,9),RendererRecovery.Action.RETIRED_RECOVER);
 }
 static Map<String,Object> pointer(Path root)throws Exception{return RemoteUpdates.object(RemoteUpdates.Json.parse(Files.readAllBytes(root.resolve("state.json"))));}
 static void store(Path root)throws Exception{
  byte[] trust="{}".getBytes("UTF-8");RemoteUpdates.Store store=new RemoteUpdates.Store(root,trust,URI.create("https://updates.example.test/manifest.json"));
  Path previous=root.resolve("snapshots/good-two");Files.createDirectories(previous);Map<String,Object> hashes=new LinkedHashMap<>();
  for(String name:RemoteUpdates.FILES){byte[] bytes=("verified effective good-two "+name).getBytes("UTF-8");Path file=previous.resolve(name);Files.createDirectories(file.getParent());Files.write(file,bytes);hashes.put(name,RemoteUpdates.hash(bytes));}
  Files.write(previous.resolve("hashes.json"),RemoteUpdates.Json.encode(hashes));
  Map<String,Object> state=new LinkedHashMap<>();state.put("highest",3L);state.put("active","bad-three");state.put("active_sequence",3L);state.put("previous","good-two");state.put("previous_sequence",2L);state.put("pending",true);state.put("ticket","boot-three");state.put("install_generation","generation-three");state.put("boot_deadline",999999L);
  Files.write(root.resolve("state.json"),RemoteUpdates.Json.encode(state));
  check(store.fail("boot-three"),"Pending startup did not fail");check("good-two".equals(store.active()),"First failure lost verified good");
  Map<String,Object> restored=pointer(root);check(Long.valueOf(2).equals(restored.get("active_sequence")),"Previous sequence not restored");check(!restored.containsKey("previous_sequence"),"Previous sequence survived consuming previous pointer");
  check(!store.fail("boot-three"),"Same startup ticket failed twice");check("good-two".equals(store.active()),"Repeated failure downgraded good to bundled");
  check(store.highest()==3&&Long.valueOf(3).equals(pointer(root).get("failed_sequence")),"Failure downgraded high water/quarantine");
  store.rollback();Map<String,Object> fallback=pointer(root);check(store.active()==null,"No previous should select bundled");
  for(String key:Arrays.asList("active_sequence","previous","previous_sequence","ticket","boot_deadline","install_generation"))check(!fallback.containsKey(key),"Fallback retained stale field: "+key);
  check(store.highest()==3&&Long.valueOf(3).equals(fallback.get("failed_sequence")),"Bundled fallback downgraded high water/quarantine");
 }
 public static void main(String[] args)throws Exception{if(args[0].equals("policy"))statePolicy();else store(Paths.get(args[1]));System.out.println("ok");}
}
'''

@pytest.fixture(scope='module')
def run_java(tmp_path_factory):
    javac=shutil.which('javac') or next((str(p) for p in (ROOT/'.omx/tools/jdk').glob('*/bin/javac.exe')),None)
    if not javac:pytest.skip('Portable JDK unavailable')
    directory=tmp_path_factory.mktemp('renderer-recovery-jvm');source=directory/'RecoveryCheck.java';source.write_text(HARNESS,'utf-8')
    subprocess.run([javac,'-encoding','UTF-8','-d',str(directory),str(PACKAGE/'RendererRecovery.java'),str(PACKAGE/'RemoteUpdates.java'),str(source)],check=True,capture_output=True)
    java=str(Path(javac).with_name('java.exe' if javac.endswith('.exe') else 'java'))
    def run(*args):
        result=subprocess.run([java,'-cp',str(directory),'RecoveryCheck',*map(str,args)],capture_output=True,text=True,timeout=15)
        assert result.returncode==0,result.stderr
    return run

def test_retirement_policy_rejects_duplicate_late_and_cancelled_callbacks(run_java):
    run_java('policy')

def test_store_duplicate_ticket_and_fallback_pointer_sequences_are_consistent(run_java,tmp_path):
    run_java('store',tmp_path/'store')

def test_native_adapter_waits_for_death_and_never_rolls_back_acknowledged_good():
    source=(PACKAGE/'MainActivity.java').read_text('utf-8')
    retirement=source[source.index('    private void retireFailedRenderer('):source.index('    private void rendererGone(')]
    api29=retirement[:retirement.index('// API26-28:')]
    assert 'process.terminate())return' in api29 and 'openTerminal(' not in api29
    gone=source[source.index('    private void rendererGone('):source.index('    private void checkUpdates(')]
    assert 'rendererRecovery.rendererGone(web,generation)' in gone
    assert 'RETIRED_RECOVER' in gone and 'documentGeneration==retirementRecoveryGeneration' in gone
    assert 'if(action==RendererRecovery.Action.FAIL_PENDING)failPendingStartup(boot)' in gone
    assert 'updateStore.rollback()' not in gone
    assert '"UI startup deadline exceeded",true' in source
    destroy=source[source.index('    private void destroyTerminal()'):source.index('    private void showConnection()')]
    assert 'if(!rendererRecovery.isRetiring(previous))destroyRendererView(previous)' in destroy
    assert 'documentGeneration++;rendererRecovery.cancel()' in source
