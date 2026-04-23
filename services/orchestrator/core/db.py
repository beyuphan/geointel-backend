import uuid
from sqlmodel import SQLModel, Field, Session, create_engine
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from pgvector.sqlalchemy import Vector
from sqlalchemy import Column
import os
from datetime import datetime, timezone
from typing import Optional

# We extract the db connection logic to here
DB_DSN = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:password@geo_db:5432/geodb")

# V3 FIX: docker-compose sets postgresql:// but async engine needs postgresql+asyncpg://
if DB_DSN.startswith("postgresql://"):
    DB_DSN = DB_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

# Create Async Engine
engine = create_async_engine(DB_DSN, echo=False)

async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# -----------------
# DATABASE MODELS
# -----------------

class User(SQLModel, table=True):
    __tablename__ = "users"
    id: uuid.UUID | None = Field(default_factory=uuid.uuid4, primary_key=True)
    username: str = Field(unique=True, index=True)
    email: Optional[str] = Field(default=None, index=True)
    password_hash: Optional[str] = None
    created_at: Optional[datetime] = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

class UserVehicle(SQLModel, table=True):
    __tablename__ = "user_vehicles"
    id: int | None = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    vehicle_name: str
    brand: str | None = None
    model: str | None = None
    year: int | None = None
    city_consumption: float | None = None
    highway_consumption: float | None = None
    avg_consumption: float
    fuel_type: str
    is_primary: bool = Field(default=False)

class SavedLocation(SQLModel, table=True):
    __tablename__ = "saved_locations"
    id: int | None = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    name: str
    address: str | None = None
    coordinates: str
    category: str | None = None

class UserPreference(SQLModel, table=True):
    __tablename__ = "user_preferences"
    id: int | None = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    key: str
    value: str

class RouteHistory(SQLModel, table=True):
    __tablename__ = "route_history"
    id: int | None = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    origin: str
    destination: str
    distance_km: float
    duration_min: float
    created_at: datetime | None = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

class POIEmbedding(SQLModel, table=True):
    __tablename__ = "poi_embeddings"
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: str | None = None
    category: str | None = None
    location: str | None = None  # "lat,lon" string
    embedding: list[float] | None = Field(default=None, sa_column=Column(Vector(768)))  # Gemini text-embedding-004 = 768 dim
