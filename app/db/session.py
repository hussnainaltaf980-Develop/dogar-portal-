"""Database session management with SQLite performance tuning."""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
from app.core.config import settings

is_sqlite = settings.DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
    # Larger pool helps under concurrent requests (and is harmless for SQLite)
    pool_size=20 if not is_sqlite else 5,
    max_overflow=10,
)

# ---- SQLite performance pragmas (huge win for read-heavy workloads) ----
if is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _conn_record):
        cur = dbapi_conn.cursor()
        # Write-Ahead Logging → concurrent readers + writer don't block
        cur.execute("PRAGMA journal_mode=WAL;")
        # OS-only sync (still ACID under WAL) — much faster commits
        cur.execute("PRAGMA synchronous=NORMAL;")
        # 64 MB page cache (negative = KB), big improvement on the 2.78MB DB
        cur.execute("PRAGMA cache_size=-65536;")
        # Memory-mapped I/O up to 256 MB
        cur.execute("PRAGMA mmap_size=268435456;")
        # Keep temp tables in RAM
        cur.execute("PRAGMA temp_store=MEMORY;")
        # 30s busy-wait before "database is locked"
        cur.execute("PRAGMA busy_timeout=30000;")
        # Foreign keys ON (data integrity)
        cur.execute("PRAGMA foreign_keys=ON;")
        cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
