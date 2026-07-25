"""Aggregates all API v1 endpoint routers."""

from fastapi import APIRouter

from app.api.v1.endpoints import api_keys, audit_events, auth, health, memberships, scans

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(api_keys.router)
api_router.include_router(memberships.router)
api_router.include_router(audit_events.router)
api_router.include_router(scans.router)
