# Words that mean the same thing

A sweep for `SPACES` does not find `SPACE`. They are the same value, written
two ways, and a report that searched for one and concluded "no other site
uses it" is wrong in a way nothing will catch.

This file is the list. **Search for every form, always.** A negative result
from a single-form search is not evidence.

This application is **IBM i (AS/400), ILE COBOL**. Everything below is the
IBM i rule. Sources are IBM's own references, cited at the end.

---

## 1. Figurative constants - singular and plural are interchangeable

IBM's wording, verbatim:

> The singular and plural forms of NULL, ZERO, SPACE, HIGH-VALUE, LOW-VALUE,
> and QUOTE can be used interchangeably.

| Write any of | Meaning |
|---|---|
| `ZERO` · `ZEROS` · `ZEROES` | the value zero |
| `SPACE` · `SPACES` | one or more blanks |
| `HIGH-VALUE` · `HIGH-VALUES` | the highest character in the collating sequence |
| `LOW-VALUE` · `LOW-VALUES` | the lowest character in the collating sequence |
| `QUOTE` · `QUOTES` | the quotation character |
| `NULL` · `NULLS` | an invalid pointer value |

Three spellings for zero, not two. `ZEROES` is as valid as `ZEROS`.

**`ALL` in front of any of them** - `ALL SPACES`, `ALL '*'`, `ALL ZEROS` - is
the same value repeated to fill the target. A search for `MOVE SPACES` misses
`MOVE ALL SPACES`.

**Symbolic characters.** A program may define its own names for characters in
`SPECIAL-NAMES`, under `SYMBOLIC CHARACTERS`. Those names are local to that
program and mean nothing outside it. If a search turns up an unfamiliar
constant, read `SPECIAL-NAMES` before deciding it is a data name.

---

## 2. Abbreviations

| Long form | Short form | Notes |
|---|---|---|
| `PICTURE` | `PIC` | identical |
| `THROUGH` | `THRU` | identical, in `PERFORM`, `VALUE`, `RENAMES`, `EVALUATE` |
| `CORRESPONDING` | `CORR` | identical, in `MOVE`, `ADD`, `SUBTRACT` |

A field declared `PICTURE X(30)` and one declared `PIC X(30)` are the same
declaration. A sweep for `PIC X(` that reports "30 sites" has not counted the
`PICTURE X(` ones.

---

## 3. USAGE

IBM's wording for ILE COBOL on IBM i:

> COMP and COMP-3 are synonymous with PACKED-DECIMAL.
> COMP-4 is synonymous with BINARY.

| Write any of | Storage |
|---|---|
| `COMP` · `COMPUTATIONAL` · `COMP-3` · `COMPUTATIONAL-3` · `PACKED-DECIMAL` | **packed decimal** |
| `COMP-4` · `COMPUTATIONAL-4` · `BINARY` | binary |

**`COMP` here means packed decimal.** A field written `PIC S9(7) COMP` holds
four bytes of packed digits, not seven characters and not a binary integer.
That matters wherever a length is computed, an offset is taken, a record is
moved as a group, or a layout is compared with a file.

> One warning, and it is about the analyst rather than the platform. Almost
> every COBOL example, tutorial and manual online is written for the
> mainframe, where `COMP` means something else. So the wrong answer is the
> one that comes to hand first, from a search or from memory, and it looks
> perfectly ordinary.
>
> **Take `COMP` from this table, never from a remembered example.** If a
> byte length matters, read the field's own declaration in the source.

---

## 4. Optional words

Reserved words that may be written or left out with no effect on what runs.
They exist for readability:

`IS` · `ARE` · `THEN` · `THE` · `OF` · `IN` · `TO` · `WHEN` · `PROCEED TO`

`IN` and `OF` are interchangeable as qualifiers: `FIELD OF RECORD` and
`FIELD IN RECORD` name the same item. The same is true in `COPY <member> IN
<file>` and `COPY <member> OF <file>`.

So `PIC X(30) IS DISPLAY`, `PIC X(30) DISPLAY` and `USAGE IS DISPLAY` are the
same declaration written three ways.

---

## 5. Relational operators

Each row is one test written several ways. A search for `IF X = SPACES` finds
none of the others.

| Test | Forms |
|---|---|
| equal | `=` · `IS EQUAL TO` · `EQUALS` |
| not equal | `NOT =` · `IS NOT EQUAL TO` |
| greater | `>` · `IS GREATER THAN` |
| less | `<` · `IS LESS THAN` |
| greater or equal | `>=` · `IS GREATER THAN OR EQUAL TO` · `IS NOT LESS THAN` |
| less or equal | `<=` · `IS LESS THAN OR EQUAL TO` · `IS NOT GREATER THAN` |

`IS` is optional in every one of them.

---

## 6. What this means for a sweep

**Every search over source text searches for every form.** Not the one the
ticket happened to use.

| Looking for | Search for |
|---|---|
| a field set to blanks | `SPACE`, `SPACES`, `ALL SPACES`, and the `INITIALIZE` of any group holding it |
| a field set to zero | `ZERO`, `ZEROS`, `ZEROES`, `ALL ZEROS` |
| a picture | `PIC`, `PICTURE` |
| a paragraph range | `THRU`, `THROUGH` |
| a group move | `CORR`, `CORRESPONDING` |
| a packed field | `COMP`, `COMPUTATIONAL`, `COMP-3`, `COMPUTATIONAL-3`, `PACKED-DECIMAL` |
| a comparison | the symbol **and** the words - see section 5 |

**A nil result from a single-form search is not a finding.** It is a search
that was not finished. Say which forms were searched, in the report, next to
the count.

When a sweep is reported, name the forms:

> `<n>` sites, searching `SPACE`, `SPACES` and `ALL SPACES`.

not:

> `<n>` sites.

---

## 7. Where this bites hardest

| Check | Why the forms matter |
|---|---|
| **Capacity** | a `COMP` field's byte length is not its digit count. `PIC S9(7) COMP` on IBM i is 4 bytes packed, not 7. Sizing an assignment against the digit count is wrong |
| **Single assignment** | `MOVE SPACES TO X` and `MOVE ALL SPACES TO X` and `INITIALIZE` of X's group all assign X. Counting only one form under-counts |
| **Field usage** | a program that sets a field to `ZEROES` is a writer of that field, whatever spelling it used |
| **Width change** | `PICTURE X(30)` sites are as affected as `PIC X(30)` sites |

---

## Sources

- [Figurative constants - IBM Enterprise COBOL for z/OS](https://www.ibm.com/docs/en/cobol-zos/6.3.0?topic=literals-figurative-constants)
- [Computational data representation (USAGE clause) - IBM i](https://www.ibm.com/docs/en/i/7.4.0?topic=items-computational-data-representation-usage-clause)
- [CORRESPONDING phrase - IBM Enterprise COBOL](https://www.ibm.com/docs/en/cobol-zos/6.2?topic=operations-corresponding-phrase)
- [PICTURE clause - IBM Enterprise COBOL](https://www.ibm.com/docs/en/cobol-zos/6.4.0?topic=entry-picture-clause)
- [ILE COBOL Language Reference - IBM i](https://www.ibm.com/docs/en/ssw_ibm_i_71/rzasb/sc092539.pdf)
