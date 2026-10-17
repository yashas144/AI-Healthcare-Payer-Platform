from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import x12_278
from app.auth import issue_token
from app.main import app
from app.models import Action, Decision

client = TestClient(app)
START, END = date(2026, 10, 20), date(2026, 12, 18)


def auth(scopes=("x12.prior-auth", "prior-auth.read")):
    return {"Authorization": f"Bearer {issue_token('test', list(scopes))}", "Content-Type": "text/plain"}


def mri_request(**overrides):
    args = dict(member_id="M100000001", last="DOE", first="JOHN", dob=date(1985, 4, 12), gender="M",
                requester_npi="1234567893", requester_last="PATEL", requester_first="ANITA",
                servicing_npi="1245319599", servicing_name="NORTH TEXAS IMAGING",
                diagnoses=["M5441", "M5416"], procedures=[("72148", 1, 1500.0)],
                start=START, end=END)
    args.update(overrides)
    return x12_278.build_278_request(**args)


def segs(edi: str) -> list[list[str]]:
    return [s.split("*") for s in edi.split("~") if s]


def first(edi: str, tag: str) -> list[str]:
    return next(s for s in segs(edi) if s[0] == tag)


# ---------- parsing ----------

def test_parse_mri_request():
    r = x12_278.parse_278(mri_request())
    assert r.member_id == "M100000001" and r.member_dob == date(1985, 4, 12)
    assert r.requester.npi == "1234567893" and r.requester.first_name == "ANITA"
    assert r.servicing_provider.npi == "1245319599"
    assert r.request_category == "HS" and r.certification_type == "I"
    assert r.service_type == "62" and r.place_of_service == "11"
    assert r.diagnoses == ["M5441", "M5416"]           # principal first
    assert (r.event_start, r.event_end) == (START, END)
    assert r.services[0].procedure_code == "72148" and r.services[0].units == 1
    assert r.trace_number == "PATRACE1"


def test_parse_multiple_service_lines():
    r = x12_278.parse_278(mri_request(procedures=[("72148", 1, 1500.0), ("72100", 2, 200.0)]))
    assert [(s.procedure_code, s.units) for s in r.services] == [("72148", 1), ("72100", 2)]


def test_isa_is_fixed_width():
    assert len(mri_request().split("~")[0]) + 1 == 106


@pytest.mark.parametrize("mutate, message", [
    (lambda e: e.replace("ST*278", "ST*270"), "278"),
    (lambda e: e.replace("BHT*0007*13", "BHT*0007*11"), "BHT02"),
    (lambda e: e.replace("SE*19*", "SE*99*"), "SE01"),
    (lambda e: e.replace("*005010X217", "*005010X279A1"), "005010X217"),
])
def test_structural_errors(mutate, message):
    with pytest.raises(x12_278.X12Error, match=message):
        x12_278.parse_278(mutate(mri_request()))


# ---------- validation ----------

def codes(edi_request: str) -> list[tuple[str, str]]:
    return [(r.loop, r.reason_code) for r in x12_278.validate(x12_278.parse_278(edi_request))]


def test_valid_request_has_no_errors():
    assert codes(mri_request()) == []


def test_invalid_icd10():
    assert ("EVENT", "15") in codes(mri_request(diagnoses=["M54.41"]))   # dot not allowed in X12


def test_invalid_cpt():
    assert ("EVENT", "15") in codes(mri_request(procedures=[("7214", 1, 100.0)]))


def test_hcpcs_accepted():
    assert codes(mri_request(procedures=[("E0601", 1, 900.0)])) == []


def test_end_before_start():
    assert ("EVENT", "57") in codes(mri_request(start=END, end=START))


def test_bad_requester_npi():
    assert ("REQUESTER", "15") in codes(mri_request(requester_npi="123"))


# ---------- response building ----------

def test_approved_response():
    req = x12_278.parse_278(mri_request())
    edi = x12_278.build_278_response(req, Decision(Action.CERTIFIED, "PA2610200001",
                                                   approved_start=START, approved_end=date(2026, 11, 19)))
    hcr = first(edi, "HCR")
    assert hcr[:3] == ["HCR", "A1", "PA2610200001"]
    assert first(edi, "REF") == ["REF", "BB", "PA2610200001"]
    assert first(edi, "DTP") == ["DTP", "AAH", "RD8", "20261020-20261119"]
    assert first(edi, "BHT")[2] == "11"                  # response
    ids = [s[0] for s in segs(edi)]
    assert int(segs(edi)[ids.index("SE")][1]) == ids.index("SE") - ids.index("ST") + 1


def test_modified_response_changes_units():
    req = x12_278.parse_278(mri_request(procedures=[("97110", 12, 80.0)], service_type="PT"))
    edi = x12_278.build_278_response(req, Decision(Action.MODIFIED, "PA1", approved_units=6))
    sv1 = first(edi, "SV1")
    assert sv1[1] == "HC:97110" and sv1[4] == "6"


def test_denied_response_has_no_approved_dates():
    req = x12_278.parse_278(mri_request())
    edi = x12_278.build_278_response(req, Decision(Action.NOT_CERTIFIED, "PA2", reason_code="CS1"))
    assert first(edi, "HCR")[:4] == ["HCR", "A3", "PA2", "CS1"]
    assert not any(s[0] == "DTP" for s in segs(edi))


def test_rejection_stops_at_failing_loop():
    req = x12_278.parse_278(mri_request(requester_npi="123"))
    edi = x12_278.build_278_response(req, None, x12_278.validate(req))
    assert first(edi, "AAA") == ["AAA", "N", "", "15", "C"]
    assert not any(s[0] == "NM1" and s[1] == "IL" for s in segs(edi))   # no subscriber loop after it


# ---------- API ----------

def test_submit_returns_pended_with_tracking_number():
    resp = client.post("/x12/278", content=mri_request(), headers=auth())
    assert resp.status_code == 200
    hcr = first(resp.text, "HCR")
    assert hcr[1] == "A4" and hcr[2].startswith("PA")
    status = client.get(f"/prior-auth/{hcr[2]}", headers=auth()).json()
    assert status["status"] == "PENDED" and status["services"][0]["procedure_code"] == "72148"


def test_submit_invalid_returns_aaa():
    resp = client.post("/x12/278", content=mri_request(diagnoses=["BAD"]), headers=auth())
    assert first(resp.text, "AAA")[3] == "15"


def test_malformed_returns_400():
    assert client.post("/x12/278", content="nonsense", headers=auth()).status_code == 400


def test_scope_required():
    assert client.post("/x12/278", content=mri_request(),
                       headers=auth(scopes=("prior-auth.read",))).status_code == 403
    assert client.post("/x12/278", content=mri_request(),
                       headers={"Content-Type": "text/plain"}).status_code == 401


def test_unknown_case_404():
    assert client.get("/prior-auth/PA000", headers=auth()).status_code == 404
