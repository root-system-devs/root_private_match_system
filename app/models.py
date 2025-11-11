from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Integer, BigInteger, ForeignKey, Date, DateTime, Boolean, Text, Float
from sqlalchemy.sql import func
from datetime import datetime


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64))
    xp: Mapped[float] = mapped_column(Float, default=2000.0)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Season(Base):
    __tablename__ = "seasons"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(16), unique=True)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    
class SeasonParticipant(Base):
    __tablename__ = "season_participants"
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), primary_key=True, index=True)
    user_id:   Mapped[int] = mapped_column(ForeignKey("users.id"),   primary_key=True, index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class EntryBox(Base):
    __tablename__ = "entry_boxes"
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    week_number: Mapped[int]
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|closed|canceled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EntryApplication(Base):
    """
    募集箱(EntryBox)に対する1人分の参加申請。
    """
    __tablename__ = "entry_applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    entry_box_id: Mapped[int] = mapped_column(ForeignKey("entry_boxes.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="confirmed")  # confirmed|canceled|waitlist
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    week_number: Mapped[int]
    room_label: Mapped[str] = mapped_column(String(8)) # 'A','B','C'...
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="scheduled") # scheduled|live|finished


class Entry(Base):
    __tablename__ = "entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), default="confirmed") # confirmed|canceled|waitlist
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SessionStat(Base):
    __tablename__ = "session_stats"
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    
class SessionSettlement(Base):
    __tablename__ = "session_settlements"
    season_id:  Mapped[int]   = mapped_column(ForeignKey("seasons.id"),  primary_key=True)
    session_id: Mapped[int]   = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    user_id:    Mapped[int]   = mapped_column(ForeignKey("users.id"),    primary_key=True)
    win_delta:  Mapped[int]   = mapped_column(Integer, default=0)   # そのセッションで加算した勝数（=通常 st.wins）
    rate_delta: Mapped[float] = mapped_column(Float,   default=0.0) # そのセッションで変更したレート量（Δ）
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SeasonScore(Base):
    __tablename__ = "season_scores"
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    entry_points: Mapped[float] = mapped_column(default=0.0)
    win_points: Mapped[int] = mapped_column(default=0)
    rate: Mapped[float] = mapped_column(Float, default=2000.0)


class Match(Base):
    __tablename__ = "matches"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    match_index: Mapped[int]
    team_a_ids: Mapped[str] = mapped_column(Text) # CSV: "1,3,5,8"
    team_b_ids: Mapped[str] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(String(64), default="")
    winner: Mapped[str] = mapped_column(String(1), nullable=True) # 'A'|'B'|NULL