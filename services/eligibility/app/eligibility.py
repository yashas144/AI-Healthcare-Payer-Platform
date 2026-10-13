"""Format-independent eligibility engine.

Both the X12 270/271 path and the FHIR path call `check_eligibility`.
Keeping business logic separate from wire formats is the key design decision:
X12 and FHIR are just two "adapters" around one domain model.
"""
from dataclasses import dataclass, field
from datetime import date

from .data import MEMBERS, PLANS, PROVIDERS, Member, Plan

# Subset of X12 service type codes (EQ01 / EB03)
SERVICE_TYPES = {
    "30": "Health Benefit Plan Coverage",
    "1": "Medical Care",
    "33": "Chiropractic",
    "47": "Hospital",
    "62": "MRI/CAT Scan",
    "86": "Emergency Services",
    "88": "Pharmacy",
    "98": "Professional (Physician) Visit - Office",
    "4": "Diagnostic X-Ray",
    "UC": "Urgent Care",
}


@dataclass
class EligibilityRequest:
    member_id: str
    last_name: str | None = None
    dob: date | None = None
    provider_npi: str | None = None
    service_date: date = field(default_factory=date.today)
    service_types: list[str] = field(default_factory=lambda: ["30"])


@dataclass
class Benefit:
    service_type: str
    description: str
    in_network: bool
    copay: float | None = None
    coinsurance_pct: int | None = None
    deductible_remaining: float | None = None
    oop_remaining: float | None = None
    auth_required: bool = False


@dataclass
class EligibilityResult:
    found: bool
    active: bool = False
    member: Member | None = None
    plan: Plan | None = None
    provider_in_network: bool | None = None
    benefits: list[Benefit] = field(default_factory=list)
    # AAA reject reason code from X12 (e.g. 75 = subscriber not found)
    reject_reason: str | None = None
    message: str = ""


def _is_active(m: Member, on: date) -> bool:
    return m.coverage_start <= on and (m.coverage_end is None or on <= m.coverage_end)


def check_eligibility(req: EligibilityRequest) -> EligibilityResult:
    member = MEMBERS.get(req.member_id)
    if member is None:
        return EligibilityResult(False, reject_reason="75", message="Subscriber/member not found")

    # Demographic match protects against a typo'd ID returning someone else's data
    if req.last_name and req.last_name.upper() != member.last_name:
        return EligibilityResult(False, reject_reason="73", message="Invalid/missing subscriber name")
    if req.dob and req.dob != member.dob:
        return EligibilityResult(False, reject_reason="71", message="Patient birth date does not match")

    plan = PLANS[member.plan_id]
    active = _is_active(member, req.service_date)
    provider = PROVIDERS.get(req.provider_npi) if req.provider_npi else None
    in_net = provider.in_network if provider else None

    result = EligibilityResult(True, active, member, plan, in_net,
                               message="Active coverage" if active else "Coverage inactive on service date")
    if not active:
        return result

    for st in req.service_types:
        network = in_net if in_net is not None else True
        is_office = st in ("98", "1")
        result.benefits.append(Benefit(
            service_type=st,
            description=SERVICE_TYPES.get(st, "Unknown service type"),
            in_network=network,
            copay=(plan.copay_pcp if st == "98" else plan.copay_specialist) if is_office else None,
            coinsurance_pct=plan.coinsurance_pct if network else min(plan.coinsurance_pct + 20, 100),
            deductible_remaining=max(plan.deductible_individual - plan.deductible_met, 0),
            oop_remaining=max(plan.oop_max - plan.oop_met, 0),
            auth_required=st in plan.auth_required_service_types,
        ))
    return result
