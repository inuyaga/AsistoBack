"""API routers package."""

from fastapi import APIRouter
from app.api.routes import attendance, auth, collaborators, devices, health, mobile, permissions, users

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(permissions.router, tags=["roles & permissions"])
api_router.include_router(collaborators.router, tags=["companies & collaborators"])
api_router.include_router(attendance.router, tags=["shifts & attendance"])
api_router.include_router(devices.router, prefix="/devices", tags=["hikvision devices (ISAPI)"])
api_router.include_router(mobile.router, tags=["mobile app (asisto)"])
