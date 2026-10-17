"""Prior authorization service: X12 278 intake.

This branch handles intake only: parse, validate, and acknowledge.
Valid requests are PENDED (HCR*A4) with a tracking number; the rules engine
in the next branch decides approve / deny / pend-for-info.
"""
import logging
import os
import uuid
from dataclasses import asdict
from datetime import datetime

from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import x12_278
from .auth import issue_token, require_scope
from .models import Action, Decision

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
audit = logging.getLogger("audit")

app = FastAPI(title="Prior Authorization Service", version="0.2.0",
              description="X12 278 prior authorization intake. Synthetic data only.")

# In-memory case store; replaced by a database in Phase 4
CASES: dict[str, dict] = {}


def _tracking_number() -> str:
    """Payer-assigned review number, returned in HCR02 and REF*BB."""
    return f"PA{datetime.now():%y%m%d}{len(CASES) + 1:05d}"


@app.middleware("http")
async def correlation_and_audit(request: Request, call_next):
    cid = request.headers.get("x-correlation-id", str(uuid.uuid4()))
    response = await call_next(request)
    response.headers["x-correlation-id"] = cid
    audit.info("cid=%s method=%s path=%s status=%s", cid, request.method,
               request.url.path, response.status_code)
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


class TokenRequest(BaseModel):
    client_id: str
    scopes: list[str] = ["x12.prior-auth", "prior-auth.read"]


@app.post("/auth/token", tags=["dev"])
def dev_token(body: TokenRequest):
    if os.getenv("ENV") == "production":
        return JSONResponse({"detail": "Not available"}, status_code=404)
    return {"access_token": issue_token(body.client_id, body.scopes), "token_type": "Bearer"}


@app.post("/x12/278", response_class=PlainTextResponse, tags=["x12"],
          dependencies=[Depends(require_scope("x12.prior-auth"))])
def submit_278(raw: str = Body(..., media_type="text/plain")):
    try:
        req = x12_278.parse_278(raw)
    except (x12_278.X12Error, ValueError) as exc:
        # Structural failure -> 999 acknowledgment in production
        return PlainTextResponse(f"999 REJECTED: {exc}", status_code=400)

    rejections = x12_278.validate(req)
    if rejections:
        audit.info("278 rejected reasons=%s", [r.reason_code for r in rejections])
        return PlainTextResponse(x12_278.build_278_response(req, None, rejections),
                                 media_type="application/edi-x12")

    tracking = _tracking_number()
    decision = Decision(Action.PENDED, certification_number=tracking,
                        message="Received - pending clinical review")
    CASES[tracking] = {"status": decision.action.name, "request": req, "decision": decision,
                       "received_at": datetime.now().isoformat(timespec="seconds")}
    return PlainTextResponse(x12_278.build_278_response(req, decision),
                             media_type="application/edi-x12")


@app.get("/prior-auth/{tracking}", tags=["status"],
         dependencies=[Depends(require_scope("prior-auth.read"))])
def case_status(tracking: str):
    """Status lookup, the equivalent of a provider checking a portal."""
    case = CASES.get(tracking)
    if not case:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    req = case["request"]
    return {
        "tracking_number": tracking,
        "status": case["status"],
        "received_at": case["received_at"],
        "member_id": req.member_id,
        "diagnoses": req.diagnoses,
        "services": [asdict(s) for s in req.services],
        "requested_dates": [str(req.event_start), str(req.event_end)],
    }
