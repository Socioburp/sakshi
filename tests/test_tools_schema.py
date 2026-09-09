from app.agent.tools import BRIEF_SCHEMA, TOOLS


def _walk(node, seen):
    if isinstance(node, dict):
        seen.update(node.keys())
        for v in node.values():
            _walk(v, seen)
    elif isinstance(node, list):
        for v in node:
            _walk(v, seen)


def test_brief_schema_has_no_refs():
    keys = set()
    _walk(BRIEF_SCHEMA, keys)
    assert "$ref" not in keys and "$defs" not in keys


def test_brief_schema_covers_the_contract():
    props = BRIEF_SCHEMA["properties"]
    for field in (
        "intent",
        "format",
        "headline",
        "subhead",
        "cta",
        "visual_direction",
        "template_id",
        "caption",
        "alt_text",
        "grounding",
        "slides",
    ):
        assert field in props, f"missing {field}"
    assert "prompt" in props["visual_direction"]["properties"]
    assert "slide_count" in props["format"]["properties"]


def test_tool_names_unique_and_documented():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names))
    for t in TOOLS:
        assert len(t["description"]) > 40
        assert t["input_schema"]["type"] == "object"


def test_revise_is_advertised_as_free():
    revise = next(t for t in TOOLS if t["name"] == "revise_creative")
    assert "FREE" in revise["description"]


def test_carousel_is_reachable_from_the_tool_surface():
    create = next(t for t in TOOLS if t["name"] == "create_creative")
    fmt = create["input_schema"]["properties"]["brief"]["properties"]["format"]
    assert set(fmt["properties"]["type"]["enum"]) == {"single", "carousel"}
    regen = next(t for t in TOOLS if t["name"] == "regenerate_image")
    assert "slide_position" in regen["input_schema"]["properties"]
