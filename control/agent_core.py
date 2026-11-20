"""ERNASA agent task and approval core.

Offline-only coordination library. It does not call an AI model, send mail,
deploy code, access accounts or execute approved actions. Founder approval is a
recorded decision, never a substitute for separate authenticated execution.
"""
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

AGENTS = {
    "LARA": "Koordinasyon ve görev planlama",
    "MERT": "Yazılım geliştirme ve teknik inceleme",
    "SELIN": "İK iletişimi ve mülakat taslakları",
}
KINDS = {"analysis", "development", "communication", "interview"}
STATES = {"queued", "draft", "awaiting_approval", "approved", "rejected"}


class AgentError(ValueError):
    pass


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _text(value, field, limit=8000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AgentError(f"{field}: geçersiz içerik")
    return value.strip()


class AgentStore:
    """Private SQLite prototype; NEVER expose without server-side founder auth."""

    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute() or not self.path.parent.is_dir():
            raise AgentError("Mevcut dizinde mutlak veritabanı yolu gerekli")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self, write=False):
        with closing(self.connect()) as db:
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db

    def initialize(self):
        with self.transaction() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS agent_tasks (
                id TEXT PRIMARY KEY,
                agent TEXT NOT NULL CHECK(agent IN ('LARA','MERT','SELIN')),
                kind TEXT NOT NULL CHECK(kind IN ('analysis','development','communication','interview')),
                request TEXT NOT NULL,
                draft TEXT,
                status TEXT NOT NULL CHECK(status IN ('queued','draft','awaiting_approval','approved','rejected')),
                founder_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_events (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES agent_tasks(id),
                actor TEXT NOT NULL,
                event TEXT NOT NULL,
                note TEXT NOT NULL,
                at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS agent_events_task_idx ON agent_events(task_id, at);
            CREATE TRIGGER IF NOT EXISTS agent_events_immutable_update
            BEFORE UPDATE ON agent_events BEGIN SELECT RAISE(ABORT,'immutable agent event'); END;
            CREATE TRIGGER IF NOT EXISTS agent_events_immutable_delete
            BEFORE DELETE ON agent_events BEGIN SELECT RAISE(ABORT,'immutable agent event'); END;
            """)

    @staticmethod
    def _event(db, task, actor, event, note):
        db.execute("INSERT INTO agent_events VALUES(?,?,?,?,?,?)",
                   (uuid.uuid4().hex, task, actor, event, note, _stamp()))

    def create(self, agent, kind, request):
        if agent not in AGENTS or kind not in KINDS:
            raise AgentError("Ajan veya görev türü tanınmıyor")
        request = _text(request, "Görev")
        task = uuid.uuid4().hex
        instant = _stamp()
        with self.transaction(write=True) as db:
            db.execute("INSERT INTO agent_tasks VALUES(?,?,?,?,?,?,?,?,?)",
                       (task, agent, kind, request, None, "queued", None, instant, instant))
            self._event(db, task, "founder", "created", "Kurucu yeni görev oluşturdu")
        return task

    def _load(self, db, task):
        if not isinstance(task, str) or len(task) != 32:
            raise AgentError("Geçersiz görev")
        row = db.execute("SELECT * FROM agent_tasks WHERE id=?", (task,)).fetchone()
        if row is None:
            raise AgentError("Görev bulunamadı")
        return dict(row)

    def prepare(self, task, agent, draft):
        draft = _text(draft, "Taslak")
        with self.transaction(write=True) as db:
            row = self._load(db, task)
            if row["agent"] != agent or row["status"] not in ("queued", "draft"):
                raise AgentError("Görev bu ajan tarafından düzenlenemez")
            db.execute("UPDATE agent_tasks SET draft=?,status='draft',updated_at=? WHERE id=?",
                       (draft, _stamp(), task))
            self._event(db, task, agent, "draft_prepared", "Taslak hazırlandı")
        return self.get(task)

    def submit(self, task, agent):
        with self.transaction(write=True) as db:
            row = self._load(db, task)
            if row["agent"] != agent or row["status"] != "draft" or not row["draft"]:
                raise AgentError("Onaya sunulacak taslak bulunamadı")
            db.execute("UPDATE agent_tasks SET status='awaiting_approval',updated_at=? WHERE id=?",
                       (_stamp(), task))
            self._event(db, task, agent, "submitted", "Kurucu onayına sunuldu")
        return self.get(task)

    def decide(self, task, *, is_founder, approved, reason):
        # Authentication is not implemented in this offline library.
        # The integrating HTTP application MUST derive is_founder from its
        # authenticated CONTROL session, never from request-controlled JSON.
        if is_founder is not True or type(approved) is not bool:
            raise AgentError("Kurucu oturumu ve açık karar gerekli")
        reason = _text(reason, "Gerekçe", 1000)
        with self.transaction(write=True) as db:
            row = self._load(db, task)
            if row["status"] != "awaiting_approval":
                raise AgentError("Bu görev karar beklemiyor")
            status = "approved" if approved else "rejected"
            db.execute("UPDATE agent_tasks SET status=?,founder_reason=?,updated_at=? WHERE id=?",
                       (status, reason, _stamp(), task))
            self._event(db, task, "founder", status, reason)
        return self.get(task)

    def get(self, task):
        with self.transaction() as db:
            return self._load(db, task)

    def queue(self, status=None):
        if status is not None and status not in STATES:
            raise AgentError("Geçersiz durum")
        with self.transaction() as db:
            sql = "SELECT * FROM agent_tasks"
            args = ()
            if status is not None:
                sql += " WHERE status=?"
                args = (status,)
            sql += " ORDER BY created_at DESC,id DESC"
            return [dict(row) for row in db.execute(sql, args)]

    def history(self, task):
        with self.transaction() as db:
            self._load(db, task)
            return [dict(row) for row in db.execute(
                "SELECT actor,event,note,at FROM agent_events WHERE task_id=? ORDER BY at,id", (task,))]
