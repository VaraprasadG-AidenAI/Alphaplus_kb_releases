"""
The ticket benchmark.

Forty tickets in four bands, each with a gold answer.  This is the only thing
that will tell us whether the knowledge base is worth having, so the design
matters more than the count.

Three deliberate choices:

  * **Vague tickets outnumber precise ones.**  A ticket that names the program
    is one an experienced developer already knows how to handle.  The ones
    that cost a day are the ones written in business language, and those are
    where a knowledge base either earns its place or does not.

  * **Five tickets are misleading.**  They blame the wrong subsystem, because
    real tickets do.  An agent that follows the ticket's own framing off a
    cliff should be penalised for it.

  * **Five tickets are unanswerable from source.**  The right answer is "I
    cannot tell you this from the code".  A system that invents an answer for
    those is worse than no system, and nothing else in the benchmark measures
    that.

Gold answers name the members that must appear in the agent's shortlist.
`fix_site` is the single member where the change actually belongs, when there
is one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

PRECISE = "precise"
VAGUE = "vague"
MISLEADING = "misleading"
UNANSWERABLE = "unanswerable"


@dataclass
class Ticket:
    id: str
    band: str
    summary: str
    detail: str
    #: members that must appear in the agent's answer
    gold: List[str] = field(default_factory=list)
    #: the one member where the change belongs, if there is one
    fix_site: str = ""
    #: what the agent should conclude when nothing in the source can answer it
    expect_cannot_answer: bool = False
    #: why this ticket is here, for the scorecard
    tests: str = ""

    @property
    def prompt(self) -> str:
        return (f"JIRA {self.id}\n\n{self.summary}\n\n{self.detail}\n\n"
                f"Which source members are involved, and where would the "
                f"change go? Answer with member names.")


TICKETS: List[Ticket] = [

    # =======================================================================
    # PRECISE - the ticket names an identifier
    # =======================================================================

    Ticket("APD-1001", PRECISE,
           "ORD400 shows the wrong order date for company 002",
           "The order backlog report prints a date that is a century out for "
           "some orders. Other reports show the same orders correctly.",
           gold=["ORD400", "ORDHDR", "SYS100"], fix_site="ORD400",
           tests="dual date fields - ORDDAT six digit vs ORDDTC eight digit"),

    Ticket("APD-1002", PRECISE,
           "Add the credit agency score to the customer enquiry screen",
           "Credit control want CUSCRA visible when they look an account up.",
           gold=["CUSMAS", "CUSREC"], fix_site="",
           tests="field to screen - needs the file, the copybook and a screen"),

    Ticket("APD-1003", PRECISE,
           "ORD100 fails with a record lock at about 02:00",
           "Order entry occasionally fails overnight. The joblog shows a lock "
           "on ORDHDR. It clears if the user retries.",
           gold=["ORD100", "ORDHDR", "ALO100"], fix_site="ORD100",
           tests="lock retry logic and what else holds the file overnight"),

    Ticket("APD-1004", PRECISE,
           "What breaks if we widen CUSNAM from 30 to 40 characters?",
           "Sales want longer customer names. We need to know the blast "
           "radius before we agree to it.",
           gold=["CUSMAS", "CUSREC", "CUSREC2", "CUSRECN"], fix_site="",
           tests="impact analysis across the three drifted copybook generations"),

    Ticket("APD-1005", PRECISE,
           "Option 7 on the reports menu does nothing",
           "Selecting it returns to the menu with no message.",
           gold=["RPTMENU"], fix_site="RPTMENU",
           tests="a menu option pointing at a program that does not exist"),

    Ticket("APD-1006", PRECISE,
           "ORD100 abends writing the DETAIL format",
           "Order entry fails immediately on entry with a format not found "
           "error.",
           gold=["ORD100", "ORD100D"], fix_site="ORD100",
           tests="FORMAT IS naming a record format the display file lacks"),

    Ticket("APD-1007", PRECISE,
           "Which programs update CUSMAS?",
           "We are changing how the customer master is maintained and need "
           "the full list of programs that write to it.",
           gold=["CUSMAS", "CUS200"], fix_site="",
           tests="the CRUD matrix - grep cannot separate readers from writers"),

    Ticket("APD-1008", PRECISE,
           "Which physical file does ORD100 actually open?",
           "We want to point order entry at the archive library for a test "
           "and need to know what it opens and how.",
           gold=["ORD100", "ORD100C", "ORDHDR"], fix_site="ORD100C",
           tests="OVRDBF resolution - invisible to text search"),

    Ticket("APD-1009", PRECISE,
           "SYS100 needs a new function code for week numbers",
           "Finance want a week number from a date. Before we touch SYS100 "
           "we need to know how widely it is called.",
           gold=["SYS100"], fix_site="SYS100",
           tests="reverse call graph on the most called program"),

    Ticket("APD-1010", PRECISE,
           "PRCSRV signature change for the new promotion type",
           "We need to add a procedure. What is the safe way to change the "
           "binder source?",
           gold=["PRCSRV"], fix_site="PRCSRV",
           tests="binder language - export order is positional"),

    Ticket("APD-1011", PRECISE,
           "ALO100 leaves allocations behind when it fails part way",
           "If the overnight allocation run fails, some order lines show as "
           "allocated but the stock is not reserved.",
           gold=["ALO100", "ORDALO", "ITMWHS"], fix_site="ALO100",
           tests="commitment control boundary"),

    Ticket("APD-1012", PRECISE,
           "FIN400 cannot be restarted after a failure",
           "The GL posting run failed at 03:00 and the restart posted some "
           "transactions twice.",
           gold=["FIN400", "GLINTF"], fix_site="FIN400",
           tests="checkpoint and restart logic"),

    Ticket("APD-1013", PRECISE,
           "CUS200 does not write an audit record when an account is closed",
           "Every other change to a customer is audited. Closing one is not.",
           gold=["CUS200", "AUDTRL", "CUSMAS"], fix_site="CUS200",
           tests="a missing path in an era-1 maintenance program"),

    Ticket("APD-1014", PRECISE,
           "Where is the six digit to eight digit date conversion done?",
           "We are planning the 2040 window problem and need every place a "
           "two digit year is expanded.",
           gold=["SYS100", "Y2KDATE"], fix_site="",
           tests="finding a cross-cutting concern by name"),

    Ticket("APD-1015", PRECISE,
           "ORD900 - is this still used?",
           "It appears in the build list. Nobody knows what it does.",
           gold=["ORD900"], fix_site="",
           tests="reachability - is anything calling it"),

    # =======================================================================
    # VAGUE - business language, no identifier anywhere
    # =======================================================================

    Ticket("APD-2001", VAGUE,
           "Irish customers are being billed the wrong VAT",
           "Invoices for the Dublin operation show UK VAT rates. Started "
           "after the last release.",
           gold=["TAXRAT", "IVCHDR"], fix_site="",
           tests="business words to code - Irish is company 002, VAT is TAXRAT"),

    Ticket("APD-2002", VAGUE,
           "Customers on stop are still getting orders taken",
           "Credit control put an account on stop and orders continued to be "
           "accepted for two days.",
           gold=["CUSMAS", "CUSCRD"], fix_site="",
           tests="a status code with business meaning behind it"),

    Ticket("APD-2003", VAGUE,
           "The overnight run is finishing two hours later than it used to",
           "It used to finish by 04:00 and is now running past 06:00, which "
           "delays the depots.",
           gold=["EODMAIN"], fix_site="",
           tests="finding the batch cycle from a description of it"),

    Ticket("APD-2004", VAGUE,
           "Back orders are not being released when stock arrives",
           "Goods are received and the outstanding orders stay unallocated "
           "until somebody runs allocation by hand.",
           gold=["ORDBOR", "ALO100"], fix_site="",
           tests="a business process spanning two subsystems"),

    Ticket("APD-2005", VAGUE,
           "Contract customers who order in bulk get charged too much",
           "A customer with a negotiated price is being charged the quantity "
           "break price instead, which is higher.",
           gold=["PRC100", "PRCCALC", "PRCCON", "PRCQTY"], fix_site="",
           tests="two implementations of the same rule that have drifted"),

    Ticket("APD-2006", VAGUE,
           "Statements are going to the wrong address",
           "Some customers get their statement at the delivery address "
           "rather than the invoice address.",
           gold=["CUSMAS", "CUSADR"], fix_site="",
           tests="two address files and which one is the right one"),

    Ticket("APD-2007", VAGUE,
           "We cannot tell which warehouse an old order was picked from",
           "Orders before 2009 do not show a warehouse anywhere.",
           gold=["ORDHDR", "ITMWHS"], fix_site="",
           tests="a field added in a later era, absent from older rows"),

    Ticket("APD-2008", VAGUE,
           "Stock figures on the enquiry screen do not match the report",
           "The screen shows free stock and the report shows a different "
           "number for the same item.",
           gold=["ITMWHS", "ITMMAS"], fix_site="",
           tests="quantity fields that mean different things"),

    Ticket("APD-2009", VAGUE,
           "Settlement discount is being allowed after the due date",
           "Customers are taking the early payment discount weeks late and "
           "the system accepts it.",
           gold=["ARTRAN", "CUSCRD"], fix_site="",
           tests="a date comparison rule in the finance subsystem"),

    Ticket("APD-2010", VAGUE,
           "Invoice totals disagree with the sum of the lines by a penny",
           "Only on euro invoices. Sterling ones are fine.",
           gold=["IVCHDR", "IVCDTL", "CURRAT"], fix_site="",
           tests="rounding, and the two multi currency mechanisms"),

    Ticket("APD-2011", VAGUE,
           "Nobody can tell us what happens when an order is cancelled",
           "We need the sequence of events and which files change.",
           gold=["ORDHDR", "ORDDTL", "ORDALO"], fix_site="",
           tests="explaining a process rather than finding a defect"),

    Ticket("APD-2012", VAGUE,
           "Pick lists are printing items in the wrong order",
           "Pickers are walking the warehouse twice because the sequence is "
           "wrong.",
           gold=["PCKDTL", "ITMWHS"], fix_site="",
           tests="a sequencing field on the stock file"),

    Ticket("APD-2013", VAGUE,
           "The audit trail has stopped recording who changed a credit limit",
           "Changes are being made and the audit shows nothing.",
           gold=["AUDTRL", "CUSCRD"], fix_site="",
           tests="which programs write the audit file"),

    Ticket("APD-2014", VAGUE,
           "Orders taken through the website are not appearing on the "
           "backlog report",
           "Telephone orders show, web orders do not.",
           gold=["ORDHDR", "ORD400"], fix_site="",
           tests="an era-5 field an era-1 report does not select on"),

    Ticket("APD-2015", VAGUE,
           "We need to know everything that would change if we stopped using "
           "the old six digit dates",
           "Planning work for the 2040 problem.",
           gold=["ORDHDR", "SYS100", "Y2KDATE"], fix_site="",
           tests="the largest impact question in the codebase"),

    # =======================================================================
    # MISLEADING - the ticket blames the wrong thing
    # =======================================================================

    Ticket("APD-3001", MISLEADING,
           "Bug in the invoice print program - wrong customer name",
           "The invoice shows a customer name that is out of date. Please fix "
           "the print program.",
           gold=["ORDHDR", "CUSMAS"], fix_site="",
           tests="the name is snapshotted on the order, not read at print time"),

    Ticket("APD-3002", MISLEADING,
           "The customer master file is corrupt",
           "Balances are wrong for about forty accounts. The file must be "
           "damaged.",
           gold=["CUSMAS", "ARTRAN"], fix_site="",
           tests="a derived total disagreeing with its source, not corruption"),

    Ticket("APD-3003", MISLEADING,
           "Order entry screen is too slow - needs a faster display file",
           "Users say the screen takes seconds to paint. Please optimise "
           "ORD100D.",
           gold=["ORD100", "ORD100D"], fix_site="ORD100",
           tests="the cost is in the program's subfile load, not the DDS"),

    Ticket("APD-3004", MISLEADING,
           "Remove the unused field CUSFAX from the customer master",
           "Nobody has used a fax since 2008. Delete the field.",
           gold=["CUSMAS", "CUSREC", "CUSREC2", "CUSRECN"], fix_site="",
           tests="deleting a field changes the record length and recompiles "
                 "everything - the answer is do not"),

    Ticket("APD-3005", MISLEADING,
           "The allocation program is ignoring our new warehouse",
           "We opened Rotterdam and allocation will not use it. The "
           "allocation program must be hard coded.",
           gold=["ALO100", "ITMWHS", "WHSMAS"], fix_site="",
           tests="the constraint is in the stock file, not the program"),

    # =======================================================================
    # UNANSWERABLE - the right answer is "not from the source"
    # =======================================================================

    Ticket("APD-4001", UNANSWERABLE,
           "Which job schedule entry runs the overnight cycle?",
           "We want to move it half an hour later.",
           gold=[], expect_cannot_answer=True,
           tests="job schedule entries are not in source - a stated blind spot"),

    Ticket("APD-4002", UNANSWERABLE,
           "Is the ORD100 object in production compiled from this source?",
           "We suspect somebody patched the object directly in 2019.",
           gold=[], expect_cannot_answer=True,
           tests="source cannot tell you what was compiled"),

    Ticket("APD-4003", UNANSWERABLE,
           "How many orders did we take last month?",
           "Needed for the board pack.",
           gold=[], expect_cannot_answer=True,
           tests="a data question, not a code question"),

    Ticket("APD-4004", UNANSWERABLE,
           "Who authorised the change that broke the aged debt report?",
           "We need the approval record.",
           gold=[], expect_cannot_answer=True,
           tests="change approval is not in source"),

    Ticket("APD-4005", UNANSWERABLE,
           "Which users are authorised to release orders from credit hold?",
           "Audit have asked for the list.",
           gold=[], expect_cannot_answer=True,
           tests="authority is data in SECFIL plus system authority, not code"),
]


BY_BAND: Dict[str, List[Ticket]] = {}
for _t in TICKETS:
    BY_BAND.setdefault(_t.band, []).append(_t)

BY_ID: Dict[str, Ticket] = {t.id: t for t in TICKETS}


def summary() -> str:
    lines = [f"{len(TICKETS)} tickets"]
    for band in (PRECISE, VAGUE, MISLEADING, UNANSWERABLE):
        items = BY_BAND.get(band, [])
        lines.append(f"  {band:<14}{len(items):>3}")
    gold_members = {m for t in TICKETS for m in t.gold}
    lines.append(f"  {len(gold_members)} distinct members named in gold answers")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary())
    print()
    for ticket in TICKETS[:2]:
        print("-" * 70)
        print(ticket.prompt)
        print(f"   gold: {ticket.gold}   tests: {ticket.tests}")
