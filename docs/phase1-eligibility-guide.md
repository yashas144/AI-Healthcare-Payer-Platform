# Phase 1 Study Guide: Eligibility (270/271 and FHIR)

Use this to explain the code in interviews. Read it alongside `samples/`.

## 1. What eligibility answers

Before any service, a provider asks the payer: *Is this person covered on this date,
and what will they owe for this type of service?* The answer drives whether the visit
proceeds, what to collect at the front desk, and whether prior auth is needed.

## 2. The 270 request, line by line (`samples/270_active_member.edi`)

| Segment | Meaning |
|---|---|
| `ISA*...*ZZ*DENTONFM*ZZ*LSHP01*...*00501*000000001*0*T*:~` | Interchange envelope. Fixed width (106 chars). Sender, receiver, version 00501, control number, `T` = test. The 4th character defines the element separator, the last two define component and segment separators. |
| `GS*HS*...*005010X279A1~` | Functional group. `HS` = eligibility inquiry. `005010X279A1` is the implementation guide. |
| `ST*270*0001~` | Transaction set start. |
| `BHT*0022*13*REF1~` | `0022` = eligibility structure, `13` = request. `REF1` is the submitter's reference, echoed back. |
| `HL*1**20*1~` + `NM1*PR*...*PI*LSHP01~` | Level 20: information source (the **payer**). |
| `HL*2*1*21*1~` + `NM1*1P*...*XX*1234567893~` | Level 21: information receiver (the **provider**). `XX` = NPI. |
| `HL*3*2*22*0~` | Level 22: **subscriber**. Last element `0` = no child levels (no dependent loop). |
| `TRN*1*TRACE1~` | Trace number so the provider can match the response. |
| `NM1*IL*1*DOE*JOHN****MI*M100000001~` | `IL` = insured, `MI` = member ID. |
| `DMG*D8*19850412~` | Date of birth, `D8` = CCYYMMDD format. |
| `DTP*291*D8*20261012~` | `291` = date eligibility is being checked for. |
| `EQ*30~ EQ*98~ EQ*62~` | Service types asked about: 30 general plan, 98 office visit, 62 MRI/CT. |
| `SE*15*0001~` | Segment count from ST to SE inclusive. The parser rejects mismatches. |
| `GE` / `IEA` | Group and interchange trailers. |

Dependent note: when the patient is a dependent with their own ID, the 270 adds `HL*4*3*23*0`
and `NM1*03`. Many payers now issue unique IDs to dependents, so they are sent as subscribers.

## 3. The 271 response (`samples/271_active_member.edi`)

| Segment | Meaning |
|---|---|
| `BHT*0022*11` | `11` = response. |
| `TRN*2*TRACE1` | `2` = referenced trace, echoes the request. |
| `REF*6P*GRP1001` | Group number. |
| `DTP*346*D8*20260101` | Plan begin date (`347` = plan end). |
| `EB*1*IND*30*PR*Lone Star PPO Gold` | **Active coverage**. `IND` = individual, `PR` = PPO (`HM` = HMO). |
| `EB*B*IND*98*PR**27*25.00****N*Y` | **Copay** $25 per visit (`27`) for office visits. EB11 `N` = no auth. EB12 `Y` = in network. |
| `EB*A*...**0.20` | **Coinsurance** 20%. |
| `EB*C*...*29*900.00` | **Deductible**, `29` = remaining amount. |
| `EB*G*...*29*5100.00` | **Out-of-pocket max** remaining. |
| `EB*A*IND*62...***Y*Y` | MRI: EB11 `Y` = **prior authorization required**. This hands off to Phase 2. |
| `MSG*...` | Free-text message. |

EB01 codes to memorize: `1` active, `6` inactive, `A` coinsurance, `B` copay, `C` deductible, `G` out-of-pocket.

### Rejections

If the payer cannot answer, it returns `AAA*Y**75*C`:
- `Y` = the request was valid but could not be processed
- `75` = subscriber not found (`71` DOB mismatch, `73` name mismatch, `72` invalid member ID)
- `C` = correct and resubmit

A syntactically broken file gets a **999** acknowledgment instead (the service returns a 400 with the reason).

## 4. The FHIR equivalent

```
CoverageEligibilityRequest              CoverageEligibilityResponse
  patient  -> Patient/M100000001          outcome: complete | error
  provider -> NPI identifier              insurance[0].inforce: true      (EB*1)
  servicedDate                            insurance[0].item[]:
  item[].category -> X12 service type       category, network, benefit[]  (EB*A/B/C/G)
                                            authorizationRequired         (EB11)
                                          error[] -> AAA reason code
```

Endpoints: `POST /fhir/CoverageEligibilityRequest/$submit`, `GET /fhir/Patient/{id}`,
`GET /fhir/Coverage?beneficiary=Patient/{id}` (returns a search `Bundle`).

`Coverage.subscriberId` and `relationship` show how a dependent links to the policyholder.

## 5. Design decisions to talk about

1. **Adapters over one engine.** `x12.py` and `fhir.py` only translate; `eligibility.py` holds the rules.
   Real payers run X12 and FHIR side by side during the CMS-0057-F transition, so this matters.
2. **Demographic matching.** ID plus name and DOB must agree, or the request is rejected. Returning
   another member's benefits would be a HIPAA breach.
3. **Scopes, not just login.** Tokens carry SMART-style scopes; a client allowed to check eligibility
   cannot necessarily read Patient. In Phase 4 the token issuer becomes Entra ID with no endpoint changes.
4. **Audit without PHI.** Logs record who called what, with a correlation ID, never the payload.
5. **Out-of-network logic.** Coinsurance rises 20 points out of network, which is how most PPOs behave.

## 6. Practice questions

1. What does EB11 tell you, and why does it matter for prior auth?
2. A 271 comes back with `AAA*Y**72*C`. What happened and what should the provider do?
3. Why is the ISA segment fixed width?
4. How does the FHIR response show that an MRI needs authorization?
5. Why keep the X12 and FHIR code separate from eligibility rules?
6. What is the difference between a 999 and an AAA rejection?
