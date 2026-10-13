from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import x12
from app.auth import issue_token
from app.eligibility import EligibilityRequest, check_eligibility
from app.main import app

client = TestClient(app)
SVC_DATE = date(2026, 10, 12)
ALL_SCOPES = ["x12.eligibility", "system/CoverageEligibilityRequest.write",
              "system/Patient.read", "system/Coverage.read"]


def auth(scopes=ALL_SCOPES):
    return {"Authorization": f"Bearer {issue_token('test-client', scopes)}"}


def make_270(member="M100000001", last="DOE", first="JOHN", dob=date(1985, 4, 12),
             npi="1234567893", types=("30", "98", "62")):
    return x12.build_270(member, last, first, dob, npi, "DENTON FAMILY MEDICINE", SVC_DATE, list(types))


def segments(edi: str) -> list[list[str]]:
    return [s.split("*") for s in edi.split("~") if s]


# ---------- engine ----------

def test_active_member_gets_benefits():
    r = check_eligibility(EligibilityRequest("M100000001", "DOE", date(1985, 4, 12),
                                             "1234567893", SVC_DATE, ["98", "62"]))
    assert r.found and r.active
    office, mri = r.benefits
    assert office.copay == 25 and not office.auth_required
    assert mri.auth_required
    assert office.deductible_remaining == 900


def test_terminated_member_is_inactive():
    r = check_eligibility(EligibilityRequest("M300000001", service_date=SVC_DATE))
    assert r.found and not r.active and r.benefits == []


def test_unknown_member_rejected_75():
    assert check_eligibility(EligibilityRequest("NOPE")).reject_reason == "75"


def test_dob_mismatch_rejected_71():
    r = check_eligibility(EligibilityRequest("M100000001", dob=date(2000, 1, 1)))
    assert not r.found and r.reject_reason == "71"


def test_out_of_network_raises_coinsurance():
    r = check_eligibility(EligibilityRequest("M100000001", provider_npi="1003000126",
                                             service_date=SVC_DATE, service_types=["62"]))
    assert r.benefits[0].in_network is False and r.benefits[0].coinsurance_pct == 40


# ---------- X12 ----------

def test_270_isa_is_106_chars():
    assert len(make_270().split("~")[0]) + 1 == 106


def test_parse_270():
    p = x12.parse_270(make_270())
    assert p.request.member_id == "M100000001"
    assert p.request.provider_npi == "1234567893"
    assert p.request.service_types == ["30", "98", "62"]
    assert p.request.dob == date(1985, 4, 12)


def test_bad_segment_count_rejected():
    bad = make_270().replace("SE*15*0001", "SE*99*0001")
    with pytest.raises(x12.X12Error, match="SE01"):
        x12.parse_270(bad)


def test_271_active_with_auth_flag():
    resp = client.post("/x12/270", content=make_270(), headers={**auth(), "Content-Type": "text/plain"})
    assert resp.status_code == 200
    segs = segments(resp.text)
    assert segs[0][0] == "ISA" and len(resp.text.split("~")[0]) + 1 == 106
    st = next(s for s in segs if s[0] == "ST")
    assert st[1] == "271"
    ebs = [s for s in segs if s[0] == "EB"]
    assert ebs[0][:4] == ["EB", "1", "IND", "30"]
    copay = next(s for s in ebs if s[1] == "B" and s[3] == "98")
    assert copay[7] == "25.00"
    mri = [s for s in ebs if s[3] == "62"]
    assert all(s[11] == "Y" for s in mri)          # EB11 authorization required
    # SE01 must equal segments ST..SE inclusive
    ids = [s[0] for s in segs]
    assert int(segs[ids.index("SE")][1]) == ids.index("SE") - ids.index("ST") + 1


def test_271_reject_has_aaa():
    resp = client.post("/x12/270", content=make_270(member="M999"),
                       headers={**auth(), "Content-Type": "text/plain"})
    aaa = next(s for s in segments(resp.text) if s[0] == "AAA")
    assert aaa == ["AAA", "Y", "", "75", "C"]


def test_271_inactive_eb6():
    resp = client.post("/x12/270", content=make_270(member="M300000001", last="SMITH", first="ROBERT",
                                                   dob=date(1960, 11, 5)),
                       headers={**auth(), "Content-Type": "text/plain"})
    ebs = [s for s in segments(resp.text) if s[0] == "EB"]
    assert ebs[0][1] == "6" and len(ebs) == 1


def test_malformed_x12_returns_400():
    resp = client.post("/x12/270", content="hello", headers={**auth(), "Content-Type": "text/plain"})
    assert resp.status_code == 400


# ---------- FHIR ----------

def fhir_request(member="M100000001", codes=("98", "62")):
    return {
        "resourceType": "CoverageEligibilityRequest", "id": "req-1", "status": "active",
        "purpose": ["benefits"], "patient": {"reference": f"Patient/{member}"},
        "servicedDate": SVC_DATE.isoformat(), "created": "2026-10-12",
        "provider": {"identifier": {"system": "http://hl7.org/fhir/sid/us-npi", "value": "1234567893"}},
        "insurer": {"display": "LONE STAR HEALTH PLAN"},
        "item": [{"category": {"coding": [{"system": "https://x12.org/codes/service-type-codes", "code": c}]}}
                 for c in codes],
    }


def test_fhir_eligibility_active():
    resp = client.post("/fhir/CoverageEligibilityRequest/$submit", json=fhir_request(), headers=auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["resourceType"] == "CoverageEligibilityResponse" and body["outcome"] == "complete"
    ins = body["insurance"][0]
    assert ins["inforce"] is True
    by_code = {i["category"]["coding"][0]["code"]: i for i in ins["item"]}
    assert by_code["62"]["authorizationRequired"] is True
    assert by_code["98"]["authorizationRequired"] is False


def test_fhir_unknown_member_error():
    body = client.post("/fhir/CoverageEligibilityRequest/$submit",
                       json=fhir_request(member="NOPE"), headers=auth()).json()
    assert body["outcome"] == "error" and body["error"][0]["code"]["coding"][0]["code"] == "75"


def test_fhir_wrong_resource_type():
    resp = client.post("/fhir/CoverageEligibilityRequest/$submit",
                       json={"resourceType": "Patient"}, headers=auth())
    assert resp.status_code == 400 and resp.json()["resourceType"] == "OperationOutcome"


def test_fhir_patient_and_coverage():
    p = client.get("/fhir/Patient/M100000002", headers=auth()).json()
    assert p["birthDate"] == "1987-09-03"
    bundle = client.get("/fhir/Coverage?beneficiary=Patient/M100000002", headers=auth()).json()
    cov = bundle["entry"][0]["resource"]
    assert cov["subscriberId"] == "M100000001"          # dependent points to subscriber
    assert cov["relationship"]["coding"][0]["code"] == "spouse"


# ---------- auth ----------

def test_missing_token_401():
    assert client.get("/fhir/Patient/M100000001").status_code == 401


def test_missing_scope_403():
    assert client.get("/fhir/Patient/M100000001", headers=auth(["x12.eligibility"])).status_code == 403


def test_correlation_id_echoed():
    r = client.get("/health", headers={"x-correlation-id": "abc"})
    assert r.headers["x-correlation-id"] == "abc"
