import os
import bcrypt
from datetime import datetime, timedelta
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from database import get_db

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod-32chars!!")
ALGORITHM  = "HS256"
TOKEN_TTL  = 60 * 24 * 30  # 30 days in minutes

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw[:72].encode(), bcrypt.gensalt()).decode()

def verify_password(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw[:72].encode(), hashed.encode())

def create_token(user_id: int, agency_id: int, role: str) -> str:
    exp = datetime.utcnow() + timedelta(minutes=TOKEN_TTL)
    return jwt.encode({"sub": str(user_id), "agency": agency_id, "role": role, "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return {}

def require_auth(request: Request, db: Session = Depends(get_db)):
    from models import User
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        token = request.cookies.get("token", "")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(401, "Not authenticated")
    user = db.query(User).filter(User.id == int(payload["sub"]), User.is_active == True).first()
    if not user:
        raise HTTPException(401, "User not found")
    return user

def require_admin(user=Depends(require_auth)):
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Admin required")
    return user

def require_owner(user=Depends(require_auth)):
    if user.role != "owner":
        raise HTTPException(403, "Owner required")
    return user
