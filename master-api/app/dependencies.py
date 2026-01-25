from fastapi import Request, HTTPException, status
from typing import Literal

from .auth import auth_manager

Role = Literal["admin", "guest", "none"]

def is_guest(request: Request) -> bool:
    """Check if the user has a guest session."""
    return request.cookies.get("guest_session") == "readonly"

def get_current_role(request: Request) -> Role:
    """
    Determine the current user's role based on cookies.
    
    Returns:
        "admin": If authenticated with dashboard password
        "guest": If has guest_session=readonly cookie
        "none": If no valid session found
    """
    if auth_manager().is_authenticated(request):
        return "admin"
    
    if is_guest(request):
        return "guest"
        
    return "none"

async def require_admin(request: Request):
    """
    Dependency to enforce admin access.
    Raises 403 if not authenticated as admin.
    """
    if not auth_manager().is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required"
        )
