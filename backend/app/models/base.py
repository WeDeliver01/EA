"""SQLAlchemy declarative base. All ORM models in app.models import this."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
