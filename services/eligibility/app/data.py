"""Synthetic payer data: plans, members, providers.

ALL DATA HERE IS FICTIONAL. Never load real PHI into this project.
In Phase 4 this moves to a database; an in-memory store keeps Phase 1 simple.
"""
from dataclasses import dataclass, field
from datetime import date


@dataclass
class Plan:
    plan_id: str
    name: str
    group_number: str
    plan_type: str                # HMO / PPO / EPO
    deductible_individual: float
    deductible_met: float
    oop_max: float
    oop_met: float
    copay_pcp: float              # primary care office visit
    copay_specialist: float
    coinsurance_pct: int          # member share after deductible
    # X12 service type codes that require prior auth under this plan
    auth_required_service_types: list[str] = field(default_factory=list)


@dataclass
class Member:
    member_id: str
    first_name: str
    last_name: str
    dob: date
    gender: str                   # M / F / U (X12 values)
    plan_id: str
    coverage_start: date
    coverage_end: date | None     # None = open-ended
    relationship: str = "self"    # self / spouse / child
    subscriber_id: str | None = None   # set when the member is a dependent


@dataclass
class Provider:
    npi: str
    name: str
    in_network: bool


PLANS = {
    "PPO-GOLD": Plan("PPO-GOLD", "Lone Star PPO Gold", "GRP1001", "PPO",
                     1500, 600, 6000, 900, 25, 50, 20, ["62", "4"]),
    "HMO-SILVER": Plan("HMO-SILVER", "Lone Star HMO Silver", "GRP2002", "HMO",
                       3000, 3000, 8000, 3200, 35, 70, 30, ["62", "4", "33"]),
}

MEMBERS = {
    "M100000001": Member("M100000001", "JOHN", "DOE", date(1985, 4, 12), "M",
                         "PPO-GOLD", date(2026, 1, 1), None),
    "M100000002": Member("M100000002", "JANE", "DOE", date(1987, 9, 3), "F",
                         "PPO-GOLD", date(2026, 1, 1), None, "spouse", "M100000001"),
    "M200000001": Member("M200000001", "MARIA", "GARCIA", date(1972, 1, 30), "F",
                         "HMO-SILVER", date(2025, 7, 1), None),
    # Terminated coverage -> inactive on any date after 2026-06-30
    "M300000001": Member("M300000001", "ROBERT", "SMITH", date(1960, 11, 5), "M",
                         "PPO-GOLD", date(2024, 1, 1), date(2026, 6, 30)),
}

PROVIDERS = {
    "1234567893": Provider("1234567893", "DENTON FAMILY MEDICINE", True),
    "1245319599": Provider("1245319599", "NORTH TEXAS IMAGING", True),
    "1003000126": Provider("1003000126", "OUT OF STATE CLINIC", False),
}

PAYER = {"name": "LONE STAR HEALTH PLAN", "id": "LSHP01"}
