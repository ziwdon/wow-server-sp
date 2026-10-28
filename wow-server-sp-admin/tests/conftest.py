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


@pytest.fixture(autouse=True)
def _reset_cloud_service():
    """Drop the process-wide CloudService between tests.

    The Backups partials and the lifespan call ``cloud_backup.get_service()``, which caches one
    instance wired to whatever ``ADMIN_DATA_DIR``/``AC_STACK_DIR`` were set at its first call; a
    cached one must not carry one test's paths into the next.
    """
    yield
    from app.services import cloud_backup

    cloud_backup._service = None
