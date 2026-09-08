import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, String, Integer, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://vend:vend@postgres:5432/vend")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class USSDSession(Base):
    __tablename__ = "sessions"

    session_id = Column(String, primary_key=True, index=True)
    msisdn = Column(String, nullable=False)
    last_text = Column(String, nullable=False, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class VendLedger(Base):
    __tablename__ = "vends"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    session_id = Column(String, unique=True, nullable=False, index=True)
    client_ref = Column(String, unique=True, nullable=False, index=True)
    msisdn = Column(String, nullable=False)
    network = Column(String, nullable=False)
    amount_minor = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="PENDING")
    operator_ref = Column(String, nullable=True)
    reason_code = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
