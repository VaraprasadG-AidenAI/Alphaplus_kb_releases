# Section 10 - metrics

Three tables. Two are measured. One cannot be, and is left blank on purpose.

The distinction is the whole point of this section. A count of what the run
produced is a fact. A confidence tier is a statement about how well a row is
evidenced. **Neither is accuracy.** Accuracy needs something to be right or
wrong against, and this engagement has no such thing.

## 10.1 What this run produced

Counts only. Every one of them is checkable by someone re-reading the report.

| Measure | Value |
|---|---|
| Members named as changing by the impact analysis | `<n>` |
| Members changed in this report | `<n>` |
| Members named but found to need no change | `<n>` |
| Members named but not found on disk | `<n>` |
| Members this report adds that the analysis did not name | `<n>` |
| Lines changed | `<n>` |
| Lines changed by member type | `PF <n>`, `LF <n>`, `CBLLE <n>`, `CLLE <n>`, `DSPF <n>`, `CPY <n>` |
| Libraries touched | `<n>` - name them |
| Rows carrying `member:line` evidence | `<n>` of `<n>` |
| Sites located by content rather than by the line number given | `<n>` of `<n>` |
| Recall ceilings hit | `<n>` - name each one |
| Items left unresolved | `<n>` |
| Guard failures resolved before the report was written | `<n>` |
| Recompile set carried from the analysis | `<n>` |

Recall ceilings are the known blind spots. Name whichever applied:

| Ceiling | Effect |
|---|---|
| a copybook or layout not in the extraction | field usage in that program is unknown, not none |
| RPG members | field usage is not extracted - unknown, not none |
| embedded SQL | parsed shallowly; host-variable usage is under-reported |
| members that failed to parse | invisible to every query |
| libraries not extracted | unreachable by any tool |
| job scheduler entries | not in the extraction at all, and never will be |

## 10.2 Confidence, carried from the impact analysis

Each changed member keeps the tier the analysis gave it. This says how well
the member's inclusion is evidenced - not whether the change to it is right.

| Tier | What earns it | Members | Lines |
|---|---|---|---|
| Confirmed | a coupling read from executable source | `<n>` | `<n>` |
| Probable | the coupling is real, but its target was resolved rather than read - an `OVRDBF` through a variable, an SQL table matched by name | `<n>` | `<n>` |
| Possible | a text match or a heuristic | `<n>` | `<n>` |

Every member in section 3 sits in exactly one tier, and the totals here equal
the member count in the header.

A member changed on a `Possible` tier gets one sentence saying why it was
changed anyway. A member on any tier that was **not** changed belongs in
section 4 with its reason.

## 10.3 Accuracy - not measured

**Leave every cell blank.** This is not an omission to be filled in later in
the same report; it is the finding.

| Measure | Value | Why |
|---|---|---|
| Precision of the impact analysis | | no ground truth |
| Recall of the impact analysis | | no ground truth |
| F1 | | no ground truth |
| Accuracy of the implementation | | no ground truth |
| False positives | | no ground truth |
| False negatives | | no ground truth |

### The rule

- Do not estimate any of these, in the table or in prose.
- Do not substitute a confidence tier for an accuracy figure. "68 Confirmed"
  is not "68 correct".
- Do not write a percentage anywhere in the report that is not a count from
  10.1 divided by another count from 10.1.
- Do not say "high accuracy", "highly reliable" or anything that reads as a
  score. Give the counts and let them speak.

### Why it is blank

Accuracy is a comparison. It needs a second list that is known to be right.
There is no such list: no set of tickets with the change the client actually
shipped, member by member, and no compile to tell us whether a change even
builds.

### What would make it measurable

State these in the report, one line each, so the blank reads as a known gap
rather than an oversight:

| To measure | We would need |
|---|---|
| Precision and recall of the analysis | a set of closed tickets with the change set the client actually shipped, member by member, to compare against |
| Whether a change is correct | a compile of the changed members, which turns "did we reach everything" into a mechanical answer |
| Whether a change is complete | the test the client runs for that ticket, and its result |

Until one of those exists, the honest report gives counts, gives tiers, and
says the accuracy is unmeasured.
