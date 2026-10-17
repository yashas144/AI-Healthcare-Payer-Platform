# AI-Powered Healthcare Payer & Prior Authorization Platform

A payer-side platform that models Eligibility, Benefits, Prior Authorization, and Claims,
with an AI copilot for prior auth review. Built with Python/FastAPI, X12 EDI, FHIR R4, and Azure.

> **All data is synthetic.** No real PHI is used anywhere in this repository.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Eligibility service: X12 270/271 + FHIR R4 `CoverageEligibilityRequest/Response`, JWT scopes, audit logging, tests | ✅ Done |
| 2 | Prior auth service: X12 278 + Da Vinci PAS (FHIR `Claim`), rules engine, pend/approve workflow | 🚧 In progress: 278 intake done |
| 3 | Prior Auth Copilot: Azure OpenAI, Azure AI Search (RAG over payer policies), missing-info agent, citations | |
| 4 | Entra ID OAuth/OIDC, Azure Service Bus, Terraform, GitHub Actions CI/CD, Application Insights | |
| 5 | React provider/reviewer portal, claims (837/835), evaluation metrics | |

## Architecture (Phase 1)

```
 Provider system ──X12 270──▶ ┌────────────────────────┐
                 ◀──X12 271── │  Eligibility Service    │
                              │  x12.py   (adapter)     │
 FHIR client ──CoverageElig── │  fhir.py  (adapter)     │──▶ eligibility.py ──▶ data.py
             ◀──Request/Resp─ │  auth.py  (JWT scopes)  │      (one engine)     (synthetic)
                              └────────────────────────┘
```

**Key design decision:** X12 and FHIR are two adapters over one format-independent
eligibility engine. Adding a new format never touches business rules.

## Run it

```bash
cp .env.example .env            # set JWT_SECRET
docker compose up --build       # API at http://localhost:8001/docs
```

Or without Docker:

```bash
cd services/eligibility
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8001
pytest -q
```

## Try it

```bash
# 1. Get a dev token
TOKEN=$(curl -s -X POST localhost:8001/auth/token -H "Content-Type: application/json" \
  -d '{"client_id":"demo"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 2. X12: send a 270, receive a 271
curl -X POST localhost:8001/x12/270 -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: text/plain" --data-binary @samples/270_active_member.edi

# 3. X12: submit a 278 prior auth request (prior-auth service, port 8002)
TOKEN2=$(curl -s -X POST localhost:8002/auth/token -H "Content-Type: application/json" \
  -d '{"client_id":"demo"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -X POST localhost:8002/x12/278 -H "Authorization: Bearer $TOKEN2" \
  -H "Content-Type: text/plain" --data-binary @samples/278_request_mri.edi

# 4. FHIR: patient and coverage
curl localhost:8001/fhir/Patient/M100000001 -H "Authorization: Bearer $TOKEN"
curl "localhost:8001/fhir/Coverage?beneficiary=Patient/M100000002" -H "Authorization: Bearer $TOKEN"
```

## Synthetic test members

| Member ID | Name | Plan | Scenario |
|---|---|---|---|
| M100000001 | John Doe | PPO Gold | Active subscriber |
| M100000002 | Jane Doe | PPO Gold | Active dependent (spouse) |
| M200000001 | Maria Garcia | HMO Silver | Active, deductible fully met |
| M300000001 | Robert Smith | PPO Gold | Terminated 2026-06-30 → inactive |

Providers: `1234567893` and `1245319599` in-network, `1003000126` out-of-network.

## Security and compliance notes

- Synthetic data only; `.env` is git-ignored.
- Every endpoint requires a JWT with a SMART-style scope (`system/Patient.read`).
- Audit log records correlation ID, path, and status, never PHI (HIPAA minimum necessary).
- Member lookups verify last name and DOB to prevent returning the wrong person's data.
- Container runs as a non-root user.

Study guides:
- [`docs/phase1-eligibility-guide.md`](docs/phase1-eligibility-guide.md): 270/271 and FHIR eligibility
- [`docs/phase2a-278-guide.md`](docs/phase2a-278-guide.md): 278 prior authorization
