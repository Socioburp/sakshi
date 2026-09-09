import io

from PIL import Image, ImageDraw

from app.creative.logo import extract_palette

GREEN = (24, 92, 62)
ORANGE = (228, 87, 46)


def _png(draw_fn, size=(400, 400), bg=(255, 255, 255, 0)):
    img = Image.new("RGBA", size, bg)
    draw_fn(ImageDraw.Draw(img))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _rgb(hex_str):
    return tuple(int(hex_str[i : i + 2], 16) for i in (1, 3, 5))


def _close(a, b, tol=8):
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def test_primary_colour_comes_from_the_pixels():
    data = _png(lambda d: d.ellipse([40, 40, 360, 360], fill=(*GREEN, 255)))
    a = extract_palette(data)
    assert _close(_rgb(a.palette["primary"]), GREEN), a.palette


def test_accent_is_a_different_hue_not_a_shade():
    def draw(d):
        d.ellipse([40, 40, 360, 360], fill=(*GREEN, 255))
        d.rectangle([170, 150, 230, 320], fill=(*ORANGE, 255))

    a = extract_palette(_png(draw))
    assert _close(_rgb(a.palette["primary"]), GREEN)
    assert _close(_rgb(a.palette["accent"]), ORANGE)


def test_ink_is_chosen_for_contrast():
    dark = extract_palette(_png(lambda d: d.rectangle([0, 0, 400, 400], fill=(16, 24, 40, 255))))
    light = extract_palette(
        _png(lambda d: d.rectangle([0, 0, 400, 400], fill=(250, 214, 80, 255)))
    )
    assert dark.palette["ink"] == "#FFFFFF"
    assert light.palette["ink"] == "#111111"


def test_transparency_is_detected():
    transparent = extract_palette(_png(lambda d: d.ellipse([40, 40, 360, 360], fill=(*GREEN, 255))))
    opaque = extract_palette(
        _png(lambda d: d.ellipse([40, 40, 360, 360], fill=(*GREEN, 255)), bg=(255, 255, 255, 255))
    )
    assert transparent.has_transparency is True
    assert opaque.has_transparency is False


def test_monochrome_logo_does_not_produce_a_muddy_palette():
    """A black-on-white mark is a real answer, not a failure to detect colour."""
    a = extract_palette(
        _png(
            lambda d: d.rectangle([80, 80, 320, 320], fill=(0, 0, 0, 255)),
            bg=(255, 255, 255, 255),
        )
    )
    assert a.palette["primary"] == "#111111"
    assert a.palette["ink"] == "#FFFFFF"


def test_prose_summary_mentions_measured_colours():
    a = extract_palette(_png(lambda d: d.ellipse([40, 40, 360, 360], fill=(*GREEN, 255))))
    assert a.palette["primary"] in a.as_prose("Kadamba")
