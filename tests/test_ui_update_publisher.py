"""Public bundle signing and transactional publisher tests; no network or CA calls."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile
from supermonoagent import ui_updates as update_core

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('ui_update_release',ROOT/'scripts/ui_update_release.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)
BASH=shutil.which('bash') or r'D:/Program Files/Git/bin/bash.exe'


class LocalSSH:
    """Execute generated remote shell against a disposable local directory, not SSH."""
    def __init__(self,root,corrupt=False):self.root=root;self.corrupt=corrupt;self.calls=[]
    def __call__(self,arguments,**kwargs):
        self.calls.append((arguments,kwargs.get('input',b'').decode()))
        if arguments[0]=='scp':
            destination=arguments[-1].split(':',1)[1].replace('/fixture-root',self.root.as_posix())
            for source in arguments[-4:-1]:shutil.copy2(source,Path(destination)/Path(source).name)
            if self.corrupt:
                with (Path(destination)/'desktop.zip').open('ab') as stream:stream.write(b'transfer-corruption')
            return b''
        script=kwargs['input'].decode().replace('/fixture-root',self.root.as_posix())
        script=script.replace('python3 - ',release.shlex.quote(Path(sys.executable).as_posix())+' - ')
        result=subprocess.run([BASH,'-s'],input=script.encode(),capture_output=True,timeout=20)
        if result.returncode:raise RuntimeError('Isolated remote shell rejected stage: '+result.stderr.decode(errors='replace'))
        return result.stdout


class KilledFinalizerSSH(LocalSSH):
    """Kill the test-owned Python transaction after its real OS lock is held."""
    def __call__(self,arguments,**kwargs):
        script=kwargs.get('input',b'').decode()
        marker='    # Publication lock held: OS releases it when this process exits or is killed.'
        if arguments[0]!='ssh' or marker not in script:return super().__call__(arguments,**kwargs)
        script=script.replace('/fixture-root',self.root.as_posix())
        script=script.replace('python3 - ',release.shlex.quote(Path(sys.executable).as_posix())+' - ')
        script=script.replace(marker,marker+"\n    import time\n    (root/'qa-remote-lock-pid').write_text(str(os.getpid()))\n    time.sleep(60)")
        process=subprocess.Popen([BASH,'-s'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        process.stdin.write(script.encode());process.stdin.close();process.stdin=None
        pid_file=self.root/'qa-remote-lock-pid'
        try:
            deadline=time.monotonic()+10
            while not pid_file.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
            if not pid_file.exists():raise AssertionError('Test-owned remote transaction never acquired lock')
            child=int(pid_file.read_text());os.kill(child,signal.SIGTERM)
            process.communicate(timeout=10)
            if process.returncode==0:raise AssertionError('Killed transaction unexpectedly succeeded')
            raise RuntimeError('Deliberately killed test-owned remote finalizer')
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=5)


class PublisherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool=release.openssl_tool();cls.trust=json.loads((ROOT/'src/supermonoagent/static/update-trust.json').read_text())

    def test_signed_complete_whitelists_and_secret_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ,{'SUPERMONO_DEFAULT_RELAY':'https://fixture-secret@example.invalid/?token=fixture-secret'}):
                folder=release.package(Path(tmp)/'out',revision='publisher-qa',openssl=self.tool)
            value=release.verify_local(folder,self.trust,self.tool)
            self.assertEqual(release.NATIVE_API,2)
            self.assertEqual(len(release.DESKTOP),17)
            self.assertEqual(len(release.ANDROID),16)
            self.assertTrue(all(item['native_api']==2 for item in value['artifacts'].values()))
            self.assertLessEqual(value['sequence'],release.SAFE_INTEGER)
            for platform,allowed in [('desktop',release.DESKTOP),('android',release.ANDROID)]:
                native_payload=update_core.verify_envelope((folder/'manifest.json').read_bytes(),self.trust,platform)
                self.assertEqual(set(update_core.verify_archive(native_payload,(folder/(platform+'.zip')).read_bytes(),platform)),set(allowed))
                with zipfile.ZipFile(folder/(platform+'.zip')) as archive:
                    self.assertEqual(set(archive.namelist()),set(allowed))
                    self.assertFalse(any('trust' in name or 'private' in name or 'native-updates' in name for name in archive.namelist()))
                    self.assertLessEqual(sum(info.file_size for info in archive.infolist()),release.MAX_SNAPSHOT)
                    if platform=='desktop':
                        self.assertNotIn(b'fixture-secret',archive.read('workspace.html'))
            with self.assertRaisesRegex(ValueError,'immutable'):
                release.package(Path(tmp)/'out',revision='publisher-qa',openssl=self.tool)
            second=release.package(Path(tmp)/'out',revision='publisher-qa-2',openssl=self.tool)
            self.assertGreater(release.verify_local(second,self.trust,self.tool)['sequence'],value['sequence'])
            with (folder/'android.zip').open('ab') as stream:stream.write(b'changed')
            with self.assertRaisesRegex(ValueError,'hash/size'):
                release.verify_local(folder,self.trust,self.tool)

    def test_build_only_existing_release_never_invokes_ssh(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=release.package(Path(tmp)/'out',revision='build-only-qa',openssl=self.tool)
            with patch.object(sys,'argv',['publish-ui-update.py','--release',str(folder),'--build-only','--ssh-target','qa@localhost']),patch.object(release,'publish',side_effect=AssertionError('build-only must not publish')):
                release.main(publishing=True)

    def test_unsafe_destinations_rejected_before_commands(self):
        for value in ['../bad','bundled','a..b','a/b','bad;touch']:
            with self.assertRaises(ValueError):release.revision_id(value)
        for value in ['http://example.test/manifest.json','https://secret@example.test/x','https://example.test/x?token=bad']:
            with self.assertRaises(ValueError):release.feed_url(value)
        for value in ['-oProxyCommand=bad','host;touch','user@host$(bad)']:
            with self.assertRaises(ValueError):release.ssh_target(value)
        for value in ['/var/../etc','/tmp/x;touch','relative','/tmp//path']:
            with self.assertRaises(ValueError):release.remote_root(value)

    @unittest.skipUnless(os.name=='nt','Windows byte-range lock regression')
    def test_empty_builder_lock_held_by_another_process_waits_without_writing(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)
            script="import sys,msvcrt; f=open(sys.argv[1],'a+b'); msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1); print('LOCKED',flush=True); sys.stdin.buffer.read(1); f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1); f.close()"
            holder=subprocess.Popen([sys.executable,'-c',script,str(output/'.package-lock')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            release_thread=None
            try:
                self.assertEqual(holder.stdout.readline().strip(),b'LOCKED')
                def release_lock():
                    time.sleep(.2);holder.stdin.write(b'\n');holder.stdin.flush()
                release_thread=threading.Thread(target=release_lock);release_thread.start()
                with release.build_lock(output):
                    self.assertEqual((output/'.package-lock').stat().st_size,0)
            finally:
                if release_thread is not None:release_thread.join(timeout=3)
                if holder.poll() is None and release_thread is None:holder.terminate()
                holder.communicate(timeout=5)

    def test_killed_local_builder_releases_os_lock_and_can_build_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'channel'
            script='import sys,time\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom ui_update_release import build_lock\nwith build_lock(Path(sys.argv[2])):\n print("LOCKED",flush=True)\n time.sleep(60)\n'
            process=subprocess.Popen([sys.executable,'-u','-c',script,str(ROOT/'scripts'),str(output)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(),'LOCKED')
                process.kill();process.wait(timeout=5)
                self.assertTrue((output/'.package-lock').is_file())
                folder=release.package(output,revision='after-killed-builder',openssl=self.tool)
                self.assertTrue((folder/'manifest.json').is_file())
            finally:
                if process.poll() is None:process.kill();process.wait(timeout=5)

    @unittest.skipUnless(Path(BASH).exists(),'Local bash is needed for isolated SSH transaction')
    def test_killed_remote_finalizer_releases_os_lock_and_retry_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);remote=tmp/'remote';first=release.package(tmp/'local',revision='before-crash',openssl=self.tool)
            release.publish(first,target='qa@localhost',remote='/fixture-root',trust=self.trust,openssl=self.tool,runner=LocalSSH(remote))
            previous=(remote/'manifest.json').read_bytes();second=release.package(tmp/'local',revision='after-crash',openssl=self.tool)
            with self.assertRaisesRegex(RuntimeError,'Deliberately killed'):
                release.publish(second,target='qa@localhost',remote='/fixture-root',trust=self.trust,openssl=self.tool,runner=KilledFinalizerSSH(remote))
            self.assertEqual((remote/'manifest.json').read_bytes(),previous)
            self.assertTrue((remote/'.publish-lock').is_file())
            release.publish(second,target='qa@localhost',remote='/fixture-root',trust=self.trust,openssl=self.tool,runner=LocalSSH(remote))
            self.assertEqual((remote/'manifest.json').read_bytes(),(second/'manifest.json').read_bytes())
            self.assertTrue((remote/'releases/before-crash/desktop.zip').is_file())

    @unittest.skipUnless(Path(BASH).exists(),'Local bash is needed to execute generated Linux publication shell')
    def test_stage_verify_publish_idempotence_and_failed_transfer_keep_old_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);first=release.package(tmp/'local',revision='first',openssl=self.tool)
            remote=tmp/'remote';runner=LocalSSH(remote)
            release.publish(first,target='qa@localhost',remote='/fixture-root',trust=self.trust,openssl=self.tool,runner=runner,stage_only=True)
            self.assertFalse((remote/'manifest.json').exists())
            self.assertTrue((remote/'releases/first/desktop.zip').is_file())
            release.publish(first,target='qa@localhost',remote='/fixture-root',trust=self.trust,openssl=self.tool,runner=runner)
            previous=(remote/'manifest.json').read_bytes()
            self.assertEqual(previous,(first/'manifest.json').read_bytes())
            release.publish(first,target='qa@localhost',remote='/fixture-root',trust=self.trust,openssl=self.tool,runner=runner)
            second=release.package(tmp/'local',revision='second',openssl=self.tool)
            with self.assertRaises(RuntimeError):
                release.publish(second,target='qa@localhost',remote='/fixture-root',trust=self.trust,openssl=self.tool,runner=LocalSSH(remote,corrupt=True))
            self.assertEqual((remote/'manifest.json').read_bytes(),previous)
            self.assertTrue((remote/'releases/first/desktop.zip').is_file())
            self.assertFalse((remote/'releases/second').exists())
            self.assertFalse(list(remote.glob('.incoming-*')))
            self.assertTrue((remote/'.publish-lock').is_file())
            conflicting=release.package(tmp/'conflicting',revision='first',openssl=self.tool)
            with self.assertRaises(RuntimeError):
                release.publish(conflicting,target='qa@localhost',remote='/fixture-root',trust=self.trust,openssl=self.tool,runner=runner)
            self.assertEqual((remote/'manifest.json').read_bytes(),previous)
            release.publish(second,target='qa@localhost',remote='/fixture-root',trust=self.trust,openssl=self.tool,runner=runner)
            with self.assertRaises(RuntimeError):
                release.publish(first,target='qa@localhost',remote='/fixture-root',trust=self.trust,openssl=self.tool,runner=runner)
            self.assertEqual((remote/'manifest.json').read_bytes(),(second/'manifest.json').read_bytes())


if __name__=='__main__':unittest.main()
