# Revenue Recovery Agent

Built for the **Razorpay AI Buildathon — Track 03: AI Revenue Recovery**.

Detects at-risk revenue (failed payments) and runs a bounded, auditable recovery
workflow: **detect → diagnose → decide → act → stop**.

---

## The two design commitments

**1. Restraint is a measured result, not a missing feature.**

Three failure causes are hard-gated — the agent may never retry or re-contact
them:

| Cause | Why it's gated |
|---|---|
| `fraud_suspected` | Retrying is a compliance problem, not an opportunity |
| `card_blocked` | The issuer has already said no; asking again is harassment |
| `customer_cancelled` | The customer expressed intent — respect it |

That gate lives in [app/models.py](app/models.py) as `UNRECOVERABLE_REASONS` and
is enforced by deterministic code, **not** by asking an LLM to behave. The batch
report treats "₹ deliberately not chased" as a headline figure alongside
"₹ recovered".

A fourth path exists for causes outside the known taxonomy: they classify as
`UNKNOWN` and escalate to a human. The agent does not guess an action for a
cause it doesn't recognise.

**2. Verified rupees and modeled rupees are never summed.**

Every recovery action creates a **real Razorpay test-mode Payment Link**. A
handful are paid by hand with a test card to prove the loop genuinely closes;
the rest resolve through a seeded, explicitly-labeled outcome model. Every
outcome carries a `SettlementSource` (`verified_api` / `modeled` / `none`), and
the metrics report keeps them in **separate columns**. A single blended
"₹ recovered" number would be the easiest thing in this project to disbelieve.

---

## Project structure

```
app/
  main.py                    FastAPI endpoints
  config.py                  Settings loaded from .env
  models.py                  Domain models + the UNRECOVERABLE_REASONS gate
  detect.py                  Detect stage: normalize, validate, reject, aggregate
  services/
    razorpay_client.py       Every Razorpay call lives here — nothing else touches the SDK
scripts/
  generate_synthetic_data.py Seeded synthetic batch (clean + deliberately dirty rows)
tests/
  test_detect.py             Detect-stage unit tests, incl. the gate invariants
  test_api.py                End-to-end API tests over the real batch
data/
  failed_payments.csv        Generated batch
```

### Why money is integer paise

`amount_paise: int` is the canonical value everywhere; `amount_inr` is a derived,
display-only property. Razorpay's API speaks paise, and integer arithmetic means
the ₹ totals in the final report are exact rather than float-drifted. There's a
test that accumulates 1000 × ₹0.10 and asserts the total is exactly ₹100.00.

### Why the detect stage is so defensive

Real recovery data is dirty, and a row silently dropped is money silently
vanishing from the metrics. So every row is normalized and validated
independently:

- **Rejected** (hard data faults, reported not dropped): missing `payment_id`,
  duplicate `payment_id`, unparseable/non-positive amount, unparseable
  `created_at`.
- **Accepted with warnings** (carried into the audit trail): unmappable cause,
  no usable contact channel, future timestamps (clamped), negative retry count,
  non-INR currency.

`cases + rejected == rows_read` is asserted as a test invariant.

The generator injects eight deliberate dirty rows precisely so this path is
demonstrable rather than theoretical — see `DIRTY_ROW_BUILDERS` in
[scripts/generate_synthetic_data.py](scripts/generate_synthetic_data.py), where
each one is annotated with what it's meant to prove.

---

## Setup

1. **Virtual environment and dependencies**
   ```bash
   python -m venv venv
   source venv/bin/activate        # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Razorpay TEST MODE keys**
   - Log in at https://dashboard.razorpay.com with the **Test Mode** toggle ON
   - Settings → API Keys → Generate Test Key

3. **Anthropic API key** — for the diagnose stage (Day 3)
   - https://console.anthropic.com/settings/keys

4. **Configure environment**
   ```bash
   cp .env.example .env      # then paste your keys in
   ```

5. **Generate the batch** (seeded — same seed, same batch, every time)
   ```bash
   python scripts/generate_synthetic_data.py
   ```

6. **Run**
   ```bash
   uvicorn app.main:app --reload
   ```
   Interactive docs at http://127.0.0.1:8000/docs

7. **Test**
   ```bash
   python -m pytest -q
   ```

---

## Endpoints

| Endpoint | What it shows |
|---|---|
| `GET /health` | Liveness + whether the batch file is present |
| `GET /detect/run` | Full detect output: cases, rejects, summary |
| `GET /detect/summary` | Aggregates — ₹ at risk split by recoverability |
| `GET /detect/rejected` | Rows that couldn't be ingested, each with a reason |
| `GET /detect/unrecoverable` | The cases the agent refuses to touch |
| `GET /payments` | Normalized at-risk batch |
| `GET /payments/summary` | Counts and ₹ grouped by failure reason |
| `GET /payments/{payment_id}` | One case |

`/agent/*`, `/audit` and `/metrics` land in Days 3–6.

---

## Build plan

- [x] **Day 1** — Scaffold, synthetic dataset, FastAPI skeleton
- [x] **Day 2** — Detect stage: normalize / validate / reject / aggregate, seeded
      reproducible batch, recoverability gate + tests
- [ ] **Day 3** — Diagnose & decide: LLM root-cause classification behind the
      deterministic gate, cause → bounded action mapping
- [ ] **Day 4** — Act: wire decisions to real Razorpay test-mode calls
- [ ] **Day 5** — Audit trail (append-only, every decision explained)
- [ ] **Day 6** — Full batch run + metrics report
- [ ] **Day 7–8** — Architecture diagram, README polish, 5-min pitch video

## Metrics the batch report will carry (Day 6)

- **₹ recovered (verified)** — Razorpay test-mode API confirmed the link was paid
- **₹ recovered (modeled)** — resolved by the seeded outcome model, reported separately
- **₹ deliberately not chased** — the gated cases, counted as a result
- **Recovery rate** over recoverable cases only
- **Correctly-stopped count** — must equal 100% of gated causes
- **Escalations** — unknown causes, uncontactable customers, retry-cap hits
- **Unresolved exceptions** — rejected rows and failed API calls, itemised

## Scope

Of the four leak types in the brief (failed payments, cart abandonment, failed
subscriptions, overdue invoices), this build implements **failed payments**
thoroughly. The detect → diagnose → decide → act → stop spine is leak-type
agnostic — `LeakType` is tagged on every case from ingestion onward — so the
other three plug in by supplying a cause taxonomy and an action map. Depth over
breadth was a deliberate choice for a one-person build.
