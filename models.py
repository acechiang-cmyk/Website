from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from database import Base

class Agency(Base):
    __tablename__ = "agencies"
    id         = Column(Integer, primary_key=True)
    name       = Column(String(200), nullable=False)
    slug       = Column(String(100), unique=True, nullable=False)
    plan       = Column(String(50), default="trial")
    created_at = Column(DateTime, default=datetime.utcnow)

    users    = relationship("User", back_populates="agency", cascade="all, delete")
    leads    = relationship("Lead", back_populates="agency", cascade="all, delete")
    contacts = relationship("Contact", back_populates="agency", cascade="all, delete")

class User(Base):
    __tablename__ = "users"
    id            = Column(Integer, primary_key=True)
    agency_id     = Column(Integer, ForeignKey("agencies.id"), nullable=False)
    email         = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    name          = Column(String(200))
    role          = Column(String(50), default="agent")  # owner | admin | agent
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    agency         = relationship("Agency", back_populates="users")
    assigned_leads = relationship("Lead", back_populates="assigned_to_user")

class Lead(Base):
    __tablename__ = "leads"
    id          = Column(Integer, primary_key=True)
    agency_id   = Column(Integer, ForeignKey("agencies.id"), nullable=False)
    session_id  = Column(String(100))
    name        = Column(String(200))
    email       = Column(String(255))
    phone       = Column(String(50))
    notes       = Column(Text)
    score       = Column(String(20), default="warm")   # hot | warm | cold
    source      = Column(String(100), default="chatbot")
    stage       = Column(String(50), default="new")    # new | contacted | nurture | showing | closed
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    agency           = relationship("Agency", back_populates="leads")
    assigned_to_user = relationship("User", back_populates="assigned_leads")
    messages         = relationship("Message", back_populates="lead", cascade="all, delete")

class Message(Base):
    __tablename__ = "messages"
    id         = Column(Integer, primary_key=True)
    lead_id    = Column(Integer, ForeignKey("leads.id"), nullable=True)
    agency_id  = Column(Integer, ForeignKey("agencies.id"), nullable=False)
    session_id = Column(String(100))
    role       = Column(String(20))
    content    = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    lead = relationship("Lead", back_populates="messages")

class Contact(Base):
    __tablename__ = "contacts"
    id          = Column(Integer, primary_key=True)
    agency_id   = Column(Integer, ForeignKey("agencies.id"), nullable=False)
    name        = Column(String(200), nullable=False)
    email       = Column(String(255))
    phone       = Column(String(50))
    budget_min  = Column(Integer)
    budget_max  = Column(Integer)
    timeline    = Column(String(200))
    tags        = Column(String(500))
    notes       = Column(Text)
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    agency = relationship("Agency", back_populates="contacts")
