# ES GST AU

Australian bank statement import, automated transaction coding, GST treatment
and BAS reporting for ERPNext v15+.

## Why this exists

Coding a bank statement by raising a Purchase Invoice and a Payment Entry for
every card transaction produces roughly three documents per line and leaves a
creditor balance that must then be cleared. For a business with 900 card
transactions a year that is over 2,600 documents to represent 900 card taps.

A card payment is not a credit purchase. It is a payment. The document that
represents it is a single Journal Entry that moves money out of the bank and
into an expense account, splitting out GST where the treatment calls for it.
This app posts that Journal Entry and reconciles it against the bank
transaction in one pass.

Purchase and Sales Invoices remain available, selected per rule, for genuine
trade creditors and real sales.

## Install

```bash
bench get-app es_gst_au
bench --site yoursite.com install-app es_gst_au
bench --site yoursite.com migrate
```

## Setup

1. **ES GST Settings** (one per company) — ABN, registration status, reporting
   cycle, cash or accruals basis, and the GST accounts.
2. **ES Bank Import Settings** (one per company) — default cost centre,
   whether to auto-submit, duplicate handling.
3. **ES Bank Rule** — the coding rules. Six starter rules ship disabled and
   without accounts, because the correct account differs per chart of accounts.
   Enable and code them before first use.

## Monthly workflow

1. Download the statement CSV from your bank.
2. Create an **ES Bank Import Batch**, attach the file, choose the bank account.
3. **Parse** — the format is detected, rows are classified, duplicates flagged.
4. Review the unmatched rows. Write a rule for any recurring merchant; the rule
   set converges and the queue shrinks each month.
5. **Submit** — Bank Transactions are created.
6. **Post Vouchers** — Journal Entries are created, submitted and reconciled.

## Design notes

**GST is computed as one eleventh of the GST-inclusive amount**, per the ATO
method, and the net is derived as `total - gst`. Deriving the net as
`total / 1.1` instead breaks the reconciliation by a cent on roughly 7% of
transactions. Verified against a 873-line statement: zero breaks.

**Duplicate protection** fingerprints each row on
`bank account + date + description + amounts + running balance`, stored in a
unique field on Bank Transaction. Where a bank supplies no running balance, an
occurrence index disambiguates genuinely repeated transactions so that two
identical coffees on the same day do not collapse into one.

**Unmatched lines are never guessed at.** A wrong account is worse than an
unposted one. They sit in the review queue with a one-click path to a new rule.

**Cost centres are resolved before posting.** ERPNext seeds a group cost centre
named after the company which cannot be posted to; the app falls back to a
postable child automatically.

## Supported statement formats

NAB, CBA, ANZ, Westpac and a generic fallback. Handles headerless exports,
single signed amount columns, separate debit/credit columns, bracket negatives
and currency symbols.

## BAS module

**ES BAS Return** is a submittable document per reporting period. Create it,
press Calculate, review, submit. It computes every GST label from the ledger:

    Sales      G1 G2 G3 G4 G5 G6 G7 G8 G9
    Purchases  G10 G11 G12 G13 G14 G15 G16 G17 G18 G19 G20
    Summary    1A 1B
    PAYG       W1 W2 W3 W4 W5

Derived labels (G5, G6, G8, G12, G16, G17, G19, W5) recalculate whenever an
adjustment is entered at G7 or G18, so a manual adjustment flows through to the
net amount without re-running the calculation.

**Capital versus non-capital.** G10 and G11 are split by the `is_capital` flag
on the matching Bank Rule, not by an amount threshold. Set it per rule for
things you actually capitalise.

**G9 and G20** are reported as the sum of GST actually charged rather than the
ATO's divide-by-eleven approximation, because that is what was really
collected and paid. The formula is used as a fallback only when an adjustment
has moved G8 or G19.

**Period locking.** Submitting a BAS return closes its date range. Journal
Entries, invoices and Payment Entries dated inside a submitted period are
blocked from submission or cancellation, so the ledger cannot drift away from
what was lodged. Amend the return to reopen the period.

**BAS Worksheet report** lists every taxable supply event behind each label,
with the voucher, party, treatment, net, GST and gross, filterable by label.
This is the drill-down for when the ATO asks how a figure was arrived at.

### Cash versus accruals

On **accruals**, the event date is the voucher posting date.

On **cash**, invoices are apportioned across the payments allocated against
them, pro rata. A part-paid invoice reports only the paid proportion.
Journal Entries posted from a bank statement are already cash events and are
taken at their posting date, so they appear identically on both bases.

### Due dates

Quarterly: Q1 28 Oct, Q2 28 Feb, Q3 28 Apr, Q4 28 Jul.
Monthly: the 21st of the following month.
A daily scheduled task raises a notification for any closed period falling due
within 14 days that has no return prepared.

## Status

- [x] Bank statement parsers (NAB, CBA, ANZ, Westpac, generic)
- [x] Rules engine
- [x] Import batch with duplicate protection
- [x] Journal Entry / Purchase Invoice / Sales Invoice posting
- [x] Auto-reconciliation
- [x] BAS Return with cash and accruals basis
- [x] BAS Worksheet drill-down report
- [x] Period locking on submitted returns
- [x] Due date notifications
- [x] Workspace
- [ ] ATO SBR / Online services lodgement (manual entry for now)

## Workspace

Installing the app adds an **ES GST AU** workspace with shortcuts to Bank Import
Batch, Bank Rule, BAS Return and the BAS Worksheet report, plus three link
cards: Bank Import, BAS & GST, and Setup.

## Not included

The app does **not** lodge with the ATO. Returns are prepared and locked here;
lodgement is done through the ATO's Online services or your tax agent, and the
receipt ID recorded against the return. Direct SBR lodgement requires an AUSkey
successor credential and a registered software ID, which is a separate
onboarding process.
