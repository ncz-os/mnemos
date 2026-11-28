"""A native UUID key id must stamp, serialise and sign like its string form."""

import json
import uuid

from mnemos.core.provenance import PROVENANCE_KEY, stamp_provenance


def test_uuid_key_id_is_stamped_as_a_string_and_serialises():
    key_id = uuid.uuid4()
    meta = stamp_provenance({}, principal="alice", key_id=key_id, sign_key="")
    assert meta[PROVENANCE_KEY]["key_id"] == str(key_id)
    json.dumps(meta)


def test_uuid_key_id_signs_the_same_as_its_string_form():
    key_id = uuid.uuid4()
    as_uuid = stamp_provenance({}, principal="alice", key_id=key_id, sign_key="k")
    as_text = stamp_provenance({}, principal="alice", key_id=str(key_id), sign_key="k")
    assert as_uuid[PROVENANCE_KEY]["key_id"] == as_text[PROVENANCE_KEY]["key_id"]
    assert as_uuid[PROVENANCE_KEY]["signed"] is True
    json.dumps(as_uuid)
