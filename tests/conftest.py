import os

os.environ.setdefault("WA_PROVIDER", "mock")
os.environ.setdefault("STT_PROVIDER", "mock")
os.environ.setdefault("IMAGEGEN_PROVIDER", "mock")
os.environ.setdefault("INSTAGRAM_MOCK", "true")
os.environ.setdefault("WA_VERIFY_TOKEN", "test-token")
os.environ.setdefault("R2_PUBLIC_BASE_URL", "https://pub-test.r2.dev")
# Fonts are vendored (templates/fonts/), so the suite runs with the production
# guard ON: a face that fails to load refuses the render, here as there.
os.environ.setdefault("COMPOSE_REQUIRE_FONTS", "true")
