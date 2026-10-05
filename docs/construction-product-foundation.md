# Construction operations product: foundation proposal

Design date: 2026-10-05. This document is a proposed business model and acceptance
plan. Only the workforce row preview is implemented in this increment.
All examples are synthetic; production files and personal data stay outside Git.

## Product outcome and pilot

An operator records a person, their engagement and dated site assignment once;
approved time and supporting documents feed a traceable cost view. A manager can
see who worked, for whom, under which agreement, the amount accrued and paid, and
which underlying facts still need clarification.

Pilot scope: one site, a small reviewed group of people, one confirmed agreement,
one reporting period and one document output. Establish participant and contract
records before posting time. Start with reviewable drafts and preserve approved
source templates. Meetings is an existing supporting domain, not evidence that
construction accounting is implemented.

## Essential distinctions

| Entity | Meaning and relationships |
|---|---|
| Workspace/tenant | Existing `ecosystem.organizations` access boundary; not automatically a legal company |
| Participant | Legal entity, sole proprietor or individual; stable ID, typed identifiers, evidence and verification state |
| Person | Human identity; can link to an individual participant without duplicating identity data |
| Contract | Agreement with N parties; signed/draft status, versions and supporting documents |
| Contract party / role | Participant membership in a contract; several roles may coexist and change by contract |
| Contract site | Many-to-many relationship; do not assume one agreement equals one site |
| Obligation | Responsible party, beneficiary, basis, amount/unit, deadlines and fulfillment |
| Engagement | Dated relationship between a person and the party engaging them; basis and terms |
| Assignment | Dated operational placement under an engagement on a site; history is retained |
| Time entry | Person, engagement, assignment/site, business date, duration, type and source; split sites per day supported |
| Rate version | Unit, currency, validity interval, source and calculation rule; no unexplained numeric rate |
| Accrual | Versioned amount owed by a specified party to another party/person, tied to basis and site |
| Payment / allocation | Actual payer, payee, date, amount and allocation to obligations; payer may differ from debtor |
| Document version | Template version, issuer/recipient roles, contract/site, source facts and review/signature state |
| Source / assertion | File hash/version, sheet/cell/page and verification history; unknown does not become confirmed |

Multiple legal companies can be represented inside one authorized workspace.
Tenant RLS alone does not implement finer company/document visibility; add
participant-level permissions before restricted company records are served.
Every database relationship must retain the tenant, and access must be checked
against all referenced parties, contracts, people and sites.

## Business invariants

- A person appearing in an access permit does not prove attendance or employment.
- Do not merge abbreviated names automatically or assign IDs from mutable names.
- Moving a person to another site adds a dated assignment; it does not rewrite history.
- A future plan, actual time and absence are separate records. Time is integer
  minutes in normalized data; amounts use fixed precision and explicit currency.
- An invoice, accrual, advance, deduction and payment represent different facts.
- A payment by Company C for Company A requires an explicit allocation/basis;
  it does not silently move A's obligation to C.
- Mirror documents reuse confirmed source facts but retain each contract's parties,
  signers and obligations. Distinct signed documents stay distinct.
- Group totals need an explicit perimeter and internal-transaction treatment;
  never sum mirrored documents as independent new work.
- Amendments create versions; review binds to the current data/template version.
- Repeat imports use an atomic tenant/operation/idempotency key and source revision.
  A matching key with changed input conflicts; a preview is not an import.

## Delivery sequence and acceptance

| Increment | Reviewable result | Acceptance |
|---|---|---|
| 0: inventory and preview | Reuse existing core; source errors visible | Read-only preview tested; limitations explicit |
| 1: participants and agreements | Reviewed participant, site, contract and party records | N parties, role changes and access boundaries verified |
| 2: people and time | Engagement/assignment history and reviewed import | Transfers, ambiguous people, periods and reimports handled |
| 3: operational costs | Rate versions, accruals, payments and allocations | Source totals reconcile; missing rates stay unresolved |
| 4: document output | One preserved customer form populated from approved facts | Correct parties/version; no unauthorized signature or sending |
| 5: automation | Jobs, personal calendar and later requested extensions | Explicit authorization and idempotent execution |

No implementation dependency on CRM, banks or message delivery is added. Choose a
single external workflow engine only when a concrete scenario needs it. Reuse the
existing PostgreSQL/runtime/registry; do not create a second core or service per table.

## Synthetic end-to-end acceptance case

Person P is engaged by Company A. They work 240 minutes on Site X and 360 minutes
on Site Y on the same day. Two dated assignments retain the split. A reviewed
hourly rate of 100 units produces 400 and 600 units of operational accrual against
A. Company C pays 300 on A's behalf under an explicit basis. Allocating 300 leaves
700 outstanding against the original obligation. Currency, rounding and rules are
explicit; this example is not a production payroll rule. An unresolved name, rate
or contract blocks approval and remains visible instead of becoming zero cost.

## References and technical boundary

- Existing project contracts: `references/plugin-contract.md` and
  `references/core-architecture.md`.
- Existing runtime: `core/`, `server/`, `core/db/`; existing domain: `meetings`.
- New implemented preview: [workforce-preview.md](workforce-preview.md).
- PostgreSQL constraints: https://www.postgresql.org/docs/16/ddl-constraints.html
  Composite foreign keys support scoped references. Cross-row rules need the
  appropriate constraints/transaction handling, not a cross-table CHECK expression.
- MCP transport: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports
  Reuse the official SDK integration already in this repository; URL spelling alone
  does not establish protocol compatibility.

Open business facts: exact contract chain and third party's role, signed source
agreement, authoritative period/source sheets, and who confirms attendance and rates.
