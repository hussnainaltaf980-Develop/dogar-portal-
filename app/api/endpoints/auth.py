from fastapi import APIRouter, Depends, HTTPException, Response, Request
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
import secrets

from app.db.session import get_db
from app.core.config import settings
from app.core.security import verify_password, create_access_token, hash_password
from app.models import User
from app.models.lookups import LoginHistory, PasswordResetToken, SupportMessage
from app.schemas.schemas import LoginRequest, TokenResponse

router = APIRouter()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")[:300]

    # Resolve the login identifier — username takes priority, email is the fallback
    identifier = (payload.username or "").strip() or (payload.email or "").strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Username or email is required")

    # Try username first, then email
    user = (
        db.query(User).filter(User.username == identifier).first()
        or db.query(User).filter(User.email == identifier).first()
    )

    def record(status: str):
        db.add(LoginHistory(email=identifier, status=status, ip_address=ip, user_agent=ua))
        db.commit()

    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        record("Failed")
        raise HTTPException(status_code=401, detail="Invalid username or password")

    record("Success")
    token = create_access_token(subject=user.id)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=60 * 60 * 24,
    )
    # The frontend uses `must_change_password` to redirect the user to a
    # forced password-change screen before the rest of the app is
    # accessible. This is set to True for the bootstrap admin so the
    # documented default credentials cannot survive past first login.
    return TokenResponse(
        access_token=token,
        user={
            "id": user.id, "name": user.name, "email": user.email,
            "role": user.role,
            "must_change_password": bool(getattr(user, "must_change_password", False)),
        },
    )


from app.core.deps import get_current_user  # noqa: E402 — used by /change-password


@router.post("/change-password")
def change_password(
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Authenticated password change.

    Used by:
      1. The forced first-login flow (bootstrap admin)
      2. Any user who wants to rotate their own password

    Requires the current password for verification + a new password
    that satisfies the minimum strength rules.
    """
    current = payload.get("current_password") or ""
    new_pw  = payload.get("new_password") or ""

    if not verify_password(current, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")

    # Strength rules — match the production guardrails in config.py
    if len(new_pw) < 12:
        raise HTTPException(400, "New password must be at least 12 characters long")
    if new_pw.strip().lower() in {"admin123", "admin", "password", "123456", "changeme"}:
        raise HTTPException(400, "That password is on the well-known weak-password list")
    if new_pw == current:
        raise HTTPException(400, "New password must differ from the current one")

    user.password_hash = hash_password(new_pw)
    user.must_change_password = False
    db.commit()
    return {"ok": True, "message": "Password updated successfully"}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("access_token", samesite="lax", secure=settings.cookie_secure)
    return {"ok": True}


@router.post("/forgot-password")
def forgot_password(payload: dict, db: Session = Depends(get_db)):
    """Generate a one-time reset token. Returns the token in the response
    (in production it would be emailed). 30-minute expiry."""
    email = (payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "Email is required")

    user = db.query(User).filter(User.email == email).first()
    # Always return success message (don't leak whether the email exists)
    if not user or not user.is_active:
        return {"ok": True, "message": "If an account exists for that email, a reset link has been generated."}

    token = secrets.token_urlsafe(32)
    pr = PasswordResetToken(
        user_id=user.id, email=user.email, token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    db.add(pr); db.commit()

    # SECURITY: Never leak the raw token to the client in production. The
    # token would be delivered by email in a real deployment. For internal
    # / development portals with no email transport, set
    # EXPOSE_RESET_TOKEN=true to surface it directly.
    resp = {
        "ok": True,
        "message": "If an account exists for that email, a password reset link has been generated.",
    }
    if settings.EXPOSE_RESET_TOKEN and not settings.is_production:
        resp["reset_url"] = f"/reset-password?token={token}"
        resp["token"] = token
    return resp


@router.post("/reset-password")
def reset_password(payload: dict, db: Session = Depends(get_db)):
    token = (payload.get("token") or "").strip()
    new_password = payload.get("new_password") or ""
    if not token or not new_password:
        raise HTTPException(400, "Token and new password are required")
    if len(new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    pr = db.query(PasswordResetToken).filter(PasswordResetToken.token == token).first()
    if not pr:
        raise HTTPException(400, "Invalid or expired token")
    if pr.used_at:
        raise HTTPException(400, "This reset link has already been used")
    if pr.expires_at < datetime.now(timezone.utc):
        raise HTTPException(400, "This reset link has expired")

    user = db.query(User).filter(User.id == pr.user_id).first()
    if not user:
        raise HTTPException(400, "User not found")

    user.password_hash = hash_password(new_password)
    pr.used_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "message": "Password updated successfully. You can now sign in."}


@router.post("/support-message")
def submit_support_message(payload: dict, db: Session = Depends(get_db)):
    """Submit a message to HussnainTechVertex support team."""
    msg = SupportMessage(
        name=(payload.get("name") or "").strip()[:150],
        email=(payload.get("email") or "").strip()[:150],
        phone=(payload.get("phone") or "").strip()[:50],
        subject=(payload.get("subject") or "").strip()[:255],
        message=(payload.get("message") or "").strip(),
        status="open",
    )
    if not msg.email or not msg.message:
        raise HTTPException(400, "Email and message are required")
    db.add(msg); db.commit(); db.refresh(msg)
    return {"ok": True, "id": msg.id, "message": "Your message has been received. Our team will contact you within 24 hours."}
