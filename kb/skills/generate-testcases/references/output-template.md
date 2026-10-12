# Output templates

Two Markdown files, written at the same time. The testcases file is what a
business tester runs; the evidence file is the trail IT checks it against.

## The testcases file - `Testcases/<ID>-testcases.md`

Scenarios and their testcases, and nothing else. The business rules, the
coverage table and every `member:line` stay in the evidence file, and no
program, file, field, paragraph, message id or library appears anywhere
below the heading. Repeat the ticket as given in the heading line, even if
it names a program.

Each scenario is a `##` heading that says the situation being tested. Under
it come its testcases, each `### TC-nn - <title>` with the same four parts
in the same order. Testcases are numbered TC-01 upwards across the whole
file, not restarted per scenario.

```markdown
# Testcases - <the ticket description, exactly as given>

<n> testcases across <s> scenarios. <m> need business confirmation before they can be run as written.

---

## SC-01 - A customer name at the new maximum length

### TC-01 - A 40-character customer name is saved in full

**Preconditions**
- A user who may maintain customers
- An existing customer to amend

**Test data**

| Field | Value |
|---|---|
| Customer name | `Northwind Wholesale Distribution Ltd Irl` (40 characters) |

**Test steps**

| # | Action |
|---|---|
| 1 | From the customer menu, choose option 4 (Customer Master Maintenance). |
| 2 | Open the customer to be amended. |
| 3 | Type the customer name above and press Enter. |
| 4 | Leave the screen and open the same customer again. |

**Expected result**
The customer name is shown in full, all 40 characters, with nothing cut off.

### TC-02 - A 40-character customer name prints in full on the customer list

**Preconditions**
- The customer amended in TC-01

**Test data**

| Field | Value |
|---|---|
| Customer name | `Northwind Wholesale Distribution Ltd Irl` (40 characters) |

**Test steps**

| # | Action |
|---|---|
| 1 | From the customer menu, choose the customer list report. |
| 2 | Run it for the customer amended in TC-01. |
| 3 | Look at the customer name column. |

**Expected result**
The whole 40-character name is printed, and it does not run into or push out
the column beside it.

---

## SC-02 - A customer name at today's maximum length

### TC-03 - A 30-character customer name is unchanged

**Preconditions**
- A user who may maintain customers
- An existing customer to amend

**Test data**

| Field | Value |
|---|---|
| Customer name | `Northwind Wholesale Dist Ltd` (30 characters) |

**Test steps**

| # | Action |
|---|---|
| 1 | From the customer menu, choose option 4 (Customer Master Maintenance). |
| 2 | Type the customer name above and press Enter. |
| 3 | Open the same customer again. |

**Expected result**
The customer name is shown exactly as it was typed, unchanged.

---

## SC-03 - A customer name longer than the new maximum

### TC-04 - A 41-character customer name

**Preconditions**
- A user who may maintain customers

**Test data**

| Field | Value |
|---|---|
| Customer name | `Northwind Wholesale Distribution Limited` plus one more character (41 characters) |

**Test steps**

| # | Action |
|---|---|
| 1 | From the customer menu, choose option 4 (Customer Master Maintenance). |
| 2 | Try to type the 41-character name into the customer name. |
| 3 | Press Enter. |

**Expected result**
TO BE CONFIRMED BY BUSINESS: whether the 41st character cannot be typed at
all, or is accepted and then refused with a message. Confirm which before
running this test.

---

## Open questions

- TC-04: TO BE CONFIRMED BY BUSINESS: whether a 41st character is refused with a message or simply cannot be typed.
- <each open question from Step 2, and any scenario left uncovered, with the reason>
```

Notes on the parts:

- **Scenario heading** - the situation in business words. No rule id, no
  angle label, no evidence.
- **Test data** - one row per value. Business name on the left, the exact
  value on the right. Give the character count when length is the point.
  Write `(left blank)` or `(not changed)` rather than an empty cell, and
  `None` when the case needs no data.
- **Test steps** - one action per row, numbered from 1, starting at a menu
  option or screen the tester recognises.
- **Expected result** - prose, not a table. A result nobody has confirmed
  starts `TO BE CONFIRMED BY BUSINESS:` and also appears under Open
  questions.

## The evidence file - `.alphaplus-work/<ID>-evidence.md`

Everything technical lives here: the rules with their evidence, which rule
each scenario tests, the coverage table, and the validation results per
testcase.

```markdown
# Evidence - <ticket>

## Business rules

### BR-01 - The customer name holds up to 40 characters
Source: the ticket
Status: stated by the ticket

### BR-02 - A customer must have a name
Evidence: CUS200:532-537 - `IF  CUSNAM OF DETAIL = SPACES` ... `MOVE 'N' TO WS-VALID-FLAG`
Status: confirmed in source

## Scenarios

| Id | Rule | Angle | Scenario | Testcases |
|---|---|---|---|---|
| SC-01 | BR-01 | Normal | A name at the new maximum length | TC-01, TC-02 |
| SC-02 | BR-01 | Boundary | A name at today's maximum length | TC-03 |
| SC-03 | BR-01 | Boundary | A name longer than the new maximum | TC-04 |
| SC-04 | BR-02 | Negative | A blank name is refused | TC-05 |

## Coverage

| Rule | Scenarios | Testcases |
|---|---|---|
| BR-01 | SC-01, SC-02, SC-03 | TC-01, TC-02, TC-03, TC-04 |
| BR-02 | SC-04 | TC-05 |

## Testcase validation

### TC-05 - A blank customer name is refused
Covers: BR-02 / SC-04
Exercises: CUS200 (3100-VALIDATE-CUSNAM), CUS200D, CUSMAS.CUSNAM
- Evidence: CUS200:532-537 re-read, quote present, code line - pass
- Names: CUS200, CUS200D, CUSMAS, CUSNAM found - pass
- Test data: blank fits A(30); refused by CUS200:532 - pass
- Entry: CUSNAM usage B at CUS200D:73 - pass
- Reach: CUSMENU -> CUS200C -> CUS200 - pass
- Arithmetic: none
- Sourcing: outcome from CUS200:532-537 - pass
- Shape: TC heading, four parts present - pass
- Wording: no technical names - pass
```
