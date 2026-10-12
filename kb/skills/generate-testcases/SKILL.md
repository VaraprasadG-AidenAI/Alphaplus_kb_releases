---
name: generate-testcases
description: Generates business-facing testcases for an Alphaplus IBM i (AS/400) COBOL ticket from nothing more than the ticket description. Gathers context with the alphaplus knowledge-base tools (trace_ticket, where_is_field_used, what_uses, read_member, describe_file and the rest), derives the business rules the code enforces, plans scenarios that cover every rule, writes the testcases, and re-checks each one against the source with the same tools before writing a Markdown file. Use whenever the user asks for testcases, test cases, test scenarios, a test plan or acceptance tests for an Alphaplus JIRA ticket, defect or change request, or types /generate-testcases.
---

# Generate testcases from business rules

You turn a ticket description into testcases a business tester can run. The
chain is always the same, and each link rests on the one before it:

```
ticket  ->  context (tool calls)  ->  business rules  ->  scenarios  ->  testcases  ->  validation  ->  Markdown file
```

A testcase is only as trustworthy as the rule it tests, and a rule is only
as trustworthy as the line of code behind it. That is why every rule carries
evidence you have read yourself, and why every testcase names the rule it
tests. The testers who read the result cannot open the COBOL, so the chain
from code to expected result has to be sound before the file reaches them.

## Inputs and outputs

- **Input:** the ticket description the user typed, used exactly as given. It
  is enough on its own; do not ask for an id, acceptance criteria or more
  detail.
- **Output:** one Markdown file, `<project root>/Testcases/<ID>-testcases.md`,
  written for business testers. `<ID>` is the ticket id if the description
  contains one, otherwise a short kebab-case slug of its first words. It
  holds **the scenarios with their testcases, and nothing else** - no
  business rules, no coverage table, no technical names.
- **Working notes:** `<project root>/.alphaplus-work/<ID>-evidence.md` - the
  business rules with their `member:line` evidence, which rule each scenario
  tests, the coverage table and the validation results. This is the trail IT
  uses to check your work; testers never see it.

The rules are how you *reach* good scenarios, so they stay in the evidence
file. A tester opening the delivered file sees each scenario as a heading
with the testcases that prove it underneath.

`<project root>` is the folder the session started in.

## Workflow

Do the whole workflow yourself, in one turn, from first tool call to written
file. The evidence you read in Step 1 is what makes Steps 2-5 correct, so
handing a step to a subagent or another skill would lose it. Copy this
checklist into your reply and tick it off as you go:

```
- [ ] 1. Gather context with the knowledge-base tools
- [ ] 2. Write the business rules, each with quoted evidence (evidence file)
- [ ] 3. Plan scenarios that cover every rule
- [ ] 4. Write the testcases under each scenario: preconditions, test data, steps, expected result
- [ ] 5. Validate every testcase with the tools, repair, re-validate
- [ ] 6. Write both files and report in one line
```

### Step 1 - Gather context

Start with `trace_ticket(<ticket description>)` to find the fields, files
and programs the ticket is about, then follow the leads until you can say,
from code you have read, what the system does today in the area the ticket
changes. When a tool comes back empty or cannot answer, see
[When a tool cannot answer](#when-a-tool-cannot-answer).

Find out:

- **The data**: which fields and files the ticket turns on, their declared
  type and length (`describe_file`, `where_is_field_used`).
- **The behaviour**: which programs read and write that data, and the
  paragraphs where they validate, calculate or refuse it (`where_is_field_used`,
  `what_paragraph_does`, then `read_member` on the exact lines).
- **The user's route**: which screen a user works on, which fields there
  they can type into, and how they reach it from a menu (`search_source` on
  the display files, `read_member` on the field's DDS line, `what_uses` up
  the callers to the menu).
- **The neighbours**: other programs, reports, batch jobs and logical files
  that use the same data and must keep working (`who_touches_file`,
  `what_uses`, `resolve_overrides`).
- **The business labels**: the names testers know, taken from the code -
  a field's `COLHDG`/`TEXT` (`describe_file`), a screen's or report's title
  constant on row 1 (`read_member` on the first lines of the display or
  printer file), a menu option's number and text (`read_member` on the menu
  source that calls the program's CL driver).

Treat `trace_ticket`, `route_ticket`, `find_similar` and `read_summary` as
pointers to where to look. Their text is ranked or
written by a model, so confirm anything they suggest with `read_member`
before relying on it. Header comments are pointers too - the executable
lines below them are the evidence.

### Step 2 - Write the business rules

A business rule is one sentence, in business words, stating something the
system does or must do, backed by the lines of code that do it. Rules live
in the evidence file only - they never appear in the delivered testcases.
Write each one like this:

```markdown
### BR-02 - A customer must have a name
Evidence: CUS200:532-537 - `IF  CUSNAM OF DETAIL = SPACES` ... `MOVE 'N' TO WS-VALID-FLAG`
Status: confirmed in source
```

Rules come from three places, and the evidence file says which:

| Source | Example | Status |
|---|---|---|
| Code you read | "A blank customer name is refused" | confirmed in source |
| The ticket | "The customer name holds up to 40 characters" | stated by the ticket |
| The platform | "A program not recompiled after a file change fails when it opens the file" | IBM i rule |

When something matters to the ticket but neither the code nor the ticket
settles it - a rate, a limit, what should happen past the new length - record
it as an **open question** rather than a rule. It still gets tested (Step 3);
its expected result is what the business must confirm.

### Step 3 - Plan scenarios that cover every rule

For each rule, work out the situations that would show whether the rule
holds. Look at each rule from the angles in
[references/coverage.md](references/coverage.md) - the rule working as
intended, its limits, the values it refuses, how it combines with other
rules, and the neighbours the change must not break - and plan a scenario
for every angle the code or the ticket gives you something to test.

There is no target number. Coverage is right when:

- every business rule has at least one scenario, and
- every limit the code declares has a scenario at the limit and just past
  it, and
- every refusal the code makes has a negative scenario, and
- every program, report or job that shares the changed data has a
  regression scenario, or is grouped into one with a reason.

Give each scenario an id (`SC-01`) and a title in business words. Record
which rule and angle it comes from in the evidence file; the delivered file
shows the scenario as a heading with its testcases underneath, and says
nothing about rules or angles.

### Step 4 - Write the testcases

A scenario names a situation; a testcase is one run of it a tester can
follow. Write at least one testcase under each scenario - more when the
situation has to be shown in more than one place, such as on the screen and
again on the report that prints the same value.

The scenario is a `##` heading in the delivered file. Every testcase under
it is headed `### TC-nn - <title>` and has the same four labelled parts, in
this order, so a tester never has to re-learn the layout:

```markdown
## SC-04 - A customer saved without a name

### TC-07 - A blank customer name is refused

**Preconditions**
- A user who may maintain customers
- An existing customer to amend

**Test data**

| Field | Value |
|---|---|
| Customer name | (left blank) |

**Test steps**

| # | Action |
|---|---|
| 1 | From the customer menu, choose option 4 (Customer Master Maintenance). |
| 2 | Open the customer, clear the customer name and press Enter. |

**Expected result**
The change is refused, an error message is shown against the customer name,
and the customer is left as it was.
```

What each part must hold:

- **Scenario heading** - the situation, in business words. No rule id, no
  angle label, no evidence.
- **Title** - in business words, what the case shows ("A 40-character
  customer name is saved in full"). No ids, no program names.
- **Preconditions** - the data and access the tester needs before step 1.
- **Test data** - a row per value, with its business name and the exact
  value. Exact values, not "a long name"; note the character count when
  length is the point. Say "(left blank)" or "(not changed)" rather than
  leaving a cell empty. A case that needs no data says "None".
- **Test steps** - numbered actions, one per row, each a single thing the
  tester does. Start from a menu option or screen they recognise and end at
  the place the result can be seen.
- **Expected result** - prose, what the tester should see, and nothing the
  evidence does not support.

Number the testcases TC-01 upwards across the whole file, not restarted
under each scenario, and order the scenarios the way a tester would work
through them. Keep which rule each scenario came from in the evidence file,
not in the delivered one.

Where the expected result comes from decides how it is written:

| Origin | Write it as |
|---|---|
| The code (a quoted line decides it) | a plain statement: "The change is refused and an error message is shown." |
| The ticket | a plain statement: "All 40 characters are shown." |
| The platform | a plain statement: "The job finishes with no file error." |
| Nobody states it | `TO BE CONFIRMED BY BUSINESS: whether a 41-character name is refused or cut short.` |

A scenario with an unconfirmed expected result is still worth running, so
it keeps its testcase; the TO BE CONFIRMED BY BUSINESS wording tells the
tester to get sign-off first.

Testers read these in business words. Name things the way the code labels
them for users - "customer name", "Customer Master Maintenance", "menu
option 4". When the code gives something no label, describe it by what it
does ("the overnight job that updates customer history").

Nothing technical reaches the delivered file. These all belong in the
evidence file instead:

| Never in a testcase | Write instead |
|---|---|
| `CUS200`, `AGD604` | the screen or job by its business name |
| `CUSMAS`, `ACSREC` | "the customer record", "the receipt" |
| `CUSNAM`, `ACSSTS` | "customer name", "account status" |
| `6380-CHECK-ACSSTS` | nothing - the tester cannot see paragraphs |
| `APP0026` | "an error message is shown against the account status" |
| `CUS200:532`, `A(30)` | nothing - evidence and declarations are IT's |

A ticket that names a program is repeated verbatim in the heading only; the
testcases below it still say it in business words.

### Step 5 - Validate every testcase with the tools

Check each testcase against the source before it goes in the file:

1. **Evidence**: re-read each cited range with `read_member`; the quoted
   words are on those lines and the lines are code, not comments - in
   fixed-format COBOL and DDS, column 7 is not `*`.
2. **Names**: every program, file, field and paragraph behind the case
   exists (`find_object`, `what_uses`, `describe_file`, `program_structure`).
3. **Test data**: each value fits the field's declared type and length
   (`describe_file`). Character values are no longer than the length - the
   new length, if the ticket widens the field - and numeric values fit the
   digits and decimals. Count string lengths exactly. A value meant to be
   refused really breaks the declaration, or the rule's evidence shows the
   code refusing it (for example `IF CUSNAM OF DETAIL = SPACES`); a
   "refused" value the code would accept is a typo, not a negative test.
4. **Executability**: each value the tester types sits on a screen field
   that accepts input - `search_source(<FIELD>, member_type='DSPF')`, then
   `read_member` on the hit; usage `B` (both) or `I` (input) passes, `O` is
   output only. The screen is reachable: `menu_path_to`, or the fallback
   below, shows a menu option or command that starts the program.
5. **Arithmetic**: any calculated value is recomputed from the case's own
   test data and matches the expected value.
6. **Sourcing**: every number and outcome in the expected result traces to
   the code, the ticket or a platform rule; anything else is reworded to
   TO BE CONFIRMED BY BUSINESS.
7. **Shape**: the case is headed `### TC-nn - <title>` under its scenario's
   `##` heading, and carries all four parts - Preconditions, Test data, Test
   steps, Expected result - in that order, with no rule id among them.
8. **Wording**: read the whole case back and strike out every program, file,
   field, paragraph, message id, library, `member:line` and declared type,
   replacing each with the business words for it.

Record each check's result in the evidence file. When a check fails, fix
the testcase with evidence you have just read - a different screen, a
corrected value, a reworded expected result - and check it again. Allow
yourself two rounds of repair. A testcase whose evidence is still wrong
after that is left out, and its scenario is listed as uncovered under Open
questions so nothing disappears silently.

### Step 6 - Write the file and report

Write `Testcases/<ID>-testcases.md` using
[references/output-template.md](references/output-template.md): a heading,
one summary line, each scenario with its testcases, and the open questions.
Nothing else - the business rules, the coverage table and every
`member:line` stay in `.alphaplus-work/<ID>-evidence.md`, which you write at
the same time.

Then reply with one line: the file path, the number of testcases, how many
need business confirmation, and any scenario left uncovered.

## When a tool cannot answer

A tool that returns nothing for a name means the index holds nothing, not
that the thing does not exist. Run `coverage_report()` before concluding
something is absent.

| Situation | Do instead |
|---|---|
| `menu_path_to` says the graph is not reachable | `what_uses(<program>)` to find its CL driver, `what_uses(<CL>)` to find the menu, then `read_member` on the menu for the option number and text. |
| `explain_screen` does not list a field's usage | `search_source(<FIELD>, member_type='DSPF')` and read the usage column on the DDS line. |
| An override's library comes from a CL variable (`inferred`) | Keep the rule, note the confidence in the evidence file, and list the library as an open question. |
| A `CALL` goes through a variable | Record the call as unresolved; the programs it reaches are an open question. |
| Job scheduling | Not in the source. Which batch jobs run is an open question. |

## If the ticket cannot be tied to the code

If Step 1 finds no field, file or program the ticket is about, say so in
chat, list what you searched and the closest leads, and ask the user to name
the screen, report or field. Write no file - an empty or speculative
testcase file would look like coverage to a tester without being coverage.
