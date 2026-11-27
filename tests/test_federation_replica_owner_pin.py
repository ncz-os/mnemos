"""Pin: federation replica owner literal.

Standalone source-reading check, kept out of the MCP NATS SSE bridge suite
(tests/test_mcp_nats_sse.py) because it is unrelated to the bridge: it
asserts against the source tree of the persistence backends and therefore
needs a source layout.

A federated-memory INSERT stamps the row's owner_id with the literal
'federation' in every backend that carries its own INSERT. Update or
delete this file together with any change to that literal.
"""

import re
from pathlib import Path


def test_pin_federation_replica_owner_literal():
    """Every backend with its own federated INSERT carries the literal owner."""
    root = Path(__file__).resolve().parent.parent / "mnemos" / "persistence"
    pattern = re.compile(r"owner_id[\s\S]{0,400}?'federation'")
    backends = ["postgres.py", "sqlite.py", "mysql.py", "oracle.py", "db2.py"]
    literal_owners = []
    for name in backends:
        source = (root / name).read_text(encoding="utf-8")
        if "'federation'" in source and "federation_source" in source:
            if pattern.search(source):
                literal_owners.append(name)
    # The shipped behaviour: the federated-memory INSERT pins the owner.
    # Every backend with its own federated INSERT must carry the literal;
    # mariadb.py is exempt by construction (subclasses MysqlBackend and
    # inherits its INSERT, so it carries no literal of its own).
    assert sorted(literal_owners) == sorted(backends), f"missing literal in: {set(backends) - set(literal_owners)}"
