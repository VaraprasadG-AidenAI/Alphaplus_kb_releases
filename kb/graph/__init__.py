"""
Neo4j behind the same answers.

    kb.graph.settings   alphaplus.toml + environment, validated
    kb.graph.driver     one pooled driver, health probe
    kb.graph.model      the map from 23 SQLite tables to a property graph
    kb.graph.schema     constraints and indexes - run before the first load
    kb.graph.load       facts.db -> Neo4j, batched so the heap cannot fill
    kb.graph.queries    Cypher for the tools that map cleanly

Order matters once:  schema, then load.  A load without the uniqueness
constraints degrades every MERGE to a label scan and goes quadratic.

Nothing here is imported by kb/server/tools.py unless
`graph.backend` is `neo4j` or `compare`, so a checkout with no Neo4j and no
driver installed behaves exactly as it did before.
"""
