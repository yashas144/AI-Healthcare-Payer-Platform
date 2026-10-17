# Phase 2a Study Guide: X12 278 Prior Authorization

Read alongside `samples/278_request_mri.edi` and `samples/278_response_pended.edi`.

## 1. Where this fits

Phase 1's 271 told the provider that an MRI (service type 62) needs authorization (EB11 = `Y`).
The 278 is how the provider asks for it. The scenario in the samples:

- **Patient:** John Doe, member `M100000001`
- **Ordering doctor:** Dr. Anita Patel, NPI `1234567893`
- **Service:** lumbar spine MRI without contrast, **CPT 72148**
- **Why:** low back pain with sciatica, **ICD-10 M54.41** (principal) and radiculopathy **M54.16**
- **Where:** North Texas Imaging, NPI `1245319599`
- **When:** any time between Oct 20 and Dec 18, 2026

## 2. The 278 request, line by line

| Segment | Meaning |
|---|---|
| `GS*HI*...*005010X217` | Functional group `HI` = health care services review. `005010X217` is the 278 implementation guide. |
| `ST*278*0001*005010X217` | Transaction set 278. |
| `BHT*0007*13*PA1` | `0007` = 278 structure, `13` = request. `PA1` is the submitter's reference. |
| `HL*1**20*1` + `NM1*X3*...*PI*LSHP01` | Level 20: **UMO**, the payer's utilization management organization. `X3` = UMO. |
| `HL*2*1*21*1` + `NM1*1P*1*PATEL*ANITA****XX*1234567893` | Level 21: **requester**. `1` = person (vs `2` = organization), `XX` = NPI. |
| `HL*3*2*22*1` + `NM1*IL...` + `DMG` | Level 22: **subscriber**, with date of birth and gender. |
| `HL*4*3*EV*1` | Level EV: **patient event**, which carries the clinical "why". |
| `TRN*1*PATRACE1*9DENTONFM` | Provider's trace number, echoed back as `TRN*2`. |
| `UM*HS*I*62*11:B` | **UM01** `HS` = health services review (outpatient). `AR` would be an inpatient admission, `SC` a specialist referral. **UM02** `I` = initial request (vs appeal, extension). **UM03** `62` = MRI/CT service type. **UM04** `11` = place of service: office. |
| `DTP*AAH*RD8*20261020-20261218` | `AAH` = event date. `RD8` = a date range. |
| `HI*ABK:M5441*ABF:M5416` | Diagnoses. `ABK` = **principal** ICD-10, `ABF` = other ICD-10. **No decimal point in X12**: M54.41 is sent as `M5441`. |
| `NM1*SJ*2*NORTH TEXAS IMAGING*****XX*1245319599` | `SJ` = **servicing provider**, who performs the service. |
| `HL*5*4*SS*0` | Level SS: **service**. The final `0` means no children. |
| `SV1*HC:72148*1500.00*UN*1` | Professional service. `HC` = CPT/HCPCS, procedure `72148`, charge $1,500, `UN` = units, quantity 1. |

The `HL` segment's four parts are: its own ID, its parent's ID, its level code, and whether it has children.
`HL*5*4*SS*0` reads as "I am #5, my parent is #4 (the event), I am a service, I have no children."

## 3. The 278 response

The payer echoes the same hierarchy with `BHT*0007*11` (`11` = response) and adds the decision.

| Segment | Meaning |
|---|---|
| `HCR*A4*PA26101600001` | **Decision.** HCR01 = action code, HCR02 = the payer's **review/authorization number**. |
| `REF*BB*PA26101600001` | `BB` = authorization number. The provider must put this on the claim (837). |
| `DTP*AAH*RD8*...` | On approvals: the dates the authorization is valid. Often narrower than requested. |
| `MSG*...` | Free text. |
| `SV1` at the service level | Echoes the service, with **approved** units if they changed. |

HCR01 action codes to memorize:

| Code | Meaning | Example |
|---|---|---|
| `A1` | Certified in total | MRI approved |
| `A2` | Certified, partial | 2 of 3 procedures approved |
| `A3` | Not certified | Denied, with reason in HCR03 |
| `A4` | **Pended** | Received, needs review or more information |
| `A6` | Modified | 12 PT visits requested, 6 approved |
| `C` | Cancelled | Provider withdrew the request |

The approved sample (`278_response_approved.edi`) shows an `A1` with a 30-day validity window,
narrower than the 60 days requested. That is typical: authorizations expire.

## 4. Three kinds of "no"

Know the difference; interviewers ask.

1. **999 rejection:** the file is broken (bad envelope, wrong segment count, wrong version).
   Nobody looked at the content. The service returns HTTP 400.
2. **AAA rejection:** the file is valid, but the request can't be processed. Missing NPI,
   invalid ICD-10, end date before start date. `AAA*N**15*C` = not valid, reason 15 (required data
   missing or invalid), correct and resubmit. The response stops at the loop that failed.
3. **HCR*A3 denial:** the request was processed and reviewed clinically, and the service was found
   not medically necessary. Only a physician reviewer can issue this, and it comes with appeal rights.

## 5. What this branch does

```
278 in ─▶ parse_278 ─▶ validate ─┬─ errors ─▶ 278 with AAA
                                 └─ ok ─────▶ store case, 278 with HCR*A4 + tracking number
```

- `parse_278()` walks the HL hierarchy and builds a `PriorAuthRequest`.
- `validate()` runs intake checks a real payer runs before clinical review: NPI format, member fields,
  ICD-10 and CPT/HCPCS format, date logic, service lines present for outpatient requests.
- `build_278_response()` can produce **any** decision (A1, A3, A4, A6) or AAA rejections. The API
  currently always pends valid requests; the next branch adds the rules engine that decides.
- `GET /prior-auth/{tracking}` is the status lookup, like a provider checking the payer portal.

## 6. Design decisions to talk about

1. **Intake vs. clinical review are separate steps.** Validation catches bad data cheaply; the rules
   engine and nurse review only see clean requests.
2. **Response builder is decision-agnostic.** It takes a `Decision` object, so the rules engine,
   a nurse, or the AI copilot can all produce decisions without touching X12 code.
3. **The authorization number links prior auth to claims.** Without `REF*BB` on the 837, the claim denies.
4. **Rejection stops at the failing loop.** If the requester is invalid, the response doesn't echo
   member data back. That's also good PHI hygiene.

## 7. Practice questions

1. What's the difference between UM01 `HS` and `AR`?
2. Why is M54.41 sent as `M5441`?
3. What does `HL*5*4*SS*0` mean, element by element?
4. A response has `HCR*A6`. What happened, and where would you see the change?
5. Compare a 999, an `AAA*N**15*C`, and an `HCR*A3`.
6. Why must the claim carry the number from `REF*BB`?
