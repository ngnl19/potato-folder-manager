from potato.app import PotatoApp, SearchCompleted


def test_search_completed_stores_its_payload() -> None:
    message = SearchCompleted(
        request_id=17,
        query="notes",
        results=[],
        error="example error",
    )

    assert message.request_id == 17
    assert message.query == "notes"
    assert message.results == []
    assert message.error == "example error"


def test_f2_binding_has_an_implemented_action() -> None:
    assert any(
        binding[0] == "f3" and binding[1] == "toggle_hidden"
        for binding in PotatoApp.BINDINGS
    )

    assert callable(
        getattr(PotatoApp, "action_toggle_hidden", None)
    )


def test_f3_binding_has_an_implemented_action() -> None:
    assert any(
        binding[0] == "f4" and binding[1] == "cycle_sort"
        for binding in PotatoApp.BINDINGS
    )

    assert callable(
        getattr(PotatoApp, "action_cycle_sort", None)
    )
