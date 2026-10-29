from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from api.config import settings

engine = create_engine(settings.db_url, connect_args={"check_same_thread": False}
                       if settings.db_url.startswith("sqlite") else {})

if settings.db_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")   # readers don't block the alarm writer
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def migrate():
    """create_all() never changes existing tables: add columns introduced after the first release.
    (A real deployment would use Alembic; this keeps the demo one-command.)"""
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} "
                                      f"{col.type.compile(engine.dialect)}"))


def get_db():
    with SessionLocal() as db:
        yield db
