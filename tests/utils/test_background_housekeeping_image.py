from unittest.mock import patch

from iac_code.utils.background_housekeeping import _run_image_cleanup


def test_image_cleanup_invokes_helper():
    with patch("iac_code.utils.image.store.cleanup_old_image_caches") as mock_cleanup:
        _run_image_cleanup(current_session_id="abc", delay_seconds=0)
    mock_cleanup.assert_called_once_with(current_session_id="abc")


def test_start_background_housekeeping_with_session_id():
    """Smoke test: passing session_id starts BOTH cleanup threads (tool-results and image cache)."""
    from iac_code.utils.background_housekeeping import start_background_housekeeping

    threads = start_background_housekeeping(
        base_dir="/tmp/iac-bg-housekeeping-test",
        delay_seconds=0,
        session_id="abc",
    )
    assert isinstance(threads, tuple)
    assert len(threads) == 2
    for t in threads:
        t.join(timeout=5)


def test_start_background_housekeeping_without_session_id_returns_single_tuple():
    """Without session_id only the tool-result cleanup thread is started."""
    from iac_code.utils.background_housekeeping import start_background_housekeeping

    threads = start_background_housekeeping(
        base_dir="/tmp/iac-bg-housekeeping-test",
        delay_seconds=0,
    )
    assert isinstance(threads, tuple)
    assert len(threads) == 1
    threads[0].join(timeout=5)
