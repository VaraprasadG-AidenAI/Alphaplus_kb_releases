# From business rules to scenarios

A scenario is one situation that shows whether one rule holds. Plan
scenarios rule by rule: for each rule, go through the angles below and keep
every angle the code or the ticket gives you something concrete to test.

## The angles

| Angle | Asks | Look for in the code |
|---|---|---|
| Normal | Does the rule work as intended for a typical value? | The paragraph that applies the rule: the `MOVE`, `COMPUTE` or `REWRITE`. |
| Boundary | What happens at the limit, just inside it and just past it? | A declared length or digits (`30A`, `P(11,2)`), a range check (`IF ... > n`), a date key. |
| Negative | Does the system refuse what it should? | An error branch: a message id (`APP0025`), an error indicator, a reject count. |
| Combination | Do two rules hold at the same time? | Two conditions tested together, or one after another in the same path. |
| Regression | Does everything that shares the data still work? | Other writers and readers of the field, reports that print it, batch jobs whose CL overrides the file, logical files over it. |
| Existing data | Do records created before the change survive it? | A file whose record layout the ticket changes. |

Not every angle applies to every rule. A rule that only prints a value has
no negative scenario; a rule with no declared limit has no boundary
scenario. Skip an angle when there is nothing to test, not to save time.

## Coverage is complete when

- every business rule has at least one scenario,
- every declared limit has a scenario at the limit and one just past it,
- every refusal in the code has a negative scenario,
- every program, report or batch job that shares the changed data has a
  regression scenario, or is grouped with similar ones under one scenario
  with the reason stated,
- every open question that affects a tester's result has a scenario whose
  expected result is TO BE CONFIRMED BY BUSINESS.

## Example 1 - a width change

Ticket: *Increase customer name length from 30 to 40 characters.*

| Rule | Angle | Scenario |
|---|---|---|
| BR-01 The customer name holds up to 40 characters (ticket) | Normal | Save and reopen a customer with a 40-character name |
| BR-01 | Boundary | A 30-character name - today's maximum - is saved unchanged |
| BR-01 | Boundary | A 41st character cannot be entered, or is refused (TO BE CONFIRMED BY BUSINESS: which) |
| BR-02 A customer must have a name (code) | Negative | A blank name is refused with an error message |
| BR-03 The Accounts On Hold report prints the name (code) | Regression | The full 40-character name prints without overwriting the next column |
| BR-04 The History bulk update rewrites the name (code) | Regression | A 40-character name is still whole after the job runs |
| BR-01 | Existing data | Names saved before the change are unchanged after it |

## Example 2 - a calculation defect

Ticket: *Irish customers on monthly consolidated invoicing are charged the
wrong VAT.*

| Rule | Angle | Scenario |
|---|---|---|
| BR-01 Irish customers are charged the Irish VAT rate (code) | Normal | Irish customer, single invoice, valid rate on file |
| BR-01 + BR-02 Consolidated invoices add the shipments of the month (code) | Combination | Irish customer, consolidated invoice with several shipments |
| BR-01 | Regression | UK customer, consolidated - the fix must not move another company |
| BR-02 | Regression | Irish customer, not consolidated - the other invoicing path |
| BR-03 A zero tax rate is refused when rates are maintained (code) | Negative | Enter a tax rate of zero |
| BR-04 The rate is chosen by effective date (code) | Boundary | Invoice dated on the rate's effective date, and the day before |

The expected VAT amount in these scenarios depends on the rate. If no line
of code or the ticket states the Irish rate, the expected amount is TO BE
CONFIRMED BY BUSINESS - the calculation is still tested, its answer is not
guessed.

## Where the expected result comes from

| Origin | When | Example |
|---|---|---|
| Code | A line you quoted decides it | `IF TAXRTE OF DETAIL = ZERO` -> the rate is refused |
| Ticket | The ticket states it | "to 40 characters" -> all 40 characters are kept |
| Platform | An IBM i rule, not this code | A program not recompiled after a record format change fails its level check at open |
| Business | Nobody states it | The VAT rate; what happens to a 41-character name |

A calculated value is only as good as its inputs: `net x rate` comes from
the code only if the rate did. A rate chosen as test data makes the result
TO BE CONFIRMED BY BUSINESS.
