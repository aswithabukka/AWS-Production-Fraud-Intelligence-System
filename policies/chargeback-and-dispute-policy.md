# Chargeback and Dispute Handling Policy

**Document ID:** POL-CB-001
**Version:** 3.2
**Effective:** 2026-01-01
**Owner:** Fraud Risk Operations

> Synthetic document. Written for a portfolio project to give the retrieval layer a
> realistic corpus. It is not real policy from any institution.

## 1. Scope

This policy governs the handling of cardholder disputes and chargebacks for all
card-present and card-not-present transactions processed on the platform.

## 2. Dispute filing windows

| Dispute reason | Cardholder filing window | Issuer representment window |
|---|---|---|
| Fraud — card not present | 120 days from transaction date | 45 days from chargeback |
| Fraud — card present, counterfeit | 120 days from transaction date | 45 days from chargeback |
| Goods or services not received | 120 days from expected delivery, max 540 days from transaction | 30 days |
| Duplicate processing | 90 days from transaction date | 30 days |
| Incorrect amount | 90 days from transaction date | 30 days |
| Cancelled recurring transaction | 120 days from the transaction being disputed | 30 days |

Disputes filed outside the applicable window are rejected at intake and do not enter the
chargeback lifecycle.

## 3. Chargeback thresholds

Merchant chargeback performance is measured monthly on two ratios:

- **Chargeback ratio** = chargebacks in the month / transactions in the month.
- **Fraud ratio** = fraud-coded chargebacks in the month / transactions in the month.

| Tier | Chargeback ratio | Fraud ratio | Action |
|---|---|---|---|
| Standard | < 0.65% | < 0.60% | No action |
| Early warning | 0.65% – 0.89% | 0.60% – 0.89% | Written notice; merchant must submit a remediation plan within 30 days |
| Excessive | 0.90% – 1.79% | 0.90% – 1.79% | Monthly monitoring, remediation plan mandatory, review fees apply |
| Egregious | ≥ 1.80% | ≥ 1.80% | Immediate settlement hold and account review; termination if not remediated within 60 days |

A merchant processing fewer than 100 transactions in a month is exempt from ratio-based
action, because the denominator is too small for the ratio to be meaningful.

## 4. Liability allocation

- **EMV liability shift.** For a counterfeit card-present transaction, liability falls on
  the party that did not support chip. If the merchant terminal was not EMV-capable and
  the card was chip-enabled, the merchant bears the loss.
- **3-D Secure.** A card-not-present transaction authenticated through 3-D Secure with a
  successful cardholder challenge shifts fraud liability to the issuer.
- **Unauthenticated card-not-present.** Liability rests with the merchant.
- **Recurring transactions.** Liability rests with the merchant when the cardholder has
  previously requested cancellation in writing and the merchant continued to bill.

## 5. Representment evidence

A representment must include, at minimum:

1. The authorisation record, including the approval code and the authorisation timestamp.
2. Evidence of delivery or service provision, addressed to the cardholder's verified
   address.
3. For card-present transactions: the terminal read method and, where a chip was used,
   the cryptogram verification result.
4. For digital goods: the account login history and the device fingerprint associated with
   the purchase.

Representments submitted without item 1 are automatically withdrawn.

## 6. Pre-arbitration and arbitration

If the cardholder disputes the representment, the case escalates to pre-arbitration.
Pre-arbitration must be filed within 30 days of the representment. Arbitration filing fees
are non-refundable and are borne by the losing party. The platform does not file for
arbitration where the disputed amount is below $75, because the filing fee exceeds the
recoverable amount.
