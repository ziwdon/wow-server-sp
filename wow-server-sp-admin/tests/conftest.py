import pytest


@pytest.fixture(autouse=True)
def _reset_admin_state():
    """Reset app.state._state between tests.

    Phase B introduces a module-global in app/state.py. Without this
    fixture, a test that runs after another test's init_state() inherits
    the prior fixture's paths.
    """
    yield
    try:
        import app.state as state
        state._state = None
    except ImportError:
        # Phase B hasn't landed yet; nothing to reset.
        pass
