"""Pydantic request/response schemas for the API layer.

Endpoints must always validate input and serialize output through schemas
defined here (or in the relevant `app.modules.*` package) rather than
exposing SQLAlchemy models directly.
"""
