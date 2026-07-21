from pathlib import Path


SERVICE_WORKER = Path(__file__).parents[1] / "templates" / "service-worker.js"


def test_direct_media_navigations_bypass_offline_homepage_fallback():
    script = SERVICE_WORKER.read_text()

    bypass_position = script.index('request.mode === "navigate" && isMediaNavigation')
    navigation_handler_position = script.index('if (request.mode === "navigate")')

    assert bypass_position < navigation_handler_position
    assert '["/view/", "/images/", "/thumb/"]' in script
    assert 'if (request.mode === "navigate" && isMediaNavigation) return;' in script
