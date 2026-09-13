from harness.compare import compare_results


def _result(width):
    return {"fixture": "tiny.html", "elements": {"target": {
        "rect": {"x": 10, "y": 20, "width": width, "height": 40},
        "style": {},
    }}}


def test_geometry_tolerance_and_exact_delta():
    comparison = compare_results(_result(100), _result(99.4), tolerance=0.5)
    assert not comparison["passed"]
    assert comparison["geometry_mismatches"] == [{
        "element": "target", "field": "width", "chrome": 100.0,
        "ours": 99.4, "delta": -0.5999999999999943, "kind": "geometry",
    }]


def test_geometry_inside_tolerance_passes():
    assert compare_results(_result(100), _result(99.75), tolerance=0.5)["passed"]


def test_text_fragment_mismatch_fails_even_when_parent_rect_matches():
    chrome = _result(100)
    ours = _result(100)
    chrome["elements"]["target"]["fragments"] = {
        "element": [], "text": [{"x": 10, "y": 20, "width": 80, "height": 18}],
    }
    ours["elements"]["target"]["fragments"] = {
        "element": [], "text": [{"x": 10, "y": 20, "width": 60, "height": 18}],
    }
    comparison = compare_results(chrome, ours)
    assert not comparison["passed"]
    assert comparison["fragment_mismatches"][0]["field"] == "text[0].width"
