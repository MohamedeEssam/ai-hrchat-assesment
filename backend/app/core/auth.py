from dataclasses import dataclass
from fastapi import Header, HTTPException


@dataclass(frozen=True)
class RunContext:
    user_id: str
    company_id: str
    role: str


TOKENS = {
    "maya-token": RunContext(user_id="maya", company_id="summit", role="employee"),
    "leila-token": RunContext(user_id="leila", company_id="summit", role="hr"),
    "ravi-token": RunContext(user_id="ravi", company_id="pebble", role="employee"),
}


def get_run_context(authorization: str = Header(default="")) -> RunContext:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1]
    ctx = TOKENS.get(token)
    if not ctx:
        raise HTTPException(status_code=401, detail="invalid token")
    return ctx
