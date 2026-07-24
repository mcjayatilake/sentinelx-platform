"""SQLAlchemy ORM models.

Domain models (tenants, users, assets, scan runs, findings, ...) are added
incrementally alongside the features that need them. Every model should
import `Base` from `app.db.base` and be registered here (or imported by
`app.db.base`) so Alembic autogeneration can discover it.
"""
