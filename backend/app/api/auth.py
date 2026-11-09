"""POST /login (demo auth: one fixed token per seeded user)."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.auth import TOKENS

router = APIRouter(tags=["auth"])


class LoginBody(BaseModel):
    username: str


@router.post("/login")
def login(body: LoginBody) -> dict:
    # Identity and company come only from this token, never from request bodies.
    token = f"{body.username}-token"
    if token not in TOKENS:
        raise HTTPException(status_code=401, detail="unknown user")
    return {"token": token}
