"""A new native UI bundle starts its own cache without mutating live older stores."""
import asyncio
import base64
import subprocess
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from supermonoagent.frontend import Frontend,UI_FILES,bundled_ui_fingerprint
from supermonoagent.ui_updates import UpdateError,UpdateStore
from test_ui_updates import OPENSSL,release


def assets(marker):
    return {name:('<!doctype html><script src="workspace.js"></script><link href="workspace.css">'+marker).encode() if name=='workspace.html' else (name+' '+marker).encode() for name in UI_FILES}


class NativeNamespaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.home=Path(self.temporary.name)
        self.base=self.home/'.supermonoagent'/'ui-updates'/'desktop'
        self.files=assets('new-native-highlight')
        self.trust={'schema':1,'keys':{}}

    def tearDown(self):
        self.temporary.cleanup()

    def frontend(self,files=None,**kwargs):
        with patch('supermonoagent.frontend.Path.home',return_value=self.home),patch('supermonoagent.frontend.bundled_ui_files',return_value=files or self.files):
            return Frontend('http://127.0.0.1:6769',update_trust=self.trust,**kwargs)

    def floor(self,root,high=4,digest='a'*64,failed=None):
        store=UpdateStore(root,'desktop',assets('old-native'),self.trust)
        with store._locked():
            state=store._state();state.update(highest_sequence=high,observed_hash=digest,failed_sequences=failed or [])
            store._save(state)
        return store

    def test_new_bundle_shows_native_without_migrating_old_active_or_touching_old_files(self):
        old=self.floor(self.base,high=4,failed=[2])
        with old._locked():
            state=old._state();state.update(active_revision='r4',active_sequence=4,previous_revision='bundled',pending={'old':'ticket'})
            old._save(state)
        before={path:path.read_bytes() for path in self.base.rglob('*') if path.is_file()}
        current=self.frontend()
        self.assertEqual(current.update_root,self.base/'native'/bundled_ui_fingerprint(self.files))
        state=current.update_store.status()
        self.assertEqual(state['active_revision'],'bundled')
        self.assertEqual(state['active_sequence'],0)
        self.assertIsNone(state['pending'])
        self.assertEqual((state['highest_sequence'],state['observed_hash'],state['failed_sequences']),(4,'a'*64,[2]))
        self.assertEqual(state['native_bundle_floor'],4)
        self.assertEqual(current.update_store.snapshot(),self.files)
        for path,content in before.items():
            self.assertEqual(path.read_bytes(),content)

    @unittest.skipUnless(OPENSSL,'Publisher OpenSSL unavailable')
    def test_same_bundle_restart_never_resets_active_and_startup_only_raises_floor(self):
        private=self.home/'test-private.pem'
        subprocess.run([OPENSSL,'genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',str(private)],capture_output=True,check=True)
        der=subprocess.run([OPENSSL,'pkey','-in',str(private),'-pubout','-outform','DER'],capture_output=True,check=True).stdout
        self.trust={'schema':1,'keys':{hashlib.sha256(der).hexdigest()[:16]:base64.b64encode(der).decode()}}
        self.floor(self.base,high=4)
        first=self.frontend()
        raw,zips,_,files=release(private,self.trust,sequence=5,revision='existing-hot')
        first.update_store.install(raw,zips['desktop'])
        first.update_store.acknowledge(first.update_store.begin_boot()['ticket'])
        self.floor(self.base/'native'/('f'*64),high=8,digest='c'*64,failed=[7])
        restarted=self.frontend()
        state=restarted.update_store.status()
        self.assertEqual(restarted.update_root,first.update_root)
        self.assertEqual(state['active_revision'],'existing-hot')
        self.assertEqual(state['active_sequence'],5)
        self.assertEqual(restarted.update_store.snapshot(),files['desktop'])
        self.assertEqual((state['highest_sequence'],state['observed_hash']),(8,'c'*64))
        self.assertEqual(state['native_bundle_floor'],4)
        self.assertEqual(state['failed_sequences'],[7])

    def test_explicit_root_and_theme_fixture_paths_keep_existing_semantics(self):
        explicit=self.home/'explicit'
        selected=self.frontend(update_root=explicit)
        self.assertEqual(selected.update_root,explicit)
        self.assertNotIn('native_bundle_floor',selected.update_store.status())
        theme=self.home/'fixture'/'themes.json'
        fixture=self.frontend(theme_file=theme)
        self.assertEqual(fixture.update_root,theme.parent/'ui-updates')
        self.assertNotIn('native_bundle_floor',fixture.update_store.status())

    def test_bundle_fingerprint_contains_names_and_content_and_busts_old_urls(self):
        first=self.frontend()
        next_files=assets('second-native')
        second=self.frontend(next_files)
        self.assertNotEqual(first.bundle_fingerprint,second.bundle_fingerprint)
        first_html=first._workspace_document()
        second_html=second._workspace_document()
        self.assertIn('/__ui/bundled/workspace.js?bundle='+first.bundle_fingerprint,first_html)
        self.assertIn('/__ui/bundled/workspace.js?bundle='+second.bundle_fingerprint,second_html)
        self.assertNotIn('?bundle='+first.bundle_fingerprint,second_html)
        renamed=dict(self.files); renamed['renamed.js']=renamed.pop('workspace.js')
        self.assertNotEqual(bundled_ui_fingerprint(renamed),bundled_ui_fingerprint(self.files))

    def test_floor_conflicts_and_invalid_fields_never_change_store(self):
        current=UpdateStore(self.home/'store','desktop',self.files,self.trust)
        current.inherit_floor([{'highest_sequence':4,'observed_hash':'a'*64,'failed_sequences':[2]}])
        before=current.status()
        for source in [{'highest_sequence':4,'observed_hash':'b'*64,'failed_sequences':[]},{'highest_sequence':True,'observed_hash':None,'failed_sequences':[]},{'highest_sequence':5,'observed_hash':'bad','failed_sequences':[]},{'highest_sequence':5,'observed_hash':'c'*64,'failed_sequences':[6]}]:
            with self.assertRaises(UpdateError):
                current.inherit_floor([source])
            self.assertEqual(current.status(),before)

    def test_native_floor_suppresses_old_feed_but_not_later_new_releases(self):
        self.floor(self.base,high=4)
        frontend=self.frontend()
        async def check(sequence):
            payload={'revision':'remote-'+str(sequence),'sequence':sequence}
            with patch('supermonoagent.ui_updates.fetch_manifest',return_value=b'valid'),patch('supermonoagent.ui_updates.verify_envelope',return_value=payload),patch.object(frontend.update_store,'observe',return_value=payload):
                return await frontend._check_update()
        self.assertEqual(asyncio.run(check(4))['phase'],'current')
        self.assertIsNone(frontend.update_candidate)
        self.assertEqual(asyncio.run(check(3))['phase'],'current')
        self.assertEqual(asyncio.run(check(5))['phase'],'available')
        self.assertEqual(frontend.update_candidate['sequence'],5)
