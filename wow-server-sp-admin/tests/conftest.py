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


@pytest.fixture(autouse=True)
def _reset_armory_proxy():
    """Drop the process-wide armory AssetProxy and DisplayIdResolver between tests.

    ``armory_assets.get_proxy()`` builds one instance from ``ADMIN_DATA_DIR`` at its first call,
    and ``armory_resolver.get_resolver()`` one on top of it (with its in-memory display-ID map);
    like the cloud service above, a cached one must not carry one test's paths into the next.
    """
    yield
    from app.services import armory_assets, armory_resolver

    armory_resolver.reset_resolver()
    armory_assets.reset_proxy()


@pytest.fixture(autouse=True)
def _reset_dbc_store():
    """Drop the process-wide armory ``DbcStore`` between tests.

    ``dbc.store_from_env()`` caches one store over whatever ``CLIENT_DATA_DIR`` was set at its
    first call (and the lifespan's warm-up thread loads it); a cached one must not carry one
    test's reference data into the next.
    """
    yield
    from app.services import dbc

    dbc.reset_store()
