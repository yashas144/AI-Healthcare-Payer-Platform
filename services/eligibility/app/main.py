"""Eligibility service: X12 270/271 and FHIR R4 endpoints over one engine."""
import logging
import os
import uuid

from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import fhir, x12
from .auth import issue_token, require_scope
from .data import MEMBERS
from .eligibility import check_eligibility

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
audit = logging.getLogger("audit")

app = FastAPI(title="Eligibility Service", version="0.1.0",
              description="X12 270/271 and FHIR R4 eligibility. Synthetic data only.")

FHIR_JSON = "application/fhir+json"


@app.middleware("http")
async def correlation_and_audit(request: Request, call_next):
    """Every request gets a correlation id. Audit log records WHO accessed WHAT,
    never the PHI itself (HIPAA Security Rule: audit controls + minimum necessary)."""
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
    scopes: list[str] = ["system/CoverageEligibilityRequest.write", "system/Coverage.read",
                         "system/Patient.read", "x12.eligibility"]


@app.post("/auth/token", tags=["dev"])
def dev_token(body: TokenRequest):
    """DEV ONLY. Disabled when ENV=production. Replaced by Entra ID in Phase 4."""
    if os.getenv("ENV") == "production":
        return JSONResponse({"detail": "Not available"}, status_code=404)
    return {"access_token": issue_token(body.client_id, body.scopes), "token_type": "Bearer"}


# ---------- X12 ----------

@app.post("/x12/270", response_class=PlainTextResponse, tags=["x12"],
          dependencies=[Depends(require_scope("x12.eligibility"))])
def x12_eligibility(raw: str = Body(..., media_type="text/plain")):
    try:
        parsed = x12.parse_270(raw)
    except x12.X12Error as exc:
        # Production would return a 999 implementation acknowledgment here
        return PlainTextResponse(f"999 REJECTED: {exc}", status_code=400)
    result = check_eligibility(parsed.request)
    return PlainTextResponse(x12.build_271(parsed, result), media_type="application/edi-x12")


# ---------- FHIR R4 ----------

@app.post("/fhir/CoverageEligibilityRequest/$submit", tags=["fhir"],
          dependencies=[Depends(require_scope("system/CoverageEligibilityRequest.write"))])
def fhir_eligibility(resource: dict = Body(...)):
    try:
        req = fhir.parse_request(resource)
    except (fhir.FhirError, ValueError) as exc:
        return JSONResponse(fhir.operation_outcome(str(exc)), 400, media_type=FHIR_JSON)
    result = check_eligibility(req)
    return JSONResponse(fhir.build_response(resource, req, result), media_type=FHIR_JSON)


@app.get("/fhir/Patient/{member_id}", tags=["fhir"],
         dependencies=[Depends(require_scope("system/Patient.read"))])
def fhir_patient(member_id: str):
    m = MEMBERS.get(member_id)
    if not m:
        return JSONResponse(fhir.operation_outcome("Patient not found", "not-found"), 404, media_type=FHIR_JSON)
    return JSONResponse(fhir.patient_resource(m), media_type=FHIR_JSON)


@app.get("/fhir/Coverage", tags=["fhir"],
         dependencies=[Depends(require_scope("system/Coverage.read"))])
def fhir_coverage_search(beneficiary: str):
    """FHIR search: GET /fhir/Coverage?beneficiary=Patient/M100000001 returns a Bundle."""
    member_id = beneficiary.split("/")[-1]
    m = MEMBERS.get(member_id)
    entries = [{"resource": fhir.coverage_resource(m)}] if m else []
    return JSONResponse({"resourceType": "Bundle", "type": "searchset",
                         "total": len(entries), "entry": entries}, media_type=FHIR_JSON)
