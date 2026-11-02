import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./resolveflow.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)


@contextmanager
def transaction():
    with SessionLocal() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
