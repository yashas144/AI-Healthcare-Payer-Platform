"""FHIR R4 adapter: CoverageEligibilityRequest -> CoverageEligibilityResponse.

FHIR is the modern equivalent of the 270/271. CMS-0057-F pushes payers toward
FHIR APIs; X12 stays in place for HIPAA-mandated transactions. Mapping:

  270 NM1*IL (member)          -> Patient + CoverageEligibilityRequest.patient
  270 NM1*1P (provider NPI)    -> CoverageEligibilityRequest.provider
  270 DTP*291                  -> CoverageEligibilityRequest.servicedDate
  270 EQ (service type)        -> CoverageEligibilityRequest.item[].category
  271 EB*1 / EB*6              -> CoverageEligibilityResponse.insurance[].inforce
  271 EB*B/A/C/G               -> insurance[].item[].benefit[]
  271 EB11 = Y                 -> insurance[].item[].authorizationRequired
  271 AAA reject               -> CoverageEligibilityResponse.error[]
"""
from datetime import date, datetime, timezone
from uuid import uuid4

from .data import MEMBERS, PAYER, PLANS, Member
from .eligibility import EligibilityRequest, EligibilityResult

X12_SERVICE_TYPE_SYSTEM = "https://x12.org/codes/service-type-codes"
NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"
MEMBER_ID_SYSTEM = "urn:lshp:member-id"


class FhirError(ValueError):
    pass


def operation_outcome(diagnostics: str, code: str = "invalid") -> dict:
    return {"resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": code, "diagnostics": diagnostics}]}


def patient_resource(m: Member) -> dict:
    return {
        "resourceType": "Patient",
        "id": m.member_id,
        "identifier": [{"system": MEMBER_ID_SYSTEM, "value": m.member_id}],
        "name": [{"family": m.last_name.title(), "given": [m.first_name.title()]}],
        "gender": {"M": "male", "F": "female"}.get(m.gender, "unknown"),
        "birthDate": m.dob.isoformat(),
    }


def coverage_resource(m: Member) -> dict:
    plan = PLANS[m.plan_id]
    period = {"start": m.coverage_start.isoformat()}
    if m.coverage_end:
        period["end"] = m.coverage_end.isoformat()
    active = m.coverage_end is None or m.coverage_end >= date.today()
    return {
        "resourceType": "Coverage",
        "id": f"cov-{m.member_id}",
        "status": "active" if active else "cancelled",
        "subscriberId": m.subscriber_id or m.member_id,
        "beneficiary": {"reference": f"Patient/{m.member_id}"},
        "relationship": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/subscriber-relationship",
            "code": m.relationship}]},
        "period": period,
        "payor": [{"display": PAYER["name"]}],
        "class": [
            {"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/coverage-class", "code": "group"}]},
             "value": plan.group_number},
            {"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/coverage-class", "code": "plan"}]},
             "value": plan.plan_id, "name": plan.name},
        ],
    }


def parse_request(res: dict) -> EligibilityRequest:
    if res.get("resourceType") != "CoverageEligibilityRequest":
        raise FhirError("resourceType must be CoverageEligibilityRequest")
    ref = (res.get("patient") or {}).get("reference", "")
    if not ref.startswith("Patient/"):
        raise FhirError("patient.reference must be 'Patient/<member id>'")
    member_id = ref.split("/", 1)[1]

    npi = None
    prov = res.get("provider") or {}
    ident = prov.get("identifier") or {}
    if ident.get("system") == NPI_SYSTEM:
        npi = ident.get("value")

    served = res.get("servicedDate")
    service_date = date.fromisoformat(served) if served else date.today()

    service_types = []
    for item in res.get("item", []):
        for c in (item.get("category") or {}).get("coding", []):
            if c.get("system") == X12_SERVICE_TYPE_SYSTEM:
                service_types.append(c["code"])
    return EligibilityRequest(member_id, None, None, npi, service_date, service_types or ["30"])


def _money(v: float) -> dict:
    return {"value": round(v, 2), "currency": "USD"}


HL7_BENEFIT_TYPE = "http://terminology.hl7.org/CodeSystem/benefit-type"
# HL7's benefit-type code system has no out-of-pocket-max code, so a local code system is used
LOCAL_BENEFIT_TYPE = "urn:lshp:benefit-type"


def _benefit(code: str, system: str = HL7_BENEFIT_TYPE, **value) -> dict:
    return {"type": {"coding": [{"system": system, "code": code}]}, **value}


def build_response(req_resource: dict, req: EligibilityRequest, r: EligibilityResult) -> dict:
    resp = {
        "resourceType": "CoverageEligibilityResponse",
        "id": str(uuid4()),
        "status": "active",
        "purpose": ["validation", "benefits"],
        "patient": {"reference": f"Patient/{req.member_id}"},
        "servicedDate": req.service_date.isoformat(),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request": {"reference": f"CoverageEligibilityRequest/{req_resource.get('id', 'unknown')}"},
        "outcome": "complete" if r.found else "error",
        "disposition": r.message,
        "insurer": {"display": PAYER["name"]},
    }
    if not r.found:
        resp["error"] = [{"code": {"coding": [{
            "system": "https://x12.org/codes/aaa-reject-reason", "code": r.reject_reason,
            "display": r.message}]}}]
        return resp

    items = []
    for b in r.benefits:
        benefits = []
        if b.copay is not None:
            benefits.append(_benefit("copay", allowedMoney=_money(b.copay)))
        if b.coinsurance_pct is not None:
            benefits.append(_benefit("copay-percent",
                                     allowedUnsignedInt=b.coinsurance_pct))
        benefits.append(_benefit("deductible", allowedMoney=_money(b.deductible_remaining)))
        benefits.append(_benefit("out-of-pocket-remaining", LOCAL_BENEFIT_TYPE,
                                 allowedMoney=_money(b.oop_remaining)))
        items.append({
            "category": {"coding": [{"system": X12_SERVICE_TYPE_SYSTEM, "code": b.service_type,
                                     "display": b.description}]},
            "network": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/benefit-network",
                                    "code": "in" if b.in_network else "out"}]},
            "unit": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/benefit-unit",
                                 "code": "individual"}]},
            "term": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/benefit-term",
                                 "code": "annual"}]},
            "benefit": benefits,
            "authorizationRequired": b.auth_required,
        })

    resp["insurance"] = [{
        "coverage": {"reference": f"Coverage/cov-{req.member_id}"},
        "inforce": r.active,
        "item": items,
    }]
    return resp
