import os
import uuid
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, String, Boolean, Float, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class Restaurant(Base):
    __tablename__ = 'restaurants'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    owner_email = Column(String, unique=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    secret_salt = Column(String, nullable=False)
    subscription_status = Column(String, default="active")
    
    menu_items = relationship("MenuItem", back_populates="restaurant")
    orders = relationship("Order", back_populates="restaurant")

class MenuItem(Base):
    __tablename__ = 'menu_items'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    restaurant_id = Column(UUID(as_uuid=True), ForeignKey('restaurants.id'), nullable=False)
    name = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    category = Column(String, nullable=False)
    modifiers = Column(JSONB, default=list) # e.g. [{"name": "Extra Cheese", "price": 20}]
    is_available = Column(Boolean, default=True)
    is_veg = Column(Boolean, default=True)

    restaurant = relationship("Restaurant", back_populates="menu_items")

class Order(Base):
    __tablename__ = 'orders'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    restaurant_id = Column(UUID(as_uuid=True), ForeignKey('restaurants.id'), nullable=False)
    table_number = Column(String, nullable=False)
    items = Column(JSONB, nullable=False) # Snapshot of items ordered
    total_price = Column(Float, nullable=False)
    status = Column(String, default="pending") # pending, preparing, ready, done
    payment_status = Column(String, default="unpaid") # unpaid, paid
    
    restaurant = relationship("Restaurant", back_populates="orders")