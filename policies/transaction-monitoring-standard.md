# Transaction Monitoring Standard

**Document ID:** POL-TM-004
**Version:** 2.1
**Effective:** 2026-02-15
**Owner:** Fraud Analytics

> Synthetic document written for a portfolio project. Not real policy.

## 1. Purpose

Defines the detection rules, thresholds, and escalation paths for real-time and
near-real-time transaction monitoring.

## 2. Velocity rules

A velocity breach is any of the following, evaluated per card:

| Rule | Threshold | Window | Action |
|---|---|---|---|
| Transaction count | 5 or more | 1 hour | Step-up authentication on the next transaction |
| Transaction count | 20 or more | 24 hours | Soft decline, cardholder verification required |
| Distinct merchants | 8 or more | 1 hour | Review queue |
| Declined attempts | 4 or more | 15 minutes | Temporary card block, 30 minutes |
| Cumulative amount | $5,000 or more | 1 hour | Review queue |

Card-testing patterns — a sequence of low-value authorisations followed by a high-value
authorisation on the same card within one hour — are escalated directly to the review
queue regardless of the counts above.

## 3. Geographic rules

- **Impossible travel.** Two authorisations whose implied travel speed exceeds 900 km/h
  are flagged. Implied speed is the great-circle distance between the transaction
  locations divided by the elapsed time.
- **High-risk geography.** Transactions originating in a jurisdiction on the restricted
  list require manual review above $250.
- **Country change.** A first-ever transaction in a new country is flagged when it occurs
  within 6 hours of a transaction in the cardholder's home country.

Impossible-travel flags alone do not decline a transaction. Corporate travel, VPN egress,
and delegated card use all produce legitimate flags, so the rule contributes to a score
rather than acting as a hard block.

## 4. Amount anomaly

A transaction is an amount anomaly when its value exceeds the cardholder's trailing
30-day mean by 3 or more standard deviations. Cardholders with fewer than 3 prior
transactions in the window have no baseline; those transactions are scored on merchant
risk alone and are never flagged as amount anomalies on the basis of an absent baseline.

## 5. Merchant risk scoring

Merchant risk is the merchant's historical fraud rate, smoothed toward the portfolio-wide
rate with a pseudo-count of 20 transactions. Tiers:

| Tier | Smoothed fraud rate |
|---|---|
| Low | < 2% |
| Medium | 2% – 5% |
| High | ≥ 5% |

A merchant with no history is assigned the portfolio rate, not zero. Newly onboarded
merchants are the highest-risk population by volume-adjusted loss, so treating an unknown
merchant as safe inverts the control.

## 6. Escalation

| Composite signals fired | Action |
|---|---|
| 0 | Approve |
| 1 | Approve, log for analysis |
| 2 | Step-up authentication |
| 3 or more | Soft decline and route to the review queue |

Review-queue items must be actioned within 4 hours during business hours and within 12
hours otherwise.

## 7. Model and rule governance

Detection thresholds are reviewed quarterly. Any threshold change requires a backtest over
90 days of production data showing the projected change in false-positive rate and
detected-loss rate. Changes projected to raise the false-positive rate by more than 15%
relative require sign-off from the Head of Fraud Risk.
