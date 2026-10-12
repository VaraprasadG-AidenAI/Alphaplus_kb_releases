"""Business process flows, discovered from the call graph.

The knowledge base already answers "what calls what". This package answers a
different question: *what business process is this, and where does it start*.

The distinction was put plainly in review: "you are not processing the data
here, you are visualizing the data here". A dependency graph rendered faithfully
is still a photograph of the source. What is wanted is the estate reorganised
into named processes that a ticket can be routed to.

Three tiers, and they are not invented - each comes from a fact in the tree:

    capability   a menu family (ORDMEN01..09 -> ORD)         9 of them
    feature      one menu option, with the label it carries  ~1,100
    flow         the call chain under that option, to depth 3

Every node keeps its technical identity as a property and carries an inferred
business title as its headline. The inference can be wrong; it is marked as
inferred, and a reviewer corrects it. What is not acceptable is a screen whose
headline is `AGD100C`.
"""
