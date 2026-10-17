"""Format-independent prior authorization domain model.

Same idea as the eligibility service: X12 278 (and later FHIR/Da Vinci PAS)
are adapters that translate into and out of these objects.
"""
from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class Action(str, Enum):
    """HCR01 - health care services review action codes."""
    CERTIFIED = "A1"           # approved in full
    PARTIAL = "A2"             # approved in part
    NOT_CERTIFIED = "A3"       # denied
    PENDED = "A4"              # pending (needs review or more information)
    MODIFIED = "A6"            # approved with changes (fewer units, other dates)
    CANCELLED = "C"
    CONTACT_PAYER = "CT"
    NO_ACTION = "NA"


@dataclass
class Party:
    name: str
    npi: str | None = None
    first_name: str | None = None


@dataclass
class ServiceLine:
    procedure_code: str               # CPT/HCPCS, e.g. 72148
    code_qualifier: str = "HC"        # HC = CPT/HCPCS
    modifiers: list[str] = field(default_factory=list)
    charge: float | None = None
    units: int = 1
    service_date: date | None = None


@dataclass
class PriorAuthRequest:
    # Envelope / tracking
    sender_id: str
    receiver_id: str
    interchange_control: str
    group_control: str
    bht_reference: str
    trace_number: str | None

    # Who
    umo: Party                        # utilization management organization (the payer)
    requester: Party                  # ordering provider
    member_id: str
    member_last: str
    member_first: str | None
    member_dob: date | None
    member_gender: str | None
    servicing_provider: Party | None  # who performs the service (imaging center)

    # What and why
    request_category: str             # UM01: HS outpatient service, AR admission, SC specialty referral
    certification_type: str           # UM02: I initial, 1 appeal, 2 renewal/extension...
    service_type: str | None          # UM03: X12 service type code (62 = MRI/CT)
    place_of_service: str | None      # UM04-1: 11 office, 22 outpatient hospital
    diagnoses: list[str]              # ICD-10-CM, first one is principal
    event_start: date | None
    event_end: date | None
    services: list[ServiceLine]


@dataclass
class Decision:
    action: Action
    certification_number: str | None = None   # HCR02, the auth number the claim must carry
    reason_code: str | None = None            # HCR03 (payer-specific in this simulation)
    message: str | None = None                # MSG free text
    approved_start: date | None = None
    approved_end: date | None = None
    approved_units: int | None = None


@dataclass
class Rejection:
    """AAA segment: the request could not be processed at all."""
    loop: str            # "UMO", "REQUESTER", "SUBSCRIBER", "EVENT"
    reason_code: str     # AAA03, e.g. 15 required data missing, 75 member not found
    follow_up: str = "C" # C = correct and resubmit
    detail: str = ""
