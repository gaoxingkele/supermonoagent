"""Real OpenSSL publisher signatures, Windows built-in CNG verification and atomic stores."""
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from unittest.mock import patch
import zipfile

from supermonoagent import ui_updates as U

OPENSSL=shutil.which('openssl') or next((str(path) for path in [Path('D:/Espressif/tools/idf-git/2.44.0/mingw64/bin/openssl.exe'),Path('D:/KimiData/daimon-bundle/runtime/git/mingw64/bin/openssl.exe')] if path.exists()),None)


def sign(payload,private,trust):
    raw=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
    signature=subprocess.run([OPENSSL,'dgst','-sha256','-sign',str(private)],input=raw,capture_output=True,check=True).stdout
    return json.dumps({'schema':1,'key_id':next(iter(trust['keys'])),'payload':base64.b64encode(raw).decode(),'signature':base64.b64encode(signature).decode()}).encode()


def release(private,trust,sequence=1,revision=None,mutate=None,zip_mutate=None,native_api=U.NATIVE_API):
    revision=revision or 'test-'+str(sequence)
    now=int(time.time())
    payload={'schema':1,'channel':'stable','revision':revision,'sequence':sequence,'created':now-10,'expires':now+86400,'artifacts':{}}
    archives={}; files={}
    for platform,names in U.FILES.items():
        files[platform]={name:('complete '+revision+' '+name).encode() for name in names}
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,data in files[platform].items():
                archive.writestr(name,data)
            if zip_mutate:
                with warnings.catch_warnings():
                    warnings.filterwarnings('ignore',message='Duplicate name:.*',category=UserWarning)
                    zip_mutate(archive,platform)
        archives[platform]=output.getvalue()
        payload['artifacts'][platform]={'url':'releases/'+revision+'/'+platform+'.zip','sha256':hashlib.sha256(archives[platform]).hexdigest(),'size':len(archives[platform]),'native_api':native_api,'files':{name:{'sha256':hashlib.sha256(data).hexdigest(),'size':len(data)} for name,data in files[platform].items()}}
    if mutate:
        mutate(payload)
    return sign(payload,private,trust),archives,payload,files


@unittest.skipUnless(OPENSSL,'Publisher OpenSSL unavailable')
class UiUpdatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory=tempfile.TemporaryDirectory()
        cls.private=Path(cls.directory.name)/'test-private.pem'
        subprocess.run([OPENSSL,'genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',str(cls.private)],capture_output=True,check=True)
        der=subprocess.run([OPENSSL,'pkey','-in',str(cls.private),'-pubout','-outform','DER'],capture_output=True,check=True).stdout
        cls.trust={'schema':1,'keys':{hashlib.sha256(der).hexdigest()[:16]:base64.b64encode(der).decode()}}
        cls.baseline={name:('bundled '+name).encode() for name in U.FILES['desktop']}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def candidate(self,*args,**kwargs):
        return release(self.private,self.trust,*args,**kwargs)

    def test_real_signature_cng_no_external_verifier_and_exact_utf8_payload(self):
        envelope,archives,payload,_=self.candidate()
        if sys.platform=='win32':
            with patch.object(U.subprocess,'run',side_effect=AssertionError('Windows runtime must not invoke OpenSSL')):
                self.assertEqual(U.verify_envelope(envelope,self.trust,'desktop'),payload)
        else:
            self.assertEqual(U.verify_envelope(envelope,self.trust,'desktop'),payload)
        U.verify_archive(payload,archives['android'],'android')
        bad=json.loads(envelope); bad['payload']=base64.b64encode(b'{}').decode()
        with self.assertRaises(U.UpdateError):
            U.verify_envelope(json.dumps(bad).encode(),self.trust,'desktop')
        bad=json.loads(envelope); bad['key_id']='unknown'
        with self.assertRaises(U.UpdateError):
            U.verify_envelope(json.dumps(bad).encode(),self.trust,'desktop')
        with self.assertRaises(U.UpdateError):
            U.verify_envelope(envelope.decode().encode('utf-16'),self.trust,'desktop')

    def test_signed_expiry_future_schema_sequence_native_and_allowlist_rejections(self):
        mutations=[lambda p:p.update(expires=p['created']-1),lambda p:p.update(created=int(time.time())+301,expires=int(time.time())+86400),lambda p:p.update(schema=True),lambda p:p.update(sequence=True),lambda p:p.update(revision='../escape'),lambda p:p['artifacts']['desktop'].update(native_api=U.NATIVE_API+1),lambda p:p['artifacts']['desktop']['files'].update({'update-trust.json':{'size':0,'sha256':'0'*64}}),lambda p:p['artifacts']['desktop'].update(size=U.MAX_ARCHIVE+1),lambda p:p['artifacts']['desktop']['files']['workspace.js'].update(size=U.MAX_FILE+1)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                raw,_,_,_=self.candidate(mutate=mutate)
                with self.assertRaises(U.UpdateError):
                    U.verify_envelope(raw,self.trust,'desktop')

    def test_api2_defaults_reject_legacy_and_explicit_api_parameter_is_preserved(self):
        self.assertEqual(U.NATIVE_API,2)
        modern,_,_,_=self.candidate()
        with self.assertRaises(U.UpdateIncompatible):
            U.verify_envelope(modern,self.trust,'desktop',native_api=1)
        legacy,_,payload,_=self.candidate(native_api=1)
        # Generic explicit-version parameter coverage; not a legacy file-set migration.
        self.assertEqual(U.verify_envelope(legacy,self.trust,'desktop',native_api=1),payload)
        with self.assertRaises(U.UpdateIncompatible):
            U.verify_envelope(legacy,self.trust,'desktop')

    def test_all_zip_paths_duplicates_symlinks_and_tampering_rejected(self):
        def symlink(archive,platform):
            entry=archive.getinfo(next(iter(U.FILES[platform]))); entry.create_system=3; entry.external_attr=(stat.S_IFLNK|0o777)<<16
        mutations=[lambda z,p:z.writestr('../escape','x'),lambda z,p:z.writestr('/absolute','x'),lambda z,p:z.writestr('C:/absolute','x'),lambda z,p:z.writestr(next(iter(U.FILES[p])),'duplicate'),lambda z,p:z.writestr(next(iter(U.FILES[p])).upper(),'case collision'),symlink]
        with tempfile.TemporaryDirectory() as directory:
            store=U.UpdateStore(directory,'desktop',self.baseline,self.trust)
            for index,mutate in enumerate(mutations,1):
                raw,archives,payload,_=self.candidate(index,zip_mutate=mutate)
                with self.assertRaises(U.UpdateError):
                    store.install(raw,archives['desktop'])
                self.assertEqual(store.snapshot(),self.baseline)
                self.assertEqual(store.status()['active_revision'],'bundled')
            raw,archives,payload,_=self.candidate(20)
            with self.assertRaises(U.UpdateError):
                store.install(raw,archives['desktop']+b'tamper')

    def test_first_backup_commit_and_pinned_old_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store=U.UpdateStore(directory,'desktop',self.baseline,self.trust)
            raw,archives,payload,files=self.candidate()
            status=store.install(raw,archives['desktop'])
            self.assertEqual(status['previous_revision'],'bundled')
            self.assertEqual(store.snapshot('bundled'),self.baseline)
            self.assertTrue((Path(directory)/'snapshots'/'bundled'/'workspace.html').is_file())
            self.assertEqual(store.snapshot(),files['desktop'])
            boot=store.begin_boot()
            self.assertEqual(store.begin_boot()['ticket'],boot['ticket'])
            store.acknowledge(boot['ticket'])
            raw2,zips2,_,files2=self.candidate(2)
            store.install(raw2,zips2['desktop'])
            self.assertEqual(store.snapshot('test-1'),files['desktop'])
            self.assertEqual(store.snapshot(),files2['desktop'])
            with self.assertRaises(U.UpdateError):
                store.acknowledge(boot['ticket'])

    def test_highest_observed_idempotence_conflict_quarantine_and_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            store=U.UpdateStore(directory,'desktop',self.baseline,self.trust)
            raw,archives,payload,_=self.candidate(3)
            store.observe(raw)
            self.assertEqual(store.status()['highest_sequence'],3)
            old,zips,_,_=self.candidate(2)
            with self.assertRaises(U.UpdateError):
                store.install(old,zips['desktop'])
            store.install(raw,archives['desktop'])
            self.assertEqual(store.install(raw,archives['desktop'])['active_revision'],'test-3')
            conflict,zips,_,_=self.candidate(3,revision='different')
            with self.assertRaises(U.UpdateError):
                store.observe(conflict)
            boot=store.begin_boot()
            with patch.object(U.time,'time',return_value=boot['deadline']+1):
                with self.assertRaises(U.UpdateError):
                    store.acknowledge(boot['ticket'])
            self.assertEqual(store.status()['active_revision'],'bundled')
            self.assertEqual(store.status()['highest_sequence'],3)
            self.assertEqual(store.status()['failed_sequences'],[3])
            with self.assertRaises(U.UpdateError):
                store.install(raw,archives['desktop'])

    def test_recovery_preserves_other_live_frontend_but_dead_owner_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            store=U.UpdateStore(directory,'desktop',self.baseline,self.trust)
            raw,archives,_,_=self.candidate()
            store.install(raw,archives['desktop']); boot=store.begin_boot()
            other=U.UpdateStore(directory,'desktop',self.baseline,self.trust)
            self.assertEqual(other.recover_pending()['active_revision'],'test-1')
            with patch.object(U,'_process_identity',return_value=None):
                self.assertEqual(other.recover_pending()['active_revision'],'bundled')
            with self.assertRaises(U.UpdateError):
                other.acknowledge(boot['ticket'])

    def test_expired_known_good_still_local_rollbackable(self):
        with tempfile.TemporaryDirectory() as directory:
            store=U.UpdateStore(directory,'desktop',self.baseline,self.trust)
            raw,archives,payload,files=self.candidate()
            store.install(raw,archives['desktop']); store.acknowledge(store.begin_boot()['ticket'])
            newer,zips,payload2,_=self.candidate(2)
            store.install(newer,zips['desktop'])
            boot=store.begin_boot()
            with patch.object(U.time,'time',return_value=payload2['expires']+1):
                store.fail(boot['ticket'],'DO NOT STORE secret input')
                self.assertEqual(store.snapshot(),files['desktop'])
            self.assertNotIn('secret',json.dumps(store.status()))
            self.assertEqual(store.status()['highest_sequence'],2)

    def test_stored_snapshot_tampering_and_interrupted_pointer_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            store=U.UpdateStore(directory,'desktop',self.baseline,self.trust)
            raw,archives,_,_=self.candidate()
            original=store._save
            count=0
            def fail_pointer(state):
                nonlocal count
                count+=1
                if count==2:
                    raise OSError('simulated interrupted pointer write')
                original(state)
            with patch.object(store,'_save',side_effect=fail_pointer):
                with self.assertRaises(OSError):
                    store.install(raw,archives['desktop'])
            self.assertEqual(store.status()['active_revision'],'bundled')
            self.assertEqual(store.snapshot(),self.baseline)
            store.install(raw,archives['desktop'])
            (Path(directory)/'snapshots'/'test-1'/'workspace.js').write_bytes(b'tampered')
            with self.assertRaises(U.UpdateError):
                store.snapshot('test-1')
            self.assertEqual(store.snapshot(),self.baseline)

    def test_multiple_store_instances_serialize_identical_install(self):
        with tempfile.TemporaryDirectory() as directory:
            raw,archives,_,files=self.candidate()
            stores=[U.UpdateStore(directory,'desktop',self.baseline,self.trust) for _ in range(4)]
            with ThreadPoolExecutor(4) as pool:
                results=list(pool.map(lambda store:store.install(raw,archives['desktop']),stores))
            self.assertTrue(all(result['active_revision']=='test-1' for result in results))
            self.assertEqual(stores[0].snapshot(),files['desktop'])

    def test_download_https_same_origin_proxy_disabled_and_size_bound(self):
        with self.assertRaises(U.UpdateError):
            U.fetch_manifest('http://example.test/manifest.json')
        with self.assertRaises(U.UpdateError):
            U._download('https://evil.test/file',10,U._origin('https://good.test/manifest'))
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def geturl(self): return 'https://good.test/manifest'
            def read1(self,size): return b'x'*size
        class Opener:
            def open(self,request,timeout):
                self.request=request
                return Response()
        def opener(*handlers):
            self.assertEqual(next(handler for handler in handlers if isinstance(handler,U.ProxyHandler)).proxies,{})
            self.assertTrue(all('Authorization' not in getattr(handler,'headers',{}) for handler in handlers))
            return Opener()
        with patch.object(U,'build_opener',side_effect=opener):
            with self.assertRaises(U.UpdateError):
                U._download('https://good.test/manifest',10,U._origin('https://good.test/manifest'))
            with patch.object(U.time,'monotonic',side_effect=[0,21]):
                with self.assertRaisesRegex(U.UpdateError,'deadline'):
                    U._download('https://good.test/manifest',10,U._origin('https://good.test/manifest'))

    def test_corrupt_bundled_backup_and_broken_active_recover_native_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            store=U.UpdateStore(directory,'desktop',self.baseline,self.trust)
            raw,zips,_,_=self.candidate()
            store.install(raw,zips['desktop'])
            store.acknowledge(store.begin_boot()['ticket'])
            (Path(directory)/'snapshots'/'bundled'/'workspace.js').write_bytes(b'bad bundled script')
            self.assertEqual(store.snapshot('bundled'),self.baseline)
            (Path(directory)/'snapshots'/'test-1'/'workspace.js').write_bytes(b'bad active script')
            self.assertEqual(store.recover_pending()['active_revision'],'bundled')
            self.assertEqual(store.snapshot(),self.baseline)
            self.assertEqual(store.status()['highest_sequence'],1)

    def test_different_envelope_cannot_reuse_an_immutable_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store=U.UpdateStore(directory,'desktop',self.baseline,self.trust)
            raw,zips,payload,files=self.candidate()
            store.install(raw,zips['desktop']); store.acknowledge(store.begin_boot()['ticket'])
            changed=copy.deepcopy(payload); changed['sequence']=2
            another=sign(changed,self.private,self.trust)
            with self.assertRaises(U.UpdateError):
                store.install(another,zips['desktop'])
            self.assertEqual((Path(directory)/'envelopes'/'test-1.json').read_bytes(),raw)
            self.assertEqual(store.status()['active_sequence'],1)
            self.assertEqual(store.status()['highest_sequence'],2)
            self.assertEqual(store.snapshot(),files['desktop'])

    def test_current_snapshot_damage_rolls_back_complete_previous_not_partial_files(self):
        with tempfile.TemporaryDirectory() as directory:
            store=U.UpdateStore(directory,'desktop',self.baseline,self.trust)
            raw,zips,_,files=self.candidate()
            store.install(raw,zips['desktop']); store.acknowledge(store.begin_boot()['ticket'])
            second,second_zips,_,_=self.candidate(2)
            store.install(second,second_zips['desktop'])
            boot=store.begin_boot()
            (Path(directory)/'snapshots'/'test-2'/'workspace.html').unlink()
            self.assertEqual(store.snapshot(),files['desktop'])
            self.assertEqual(store.status()['active_revision'],'test-1')
            with self.assertRaises(U.UpdateError):
                store.acknowledge(boot['ticket'])

    @unittest.skipUnless(os.name == 'nt', 'Windows byte-range lock regression')
    def test_empty_lock_file_already_locked_by_another_process_waits_without_writing(self):
        import threading
        import time
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            code="import sys,msvcrt; f=open(sys.argv[1],'a+b'); msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1); print('locked',flush=True); sys.stdin.buffer.read(1); f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1); f.close()"
            holder=subprocess.Popen([sys.executable,'-c',code,str(root/'store.lock')],
                                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            release=None
            try:
                self.assertEqual(holder.stdout.readline().strip(),b'locked')
                self.assertEqual((root/'store.lock').stat().st_size,0)
                def release_lock():
                    time.sleep(.2)
                    holder.stdin.write(b'\n');holder.stdin.flush()
                release=threading.Thread(target=release_lock)
                release.start()
                store=U.UpdateStore(root,'desktop',self.baseline,self.trust)
                self.assertEqual(store.status()['active_revision'],'bundled')
                self.assertEqual((root/'store.lock').stat().st_size,0)
            finally:
                if release is not None:release.join(timeout=3)
                if holder.poll() is None and release is None:holder.terminate()
                holder.communicate(timeout=5)

    def test_two_real_processes_share_atomic_store_and_dead_owner_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory)
            raw,archives,_,files=self.candidate()
            job=directory/'job.json'
            job.write_text(json.dumps({'root':str(directory/'store'),'trust':self.trust,'files':{k:base64.b64encode(v).decode() for k,v in self.baseline.items()},'envelope':base64.b64encode(raw).decode(),'archive':base64.b64encode(archives['desktop']).decode()}))
            code="import base64,json,sys;from supermonoagent.ui_updates import UpdateStore; j=json.load(open(sys.argv[1]));s=UpdateStore(j['root'],'desktop',{k:base64.b64decode(v) for k,v in j['files'].items()},j['trust']);print(s.install(base64.b64decode(j['envelope']),base64.b64decode(j['archive']))['active_revision'])"
            env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
            processes=[subprocess.Popen([sys.executable,'-c',code,str(job)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env) for _ in range(2)]
            for process in processes:
                out,err=process.communicate(timeout=10)
                self.assertEqual(process.returncode,0,err.decode())
                self.assertEqual(out.strip(),b'test-1')
            store=U.UpdateStore(directory/'store','desktop',self.baseline,self.trust)
            self.assertEqual(store.snapshot(),files['desktop'])
            self.assertEqual(store.recover_pending()['active_revision'],'bundled')
            self.assertEqual(store.status()['highest_sequence'],1)
