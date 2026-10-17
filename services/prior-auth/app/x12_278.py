"""X12 005010X217 (278 Health Care Services Review) parser, validator and response builder.

Hierarchy of a 278 request (each level is an HL segment, HL03 says which level):

  HL*1**20    UMO (payer's utilization management org)   NM1*X3
  HL*2*1*21   Requester (ordering provider)              NM1*1P  (NPI in NM109)
  HL*3*2*22   Subscriber (member)                        NM1*IL, DMG
  HL*4*3*EV   Patient event (the "why")                  TRN, UM, DTP*AAH, HI, NM1*SJ
  HL*5*4*SS   Service (the "what")                       UM, SV1, DTP*472

The response repeats the same hierarchy and adds:
  HCR   the decision (A1 approved, A3 denied, A4 pended, A6 modified)
  AAA   when the request could not be processed at all

Simplified for a portfolio project: production systems sit behind an EDI translator
that enforces the full implementation guide.
"""
import re
from datetime import date, datetime

from .models import Action, Decision, Party, PriorAuthRequest, Rejection, ServiceLine

IG_VERSION = "005010X217"
ICD10_RE = re.compile(r"^[A-TV-Z][0-9][0-9A-Z][0-9A-Z]{0,4}$")   # X12 carries ICD-10 without the dot
CPT_RE = re.compile(r"^[0-9]{4}[0-9A-Z]$")                       # 5 chars, e.g. 72148, 0001U
HCPCS_RE = re.compile(r"^[A-V][0-9]{4}$")                        # e.g. E0601


class X12Error(ValueError):
    """Structural problem: answered with a 999 rejection, not a 278 response."""


# ---------- low-level helpers ----------

def _segments(raw: str) -> list[list[str]]:
    raw = raw.strip().replace("\r", "").replace("\n", "")
    if not raw.startswith("ISA") or len(raw) < 106:
        raise X12Error("Interchange must start with a 106-character ISA segment")
    element, segment = raw[3], raw[105]
    return [s.split(element) for s in raw.split(segment) if s.strip()]


def _get(seg: list[str], i: int) -> str:
    return seg[i] if len(seg) > i else ""


def _d8(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _rd8(value: str) -> tuple[date, date]:
    start, end = value.split("-")
    return _d8(start), _d8(end)


# ---------- parsing ----------

def parse_278(raw: str) -> PriorAuthRequest:
    segs = _segments(raw)
    ids = [s[0] for s in segs]
    for required in ("ISA", "GS", "ST", "BHT", "HL", "SE", "GE", "IEA"):
        if required not in ids:
            raise X12Error(f"Missing required segment {required}")

    st = segs[ids.index("ST")]
    if st[1] != "278":
        raise X12Error(f"Expected transaction set 278, got {st[1]}")
    if not _get(st, 3).startswith(IG_VERSION):
        raise X12Error(f"Expected implementation guide {IG_VERSION}, got {_get(st, 3)}")
    bht = segs[ids.index("BHT")]
    if _get(bht, 2) != "13":
        raise X12Error("BHT02 must be 13 (request)")

    st_i, se_i = ids.index("ST"), ids.index("SE")
    if int(segs[se_i][1]) != se_i - st_i + 1:
        raise X12Error(f"SE01 count {segs[se_i][1]} != actual {se_i - st_i + 1}")

    isa = segs[0]
    gs = segs[ids.index("GS")]

    level = None
    umo = requester = servicing = None
    member = {"id": "", "last": "", "first": None, "dob": None, "gender": None}
    event = {"category": "", "cert": "", "stc": None, "pos": None,
             "dx": [], "start": None, "end": None, "trace": None}
    services: list[ServiceLine] = []

    for s in segs:
        tag = s[0]
        if tag == "HL":
            level = _get(s, 3)
            continue

        if level == "20" and tag == "NM1":
            umo = Party(_get(s, 3), _get(s, 9) or None)
        elif level == "21" and tag == "NM1":
            requester = Party(_get(s, 3), _get(s, 9) if _get(s, 8) == "XX" else None, _get(s, 4) or None)
        elif level in ("22", "23"):
            if tag == "NM1" and _get(s, 1) in ("IL", "QC"):
                member.update(last=_get(s, 3), first=_get(s, 4) or None, id=_get(s, 9))
            elif tag == "DMG" and _get(s, 1) == "D8":
                member.update(dob=_d8(s[2]), gender=_get(s, 3) or None)
        elif level == "EV":
            if tag == "TRN":
                event["trace"] = _get(s, 2)
            elif tag == "UM":
                event.update(category=_get(s, 1), cert=_get(s, 2), stc=_get(s, 3) or None,
                             pos=_get(s, 4).split(":")[0] or None)
            elif tag == "DTP" and _get(s, 1) == "AAH":
                if _get(s, 2) == "RD8":
                    event["start"], event["end"] = _rd8(s[3])
                else:
                    event["start"] = event["end"] = _d8(s[3])
            elif tag == "HI":
                # HI*ABK:M5441*ABF:M5416  ABK = principal ICD-10, ABF = other ICD-10
                for comp in s[1:]:
                    if ":" in comp:
                        event["dx"].append(comp.split(":")[1])
            elif tag == "NM1" and _get(s, 1) == "SJ":
                servicing = Party(_get(s, 3), _get(s, 9) if _get(s, 8) == "XX" else None)
        elif level == "SS":
            if tag == "SV1":
                # SV1*HC:72148:26*1500*UN*1   composite: qualifier, code, modifiers...
                comp = s[1].split(":")
                services.append(ServiceLine(
                    procedure_code=_get(comp, 1), code_qualifier=comp[0], modifiers=comp[2:6],
                    charge=float(s[2]) if _get(s, 2) else None,
                    units=int(float(s[4])) if _get(s, 4) else 1))
            elif tag == "DTP" and _get(s, 1) == "472" and services:
                services[-1].service_date = _d8(s[3])

    return PriorAuthRequest(
        sender_id=isa[6].strip(), receiver_id=isa[8].strip(),
        interchange_control=isa[13], group_control=gs[6], bht_reference=_get(bht, 3),
        trace_number=event["trace"],
        umo=umo or Party(""), requester=requester or Party(""),
        member_id=member["id"], member_last=member["last"], member_first=member["first"],
        member_dob=member["dob"], member_gender=member["gender"],
        servicing_provider=servicing,
        request_category=event["category"], certification_type=event["cert"],
        service_type=event["stc"], place_of_service=event["pos"], diagnoses=event["dx"],
        event_start=event["start"], event_end=event["end"], services=services,
    )


# ---------- intake validation (business-level, answered with AAA) ----------

def validate(req: PriorAuthRequest) -> list[Rejection]:
    """Checks a payer runs before anyone reviews the request clinically."""
    errors: list[Rejection] = []
    if not req.requester.npi or not re.fullmatch(r"\d{10}", req.requester.npi):
        errors.append(Rejection("REQUESTER", "15", detail="Requesting provider NPI missing or invalid"))
    if not req.member_id:
        errors.append(Rejection("SUBSCRIBER", "72", detail="Member ID missing"))
    if not req.member_last:
        errors.append(Rejection("SUBSCRIBER", "73", detail="Member name missing"))
    if req.member_dob is None:
        errors.append(Rejection("SUBSCRIBER", "58", detail="Member date of birth missing"))
    if not req.diagnoses:
        errors.append(Rejection("EVENT", "15", detail="At least one ICD-10 diagnosis is required"))
    for dx in req.diagnoses:
        if not ICD10_RE.match(dx):
            errors.append(Rejection("EVENT", "15", detail=f"Invalid ICD-10-CM code {dx}"))
    if req.event_start is None:
        errors.append(Rejection("EVENT", "57", detail="Event date (DTP*AAH) missing"))
    elif req.event_end and req.event_end < req.event_start:
        errors.append(Rejection("EVENT", "57", detail="Event end date before start date"))
    if req.request_category == "HS" and not req.services:
        errors.append(Rejection("EVENT", "15", detail="Outpatient request (UM01=HS) needs a service line"))
    for line in req.services:
        valid = CPT_RE.match(line.procedure_code) or HCPCS_RE.match(line.procedure_code)
        if line.code_qualifier != "HC" or not valid:
            errors.append(Rejection("EVENT", "15", detail=f"Invalid procedure code {line.procedure_code}"))
        if line.units < 1:
            errors.append(Rejection("EVENT", "15", detail="Service units must be at least 1"))
    return errors


# ---------- response ----------

def _isa(sender: str, receiver: str, control: str, now: datetime) -> str:
    return "*".join(["ISA", "00", " " * 10, "00", " " * 10, "ZZ", sender.ljust(15)[:15],
                     "ZZ", receiver.ljust(15)[:15], now.strftime("%y%m%d"), now.strftime("%H%M"),
                     "^", "00501", control.zfill(9), "0", "T", ":"])


def build_278_response(req: PriorAuthRequest, decision: Decision | None,
                       rejections: list[Rejection] | None = None,
                       now: datetime | None = None) -> str:
    """One of `decision` or `rejections` drives the response."""
    now = now or datetime.now()
    rejections = rejections or []
    body: list[str] = []

    def seg(*parts):
        body.append("*".join(str(p) for p in parts).rstrip("*"))

    def aaa_for(loop: str) -> bool:
        hits = [r for r in rejections if r.loop == loop]
        for r in hits:
            seg("AAA", "N", "", r.reason_code, r.follow_up)   # N = request not valid
        return bool(hits)

    seg("ST", "278", "0001", f"{IG_VERSION}")
    seg("BHT", "0007", "11", req.bht_reference, now.strftime("%Y%m%d"), now.strftime("%H%M"))

    seg("HL", "1", "", "20", "1")
    seg("NM1", "X3", "2", req.umo.name, "", "", "", "", "PI", req.umo.npi or "")
    stop = aaa_for("UMO")

    if not stop:
        seg("HL", "2", "1", "21", "1")
        seg("NM1", "1P", "1" if req.requester.first_name else "2", req.requester.name,
            req.requester.first_name or "", "", "", "", "XX", req.requester.npi or "")
        stop = aaa_for("REQUESTER")

    if not stop:
        seg("HL", "3", "2", "22", "1")
        seg("NM1", "IL", "1", req.member_last, req.member_first or "", "", "", "", "MI", req.member_id)
        stop = aaa_for("SUBSCRIBER")

    if not stop:
        seg("HL", "4", "3", "EV", "1" if req.services else "0")
        if req.trace_number:
            seg("TRN", "2", req.trace_number, "9" + req.umo.npi if req.umo.npi else "")
        if aaa_for("EVENT"):
            seg("UM", req.request_category, req.certification_type, req.service_type or "")
        elif decision:
            seg("UM", req.request_category, req.certification_type, req.service_type or "")
            seg("HCR", decision.action.value, decision.certification_number or "",
                decision.reason_code or "")
            if decision.certification_number:
                seg("REF", "BB", decision.certification_number)   # BB = authorization number
            start = decision.approved_start or req.event_start
            end = decision.approved_end or req.event_end
            if decision.action in (Action.CERTIFIED, Action.PARTIAL, Action.MODIFIED) and start:
                seg("DTP", "AAH", "RD8", f"{start:%Y%m%d}-{(end or start):%Y%m%d}")
            seg("HI", *[f"{'ABK' if i == 0 else 'ABF'}:{dx}" for i, dx in enumerate(req.diagnoses)])
            if decision.message:
                seg("MSG", decision.message.upper()[:264])

            for n, line in enumerate(req.services, start=5):
                seg("HL", str(n), "4", "SS", "0")
                seg("UM", req.request_category, req.certification_type, req.service_type or "")
                seg("HCR", decision.action.value, decision.certification_number or "",
                    decision.reason_code or "")
                units = decision.approved_units if decision.approved_units is not None else line.units
                code = ":".join([line.code_qualifier, line.procedure_code, *line.modifiers])
                seg("SV1", code, f"{line.charge:.2f}" if line.charge is not None else "", "UN", units)

    seg("SE", str(len(body) + 1), "0001")
    gs = "*".join(["GS", "HI", req.receiver_id, req.sender_id, now.strftime("%Y%m%d"),
                   now.strftime("%H%M"), req.group_control, "X", IG_VERSION])
    return "~".join([_isa(req.receiver_id, req.sender_id, req.interchange_control, now), gs, *body,
                     f"GE*1*{req.group_control}", f"IEA*1*{req.interchange_control.zfill(9)}"]) + "~"


# ---------- request builder (plays the provider in tests and samples) ----------

def build_278_request(*, member_id: str, last: str, first: str, dob: date, gender: str,
                      requester_npi: str, requester_last: str, requester_first: str,
                      servicing_npi: str, servicing_name: str, diagnoses: list[str],
                      procedures: list[tuple[str, int, float]], start: date, end: date,
                      service_type: str = "62", place_of_service: str = "11",
                      sender: str = "DENTONFM", control: str = "1") -> str:
    now = datetime.now()
    body = [
        f"ST*278*0001*{IG_VERSION}",
        f"BHT*0007*13*PA{control}*{now:%Y%m%d}*{now:%H%M}",
        "HL*1**20*1",
        "NM1*X3*2*LONE STAR HEALTH PLAN*****PI*LSHP01",
        "HL*2*1*21*1",
        f"NM1*1P*1*{requester_last}*{requester_first}****XX*{requester_npi}",
        "HL*3*2*22*1",
        f"NM1*IL*1*{last}*{first}****MI*{member_id}",
        f"DMG*D8*{dob:%Y%m%d}*{gender}",
        "HL*4*3*EV*1",
        f"TRN*1*PATRACE{control}*9{sender}",
        f"UM*HS*I*{service_type}*{place_of_service}:B",
        f"DTP*AAH*RD8*{start:%Y%m%d}-{end:%Y%m%d}",
        "HI*" + "*".join(f"{'ABK' if i == 0 else 'ABF'}:{dx}" for i, dx in enumerate(diagnoses)),
        f"NM1*SJ*2*{servicing_name}*****XX*{servicing_npi}",
    ]
    for n, (cpt, units, charge) in enumerate(procedures, start=5):
        body += [f"HL*{n}*4*SS*0", f"UM*HS*I*{service_type}", f"SV1*HC:{cpt}*{charge:.2f}*UN*{units}"]
    body.append(f"SE*{len(body) + 1}*0001")
    gs = f"GS*HI*{sender}*LSHP01*{now:%Y%m%d}*{now:%H%M}*{control}*X*{IG_VERSION}"
    return "~".join([_isa(sender, "LSHP01", control, now), gs, *body,
                     f"GE*1*{control}", f"IEA*1*{control.zfill(9)}"]) + "~"
