"""Short SQLite transactions: durable inbox, delivery queue and host dedup ledger."""
import json
import sqlite3
import time
from pathlib import Path

from .protocol import canonical


class LocalStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS counter(id INTEGER PRIMARY KEY, value INTEGER NOT NULL);
        INSERT OR IGNORE INTO counter VALUES(1,0);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, payload TEXT NOT NULL, request TEXT, incoming INTEGER NOT NULL, acked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, destination TEXT NOT NULL, kind TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending', error TEXT, UNIQUE(event_id,destination,kind));
        CREATE TABLE IF NOT EXISTS peers(id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, state TEXT NOT NULL);
        ''')
        self.db.commit()

    def next_seq(self):
        with self.db:
            self.db.execute("UPDATE counter SET value=value+1 WHERE id=1")
            return self.db.execute("SELECT value FROM counter WHERE id=1").fetchone()[0]

    def event(self, event_id):
        r = self.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return dict(r) if r else None

    def save(self, signed: dict, incoming: bool, destinations: list[tuple[str, str]], request: dict | None = None):
        event_id = signed["event"]["event_id"]
        raw = canonical(signed).decode()
        with self.db:
            old = self.event(event_id)
            if old and old["payload"] != raw:
                raise ValueError("event_id content conflict")
            if not old:
                self.db.execute("INSERT INTO events(id,payload,request,incoming) VALUES(?,?,?,?)",
                                (event_id, raw, canonical(request).decode() if request else None, int(incoming)))
            if incoming:
                seen = self.db.execute("SELECT state FROM seen WHERE id=?", (event_id,)).fetchone()
                if seen and seen[0] == "processed":
                    self.db.execute("UPDATE events SET acked=1 WHERE id=?", (event_id,))
            for destination, kind in destinations:
                self.db.execute("INSERT OR IGNORE INTO queue(event_id,destination,kind) VALUES(?,?,?)", (event_id, destination, kind))
        return bool(old)

    def peers(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM peers ORDER BY id")]

    def cache(self, peers):
        with self.db:
            for peer in peers:
                old = self.db.execute("SELECT data FROM peers WHERE id=?", (peer["agent_id"],)).fetchone()
                if old and json.loads(old[0])["public_key"] != peer["public_key"]:
                    raise ValueError("peer public key changed; explicit trust reset required")
                self.db.execute("INSERT OR REPLACE INTO peers VALUES(?,?)", (peer["agent_id"], json.dumps(peer)))

    def pending(self, kind: str, limit=20):
        return [dict(r) for r in self.db.execute(
            "SELECT q.*,e.payload FROM queue q JOIN events e ON e.id=q.event_id WHERE q.kind=? AND q.state='pending' AND q.due<=? ORDER BY q.id LIMIT ?",
            (kind, time.time(), limit))]

    def delivered(self, queue_id):
        with self.db:
            self.db.execute("UPDATE queue SET state='delivered',error=NULL WHERE id=?", (queue_id,))

    def failed(self, row, error, max_attempts, retry_seconds):
        attempts = row["attempts"] + 1
        state = "failed" if attempts >= max_attempts else "pending"
        with self.db:
            self.db.execute("UPDATE queue SET attempts=?,state=?,error=?,due=? WHERE id=?",
                (attempts, state, error[:500], time.time() + min(retry_seconds * 2 ** min(attempts, 10), 300), row["id"]))

    def delivery_status(self, event_id=None):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM queue WHERE (? IS NULL OR event_id=?) ORDER BY id DESC LIMIT 500", (event_id, event_id))]

    def retry(self, event_id):
        with self.db:
            cursor = self.db.execute("UPDATE queue SET state='pending',attempts=0,due=0,error=NULL WHERE event_id=? AND state!='delivered'", (event_id,))
        return {"requeued": cursor.rowcount}

    def unread(self, limit=100):
        rows = self.db.execute("SELECT e.payload,s.state FROM events e LEFT JOIN seen s ON s.id=e.id WHERE e.incoming=1 AND e.acked=0 ORDER BY e.rowid LIMIT ?", (min(max(limit, 1), 500),))
        return [{**json.loads(r[0]), "host_processing_state": r[1]} for r in rows]

    def claim(self, event_id):
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO seen VALUES(?,'claimed')", (event_id,))
        return {"event_id": event_id, "acquired": bool(cur.rowcount),
                "state": self.db.execute("SELECT state FROM seen WHERE id=?", (event_id,)).fetchone()[0]}

    def release(self, event_id):
        with self.db:
            self.db.execute("DELETE FROM seen WHERE id=? AND state='claimed'", (event_id,))
        return {"event_id": event_id, "released": True}

    def ack(self, event_ids):
        with self.db:
            for event_id in event_ids:
                self.db.execute("UPDATE events SET acked=1 WHERE id=? AND incoming=1", (event_id,))
                self.db.execute("INSERT INTO seen VALUES(?,'processed') ON CONFLICT(id) DO UPDATE SET state='processed'", (event_id,))
        return {"acknowledged": event_ids}
