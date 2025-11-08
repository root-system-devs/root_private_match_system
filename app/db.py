import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from .models import Base


load_dotenv()

# デフォルトを用意（.env に無ければ SQLite で動作）
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./league.db")

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_models():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)