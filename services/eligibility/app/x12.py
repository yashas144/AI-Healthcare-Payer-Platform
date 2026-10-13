"""X12 005010X279A1 (270/271 eligibility) parser and generator.

Covers the segments a real payer integration actually depends on.
Not a full implementation guide validator: production systems use an
EDI translator (Edifecs, Availity, etc.) in front of services like this one.

Structure of a 270:
  ISA / GS / ST          interchange, functional group, transaction envelopes
  BHT                    beginning of hierarchical transaction (purpose, ref id)
  HL*1**20               information source  (payer)        NM1*PR
  HL*2*1*21              information receiver (provider)    NM1*1P  (NPI in NM109)
  HL*3*2*22              subscriber                         TRN, NM1*IL, DMG, DTP, EQ
  SE / GE / IEA          trailers carrying control numbers and segment counts
"""
from dataclasses import dataclass
from datetime import date, datetime

from .data import PAYER
from .eligibility import EligibilityRequest, EligibilityResult


class X12Error(ValueError):
    pass


@dataclass
class Delimiters:
    element: str = "*"
    component: str = ":"
    segment: str = "~"


@dataclass
class Parsed270:
    request: EligibilityRequest
    sender_id: str
    receiver_id: str
    interchange_control: str
    group_control: str
    bht_reference: str
    trace_number: str | None
    provider_name: str | None
    subscriber_first: str | None


def _d8(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def parse_segments(raw: str) -> tuple[list[list[str]], Delimiters]:
    raw = raw.strip().replace("\r", "").replace("\n", "")
    if not raw.startswith("ISA") or len(raw) < 106:
        raise X12Error("Interchange must start with a 106-character ISA segment")
    # ISA is fixed width, so delimiters are read from fixed positions
    d = Delimiters(element=raw[3], component=raw[104], segment=raw[105])
    segs = [s.split(d.element) for s in raw.split(d.segment) if s.strip()]
    return segs, d


def parse_270(raw: str) -> Parsed270:
    segs, _ = parse_segments(raw)
    ids = [s[0] for s in segs]
    for required in ("ISA", "GS", "ST", "BHT", "HL", "NM1", "SE", "GE", "IEA"):
        if required not in ids:
            raise X12Error(f"Missing required segment {required}")

    isa = segs[0]
    gs = next(s for s in segs if s[0] == "GS")
    st = next(s for s in segs if s[0] == "ST")
    if st[1] != "270":
        raise X12Error(f"Expected transaction set 270, got {st[1]}")
    bht = next(s for s in segs if s[0] == "BHT")

    # SE01 = number of segments from ST to SE inclusive
    st_i, se_i = ids.index("ST"), ids.index("SE")
    if int(segs[se_i][1]) != se_i - st_i + 1:
        raise X12Error(f"SE01 count {segs[se_i][1]} != actual {se_i - st_i + 1}")

    hl_level = None
    member_id = last = first = provider_npi = provider_name = trace = None
    dob = None
    service_date = date.today()
    service_types: list[str] = []

    for s in segs:
        tag = s[0]
        if tag == "HL":
            hl_level = s[3]            # 20 payer, 21 provider, 22 subscriber, 23 dependent
        elif tag == "NM1" and s[1] == "1P":
            provider_name = s[3]
            provider_npi = s[9] if len(s) > 9 and s[8] == "XX" else None
        elif tag == "NM1" and s[1] in ("IL", "03") and hl_level in ("22", "23"):
            last = s[3]
            first = s[4] if len(s) > 4 else None
            if len(s) > 9 and s[8] == "MI":
                member_id = s[9]
        elif tag == "TRN" and hl_level in ("22", "23"):
            trace = s[2]
        elif tag == "DMG" and s[1] == "D8":
            dob = _d8(s[2])
        elif tag == "DTP" and s[1] == "291":     # 291 = plan / eligibility date
            service_date = _d8(s[3][:8])
        elif tag == "EQ":
            service_types.append(s[1])

    if not member_id:
        raise X12Error("Subscriber NM1*IL with MI member ID is required")

    return Parsed270(
        request=EligibilityRequest(member_id, last, dob, provider_npi, service_date,
                                   service_types or ["30"]),
        sender_id=isa[6].strip(), receiver_id=isa[8].strip(),
        interchange_control=isa[13], group_control=gs[6],
        bht_reference=bht[3], trace_number=trace,
        provider_name=provider_name, subscriber_first=first,
    )


def _isa(sender: str, receiver: str, control: str, now: datetime, d: Delimiters) -> str:
    e = d.element
    return e.join([
        "ISA", "00", " " * 10, "00", " " * 10,
        "ZZ", sender.ljust(15)[:15], "ZZ", receiver.ljust(15)[:15],
        now.strftime("%y%m%d"), now.strftime("%H%M"), "^", "00501",
        control.zfill(9), "0", "T", d.component,
    ])


def build_271(p: Parsed270, r: EligibilityResult, now: datetime | None = None) -> str:
    now = now or datetime.now()
    d = Delimiters()
    e = d.element
    body: list[str] = []

    def seg(*parts):
        body.append(e.join(str(x) for x in parts).rstrip(e))

    seg("ST", "271", "0001", "005010X279A1")
    seg("BHT", "0022", "11", p.bht_reference, now.strftime("%Y%m%d"), now.strftime("%H%M"))
    seg("HL", "1", "", "20", "1")
    seg("NM1", "PR", "2", PAYER["name"], "", "", "", "", "PI", PAYER["id"])
    seg("HL", "2", "1", "21", "1")
    seg("NM1", "1P", "2", p.provider_name or "", "", "", "", "", "XX", p.request.provider_npi or "")
    seg("HL", "3", "2", "22", "0")
    if p.trace_number:
        seg("TRN", "2", p.trace_number, "9" + PAYER["id"])   # TRN*2 echoes the submitter trace

    req = p.request
    if not r.found:
        seg("NM1", "IL", "1", req.last_name or "", p.subscriber_first or "", "", "", "", "MI", req.member_id)
        # AAA: Y = request valid but rejected, reason code, C = correct and resubmit
        seg("AAA", "Y", "", r.reject_reason, "C")
    else:
        m, plan = r.member, r.plan
        seg("NM1", "IL", "1", m.last_name, m.first_name, "", "", "", "MI", m.member_id)
        seg("REF", "6P", plan.group_number)            # 6P = group number
        seg("DMG", "D8", m.dob.strftime("%Y%m%d"), m.gender)
        seg("DTP", "346", "D8", m.coverage_start.strftime("%Y%m%d"))  # plan begin
        if m.coverage_end:
            seg("DTP", "347", "D8", m.coverage_end.strftime("%Y%m%d"))  # plan end
        ins_type = "HM" if plan.plan_type == "HMO" else "PR"

        if not r.active:
            seg("EB", "6", "IND", "30", ins_type, plan.name)   # 6 = inactive
        else:
            seg("EB", "1", "IND", "30", ins_type, plan.name)   # 1 = active coverage
            for b in r.benefits:
                net = "Y" if b.in_network else "N"
                auth = "Y" if b.auth_required else "N"
                # EB layout: 01 info code, 02 coverage level, 03 service type, 04 insurance type,
                # 05 plan desc, 06 time period, 07 amount, 08 percent, 09-10 quantity,
                # 11 authorization required, 12 in-plan network
                if b.copay is not None:
                    seg("EB", "B", "IND", b.service_type, ins_type, "", "27", f"{b.copay:.2f}", "", "", "", auth, net)
                if b.coinsurance_pct is not None:
                    seg("EB", "A", "IND", b.service_type, ins_type, "", "27", "", f"{b.coinsurance_pct / 100:.2f}", "", "", auth, net)
                seg("EB", "C", "IND", b.service_type, ins_type, "", "29", f"{b.deductible_remaining:.2f}", "", "", "", auth, net)
                seg("EB", "G", "IND", b.service_type, ins_type, "", "29", f"{b.oop_remaining:.2f}", "", "", "", auth, net)
                if b.auth_required:
                    seg("MSG", "PRIOR AUTHORIZATION REQUIRED FOR SERVICE TYPE " + b.service_type)

    seg("SE", str(len(body) + 1), "0001")

    gs = e.join(["GS", "HB", PAYER["id"], p.sender_id, now.strftime("%Y%m%d"),
                 now.strftime("%H%M"), p.group_control, "X", "005010X279A1"])
    trailer = [e.join(["GE", "1", p.group_control]), e.join(["IEA", "1", p.interchange_control.zfill(9)])]
    isa = _isa(PAYER["id"], p.sender_id, p.interchange_control, now, d)
    return d.segment.join([isa, gs, *body, *trailer]) + d.segment


def build_270(member_id: str, last: str, first: str, dob: date, npi: str,
              provider_name: str, service_date: date, service_types: list[str],
              sender: str = "DENTONFM", control: str = "1") -> str:
    """Generate a 270 (used by tests and the demo client, mirrors what a provider sends)."""
    now = datetime.now()
    d = Delimiters()
    e = d.element
    body = [
        f"ST{e}270{e}0001{e}005010X279A1",
        f"BHT{e}0022{e}13{e}REF{control}{e}{now:%Y%m%d}{e}{now:%H%M}",
        f"HL{e}1{e}{e}20{e}1",
        f"NM1{e}PR{e}2{e}{PAYER['name']}{e}{e}{e}{e}{e}PI{e}{PAYER['id']}",
        f"HL{e}2{e}1{e}21{e}1",
        f"NM1{e}1P{e}2{e}{provider_name}{e}{e}{e}{e}{e}XX{e}{npi}",
        f"HL{e}3{e}2{e}22{e}0",
        f"TRN{e}1{e}TRACE{control}{e}9{sender}",
        f"NM1{e}IL{e}1{e}{last}{e}{first}{e}{e}{e}{e}MI{e}{member_id}",
        f"DMG{e}D8{e}{dob:%Y%m%d}",
        f"DTP{e}291{e}D8{e}{service_date:%Y%m%d}",
        *[f"EQ{e}{st}" for st in service_types],
    ]
    body.append(f"SE{e}{len(body) + 1}{e}0001")
    gs = e.join(["GS", "HS", sender, PAYER["id"], f"{now:%Y%m%d}", f"{now:%H%M}", control, "X", "005010X279A1"])
    return d.segment.join([_isa(sender, PAYER["id"], control, now, d), gs, *body,
                           f"GE{e}1{e}{control}", f"IEA{e}1{e}{control.zfill(9)}"]) + d.segment
