from potato.app import PotatoApp


def test_f6_is_bound_to_copy_selected_item() -> None:
    bindings = {
        binding[0]: binding[1]
        for binding in PotatoApp.BINDINGS
    }

    assert bindings["f6"] == "begin_copy"
    assert callable(PotatoApp.action_begin_copy)


def test_f7_is_bound_to_move_selected_item() -> None:
    bindings = {
        binding[0]: binding[1]
        for binding in PotatoApp.BINDINGS
    }

    assert bindings["f7"] == "begin_move"
    assert callable(PotatoApp.action_begin_move)


def test_f8_is_bound_to_rename_selected_item() -> None:
    bindings = {
        binding[0]: binding[1]
        for binding in PotatoApp.BINDINGS
    }

    assert bindings["f8"] == "begin_rename"
    assert callable(PotatoApp.action_begin_rename)


def test_file_operation_confirmation_is_required() -> None:
    assert callable(PotatoApp._set_pending_destination)
    assert callable(PotatoApp._show_operation_confirmation)
    assert callable(PotatoApp.on_input_submitted)
    assert callable(PotatoApp._start_file_operation)
