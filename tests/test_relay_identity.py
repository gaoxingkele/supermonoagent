"""Persistent relay identities never store usable login/session secrets."""
import concurrent.futures
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from supermonoagent.relay_identity import RelayIdentity, normalize_public_url


class RelayIdentityTest(unittest.TestCase):
    def test_stable_account_grants_restart_hashed_session_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'identity.sqlite3'
            identity=RelayIdentity(path)
            account=identity.create_account('Person@Example.test','private-password')
            identity.grant(account,'room-a')
            identity.grant(account,'room-b')
            login=identity.login('person@example.test','private-password')
            self.assertEqual(login['account'],{'id':account,'email':'person@example.test'})
            restarted=RelayIdentity(path)
            self.assertEqual(restarted.authenticate(login['session'])['id'],account)
            self.assertEqual(restarted.rooms(account),['room-a','room-b'])
            raw=path.read_bytes()
            self.assertNotIn(b'private-password',raw)
            self.assertNotIn(login['session'].encode(),raw)
            self.assertIn(hashlib.sha256(login['session'].encode()).hexdigest().encode(),raw)
            restarted.reset_password(account,'replacement-password')
            self.assertIsNone(identity.authenticate(login['session']))
            with self.assertRaisesRegex(ValueError,'Invalid credentials'):
                identity.login('person@example.test','private-password')
            self.assertEqual(identity.login('person@example.test','replacement-password')['account']['id'],account)
            self.assertEqual(identity.rooms(account),['room-a','room-b'])

    def test_logout_expiry_wrong_password_and_grant_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            identity=RelayIdentity(Path(directory)/'identity.sqlite3',session_seconds=1)
            account=identity.create_account('person@example.test','private-password')
            for email,password in [('person@example.test','wrong-password'),('missing@example.test','private-password')]:
                with self.assertRaisesRegex(ValueError,'Invalid credentials'):
                    identity.login(email,password)
            first=identity.login('person@example.test','private-password')
            second=identity.login('person@example.test','private-password')
            identity.logout(first['session'])
            self.assertIsNone(identity.authenticate(first['session']))
            self.assertIsNotNone(identity.authenticate(second['session']))
            with patch('supermonoagent.relay_identity.time.time',return_value=second['expires']+1):
                self.assertIsNone(identity.authenticate(second['session']))
            for i in range(64):
                identity.grant(account,'room-'+str(i))
            with self.assertRaises(ValueError):
                identity.grant(account,'overflow')
            identity.revoke(account,'room-0')
            identity.grant(account,'replacement')
            self.assertEqual(len(identity.rooms(account)),64)

    def test_password_reset_during_login_cannot_issue_a_stale_session(self):
        import supermonoagent.relay_identity as module
        with tempfile.TemporaryDirectory() as directory:
            identity=RelayIdentity(Path(directory)/'identity.sqlite3')
            account=identity.create_account('person@example.test','private-password')
            started,finish=threading.Event(),threading.Event()
            original=module._password
            def password(value,salt):
                result=original(value,salt)
                if value=='private-password':
                    started.set()
                    finish.wait(3)
                return result
            with patch.object(module,'_password',password),concurrent.futures.ThreadPoolExecutor() as pool:
                login=pool.submit(identity.login,'person@example.test','private-password')
                self.assertTrue(started.wait(3))
                identity.reset_password(account,'replacement-password')
                finish.set()
                with self.assertRaisesRegex(ValueError,'Invalid credentials'):
                    login.result(3)

    def test_admin_cli_reads_password_only_from_stdin_and_preserves_id(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'identity.sqlite3'
            env={**os.environ,'PYTHONPATH':str(root/'src')}
            def run(*args,password=None):
                return subprocess.run([sys.executable,'-m','supermonoagent.relay','--identity-file',str(path),*args],input=password,text=True,capture_output=True,env=env,timeout=10)
            created=run('--create-account','person@example.test','--password-stdin',password='stdin-secret-password\n')
            self.assertEqual(created.returncode,0,created.stderr)
            self.assertNotIn('stdin-secret-password',created.stdout+created.stderr)
            account=created.stdout.strip()
            reset=run('--reset-account-password',account,'--password-stdin',password='replacement-password\n')
            self.assertEqual(reset.returncode,0,reset.stderr)
            self.assertEqual(RelayIdentity(path).login('person@example.test','replacement-password')['account']['id'],account)

    def test_public_endpoint_normalization_refuses_credential_and_plaintext_destinations(self):
        self.assertEqual(normalize_public_url('https://RELAY.example:443/'),'wss://relay.example/ws')
        self.assertEqual(normalize_public_url('https://relay.example:8818/ws'),'wss://relay.example:8818/ws')
        self.assertEqual(normalize_public_url('http://127.0.0.1:1234'),'ws://127.0.0.1:1234/ws')
        for endpoint in ['ws://relay.example/ws','https://user:password@relay.example','https://relay.example/ws?token=x','https://relay.example/#fragment','https://relay.example/arbitrary']:
            with self.assertRaises(ValueError):
                normalize_public_url(endpoint)
