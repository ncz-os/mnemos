"""MariaDB keeps vectors in a separate table, so its journal triggers must not
compare an ``embedding`` column on ``memories``."""

import inspect
import re

from mnemos.persistence import mariadb


def test_mariadb_open_provisions_journal_for_separate_embeddings():
    source = inspect.getsource(mariadb)
    calls = re.findall(r"_ensure_mysql_federation_journal\(([^)]*)\)", source)
    assert calls, "MariaDB backend must provision the federation journal"
    assert all("separate_embeddings=True" in call for call in calls)
