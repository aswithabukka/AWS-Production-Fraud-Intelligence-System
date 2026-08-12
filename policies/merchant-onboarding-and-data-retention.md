# Merchant Onboarding and Data Retention Policy

**Document ID:** POL-MO-002
**Version:** 1.8
**Effective:** 2025-11-01
**Owner:** Risk & Compliance

> Synthetic document written for a portfolio project. Not real policy.

## 1. Merchant onboarding due diligence

Every merchant is assigned an onboarding risk band from its MCC, expected monthly volume,
and business model.

| Band | Criteria | Requirements |
|---|---|---|
| Standard | Low-risk MCC, < $100k/month expected | Business registration, beneficial ownership, bank verification |
| Enhanced | High-risk MCC, or $100k–$1M/month | Standard plus 6 months processing history, financial statements, site visit or equivalent |
| Restricted | Prohibited or heavily regulated MCC | Executive committee approval required |

High-risk MCCs for onboarding purposes include 5944 (jewelry), 7995 (betting and casino
gambling), 5967 (direct marketing, inbound telemarketing), 6011 (ATM cash disbursement),
and 4511 (airlines).

## 2. Ongoing monitoring

Enhanced-band merchants are reviewed every 6 months. A review is triggered early by any of:

- Chargeback ratio entering the Early Warning tier or above (see POL-CB-001).
- Monthly volume exceeding 300% of the declared expectation.
- A change in beneficial ownership.
- Three or more confirmed fraud cases in a rolling 30 days.

## 3. Settlement holds

A settlement hold may be applied for up to 180 days where the merchant enters the
Egregious chargeback tier, where fraud is suspected, or where a change of control has not
been disclosed. Holds beyond 90 days require documented approval from Risk & Compliance
and written notice to the merchant.

## 4. Data retention

| Data class | Retention | Basis |
|---|---|---|
| Transaction records | 7 years | Regulatory reporting |
| Authorisation logs | 24 months | Dispute defence |
| Fraud case files | 7 years from case closure | Regulatory reporting |
| Device fingerprints | 13 months | Legitimate interest, fraud prevention |
| Derived fraud features | 25 months | Model training and backtesting |
| Quarantined and rejected records | 90 days | Operational troubleshooting |

## 5. Card data handling

The primary account number (PAN) is never stored in the analytics platform. Transaction
records carry a surrogate `transaction_id` and a `customer_id`; neither is derivable from
the PAN.

Access to any column classified as cardholder-identifying is restricted by role:

- **analyst** — aggregate tables only. No access to per-transaction records and no access
  to columns classified as cardholder-identifying.
- **risk_analyst** — aggregate tables plus fraud-specific columns, for investigation.

Column-level restrictions are enforced by the data catalog's access control, not by
convention in query authoring. A control that depends on an analyst writing the right
query is not a control.

## 6. Right to erasure

An erasure request is honoured for device fingerprints and marketing-derived attributes.
Transaction records are retained under the regulatory basis in section 4 and are exempt.
Erasure requests are actioned within 30 days and logged.
