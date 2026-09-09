import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "bakeoff", Path(__file__).resolve().parents[1] / "scripts" / "stt_bakeoff.py"
)
bakeoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bakeoff)


def test_exact_and_fuzzy_product_name_matching():
    hyp = "kal se sale hai nandhini ghee 500 ml two forty nine"
    assert bakeoff.caught("Nandini", hyp)
    assert bakeoff.caught("500 ml", hyp)
    assert not bakeoff.caught("Amul", hyp)


def test_wer_is_sane():
    assert bakeoff.wer("a b c", "a b c") == 0.0
    assert bakeoff.wer("a b c", "a b") > 0
