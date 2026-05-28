from mnemos.domain.knemon.router import _normalize_capabilities


def test_json_object_capabilities_enable_truthy_keys():
    assert _normalize_capabilities('{"chat":true,"coding":true,"reasoning":false}') == {"chat", "coding"}
