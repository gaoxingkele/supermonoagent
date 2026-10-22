"""Relay-scoped accounts, explicit room grants and revocable hashed login sessions."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit

from .accounts import _EMAIL
from .security import loopback

SESSION_SECONDS = 3600


def normalize_public_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {'http', 'https', 'ws', 'wss'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/', '/ws'}:
        raise ValueError('Expected an HTTPS/WSS relay endpoint without credentials or query')
    scheme = {'http': 'ws', 'https': 'wss'}.get(parsed.scheme, parsed.scheme)
    if scheme == 'ws' and not loopback(parsed.hostname):
        raise ValueError('Public relay account endpoints require TLS')
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('Invalid relay port')
    host = parsed.hostname.lower()
    authority = f'[{host}]' if ':' in host else host
    if port and port != (443 if scheme == 'wss' else 80):
        authority += f':{port}'
    return urlunsplit((scheme, authority, '/ws', '', ''))


def _password(password: str, salt: str) -> str:
    if not isinstance(password, str) or not 8 <= len(password) <= 1024:
        raise ValueError('Password must contain 8 to 1024 characters')
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 300_000).hex()


class RelayIdentity:
    """Each operation has its own transaction so CLI edits cannot overwrite live sessions."""
    def __init__(self, path: Path | str, *, session_seconds: int = SESSION_SECONDS):
        self.path = Path(path)
        self.session_seconds = min(86400, max(1, session_seconds))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, salt TEXT NOT NULL, verifier TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS grants (account TEXT NOT NULL REFERENCES accounts(id), code TEXT NOT NULL, PRIMARY KEY(account,code));
                CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES accounts(id), expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value INTEGER NOT NULL);
                INSERT OR IGNORE INTO metadata VALUES ('revision',0);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=.2)
        db.execute('PRAGMA foreign_keys=ON')
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _changed(db):
        db.execute("UPDATE metadata SET value=value+1 WHERE key='revision'")

    @property
    def revision(self):
        with self.connection() as db:
            return db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0]

    @staticmethod
    def _account(db, value):
        row = db.execute('SELECT * FROM accounts WHERE id=? OR email=?', (value, str(value).strip().lower())).fetchone()
        if row is None:
            raise ValueError('Unknown account')
        return row

    def create_account(self, email, password):
        email = str(email).strip().lower()
        if len(email) > 320 or not _EMAIL.fullmatch(email):
            raise ValueError('Valid account email required')
        account, salt = secrets.token_hex(16), secrets.token_hex(16)
        verifier = _password(password, salt)
        with self.connection() as db:
            try:
                db.execute('INSERT INTO accounts VALUES (?,?,?,?)', (account,email,salt,verifier))
            except sqlite3.IntegrityError as exc:
                raise ValueError('Account already exists') from exc
            self._changed(db)
        return account

    def reset_password(self, account, password):
        salt = secrets.token_hex(16)
        verifier = _password(password,salt)
        with self.connection() as db:
            account = self._account(db,account)['id']
            db.execute('UPDATE accounts SET salt=?,verifier=? WHERE id=?',(salt,verifier,account))
            db.execute('DELETE FROM sessions WHERE account=?',(account,))
            self._changed(db)

    def grant(self, account, code):
        if not isinstance(code,str) or not 1 <= len(code) <= 128:
            raise ValueError('Room code must contain 1 to 128 characters')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            account = self._account(db,account)['id']
            if db.execute('SELECT COUNT(*) FROM grants WHERE account=?',(account,)).fetchone()[0] >= 64 and not db.execute('SELECT 1 FROM grants WHERE account=? AND code=?',(account,code)).fetchone():
                raise ValueError('At most 64 rooms per account')
            db.execute('INSERT OR IGNORE INTO grants VALUES (?,?)',(account,code))
            self._changed(db)

    def revoke(self, account, code):
        with self.connection() as db:
            account = self._account(db,account)['id']
            db.execute('DELETE FROM grants WHERE account=? AND code=?',(account,code))
            self._changed(db)

    def rooms(self, account):
        with self.connection() as db:
            return [row[0] for row in db.execute('SELECT code FROM grants WHERE account=? ORDER BY code',(account,))]

    def login(self, email, password):
        if not isinstance(email,str) or len(email)>320 or not isinstance(password,str) or not 8 <= len(password) <= 1024:
            raise ValueError('Invalid credentials')
        with self.connection() as db:
            row = db.execute('SELECT * FROM accounts WHERE email=?',(email.strip().lower(),)).fetchone()
            # Perform the same password work for unknown users; never disclose account existence.
            salt = row['salt'] if row else '00'*16
            verifier = _password(password,salt)
            if row is None or not hmac.compare_digest(verifier,row['verifier']):
                raise ValueError('Invalid credentials')
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT salt,verifier FROM accounts WHERE id=?',(row['id'],)).fetchone()
            if current is None or current['salt'] != row['salt'] or current['verifier'] != row['verifier']:
                raise ValueError('Invalid credentials')
            session = secrets.token_urlsafe(32)
            expires = time.time()+self.session_seconds
            db.execute('DELETE FROM sessions WHERE expires<=?',(time.time(),))
            # Bound retained active logins per account.
            active = db.execute('SELECT digest FROM sessions WHERE account=? ORDER BY expires DESC',(row['id'],)).fetchall()
            for item in active[31:]:
                db.execute('DELETE FROM sessions WHERE digest=?',(item[0],))
            db.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(session.encode()).hexdigest(),row['id'],expires))
            self._changed(db)
            return {'session':session,'expires':expires,'account':{'id':row['id'],'email':row['email']}}

    def authenticate(self, session):
        if not isinstance(session,str) or not 1 <= len(session) <= 4096:
            return None
        with self.connection() as db:
            row = db.execute('SELECT accounts.id,accounts.email,sessions.expires FROM sessions JOIN accounts ON accounts.id=sessions.account WHERE digest=? AND expires>?',(hashlib.sha256(session.encode()).hexdigest(),time.time())).fetchone()
            return dict(row) if row else None

    def logout(self, session):
        if not isinstance(session,str) or len(session)>4096:
            return
        with self.connection() as db:
            db.execute('DELETE FROM sessions WHERE digest=?',(hashlib.sha256(session.encode()).hexdigest(),))
            self._changed(db)
