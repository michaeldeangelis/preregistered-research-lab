from rlab.rules import evaluate

RULE = {"metric": "delta", "comparator": "gt", "threshold": 0.0, "ci_must_exclude": True, "min_n": 50, "null_value": 0.0}


def res(point, lo, hi, n=100, controls=None):
    return {"effects": {"delta": {"point": point, "lo": lo, "hi": hi, "n": n}}, "controls": controls or {}}


def test_pass_kill_provisional():
    assert evaluate(RULE, res(0.1, 0.05, 0.15)).verdict == "pass"
    assert evaluate(RULE, res(-0.1, -0.15, -0.02)).verdict == "kill"
    assert evaluate(RULE, res(0.0, -0.01, 0.01)).verdict == "provisional"
    assert evaluate(RULE, res(0.1, -0.01, 0.2)).verdict == "provisional"  # overclaim: point beyond, CI crosses


def test_lt_comparator():
    rule = {**RULE, "comparator": "lt"}
    assert evaluate(rule, res(-0.1, -0.2, -0.05)).verdict == "pass"
    assert evaluate(rule, res(0.1, 0.02, 0.2)).verdict == "kill"


def test_fail_closed():
    assert evaluate({}, res(1, 1, 1)).verdict == "invalid"
    assert evaluate({**RULE, "comparator": "bigger"}, res(1, 1, 1)).verdict == "invalid"
    assert evaluate(RULE, {"effects": {}}).verdict == "provisional"
    assert evaluate(RULE, res(0.1, 0.05, 0.15, n=10)).verdict == "provisional"
    assert evaluate(RULE, {"effects": {"delta": {"point": 0.1}}}).verdict == "provisional"  # CI required, missing
    assert evaluate(RULE, res(True, True, True)).verdict == "provisional"  # booleans are not numbers


def test_controls_must_be_null():
    ok = {"shuffled": {"point": 0.0, "lo": -0.02, "hi": 0.02}}
    bad = {"shuffled": {"point": 0.1, "lo": 0.05, "hi": 0.15}}
    assert evaluate(RULE, res(0.1, 0.05, 0.15, controls=ok), ["shuffled"]).verdict == "pass"
    assert evaluate(RULE, res(0.1, 0.05, 0.15, controls=bad), ["shuffled"]).verdict == "invalid"
    assert evaluate(RULE, res(0.1, 0.05, 0.15), ["shuffled"]).verdict == "invalid"


def test_no_ci_rule():
    rule = {**RULE, "ci_must_exclude": False, "min_n": 0}
    assert evaluate(rule, {"effects": {"delta": {"point": 0.2}}}).verdict == "pass"
    assert evaluate(rule, {"effects": {"delta": {"point": -0.2}}}).verdict == "kill"


def _block(point, se, n=300):
    return {"point": point, "lo": point - 1.96 * se, "hi": point + 1.96 * se, "n": n}


MRULE = {"metric": "best", "comparator": "lt", "threshold": 0.0, "ci_must_exclude": True, "min_n": 100, "null_value": 0.0}


def test_multiplicity_guard_downgrades_selected_best():
    # nominally significant (p~0.019) but best of 10 distinct effects
    effects = {"best": _block(-2.35, 1.0)} | {f"e{i}": _block(0.1 * i, 1.0) for i in range(9)}
    v = evaluate(MRULE, {"effects": effects, "controls": {}})
    assert v.verdict == "provisional" and "Bonferroni over 10" in v.reason and v.n_comparisons == 10
    # a clear effect survives the correction
    effects = {"best": _block(-4.5, 1.0)} | {f"e{i}": _block(0.1 * i, 1.0) for i in range(9)}
    assert evaluate(MRULE, {"effects": effects, "controls": {}}).verdict == "pass"


def test_alias_of_another_effect_counts_once():
    a = _block(-2.1, 1.0)
    one = {"effects": {"best": a, "copy_of_best": dict(a)}}
    assert evaluate(MRULE, one).verdict == "pass"  # an exact copy is not a second comparison
    two = {"effects": {"best": a, "other": _block(0.2, 1.0)}}
    assert evaluate(MRULE, two).verdict == "provisional"


def test_declared_comparisons_apply_even_when_only_one_is_reported():
    only = {"effects": {"best": _block(-2.35, 1.0)}}
    assert evaluate(MRULE, only).verdict == "pass"
    assert evaluate(MRULE, only, n_declared=20).verdict == "provisional"


def test_ci_free_rule_cannot_skip_multiplicity():
    rule = {**MRULE, "ci_must_exclude": False}
    assert evaluate(rule, {"effects": {"best": _block(-3, 1.0)}}, n_declared=5).verdict == "provisional"


def test_kill_reports_opposite_direction():
    v = evaluate(MRULE, {"effects": {"best": _block(5.0, 1.0)}})
    assert v.verdict == "kill" and v.opposite and "OPPOSITE" in v.reason
    v = evaluate(MRULE, {"effects": {"best": _block(0.2, 1.0)}})
    assert v.verdict == "provisional" and not v.opposite
    v = evaluate({**MRULE, "comparator": "gt"}, {"effects": {"best": _block(-5.0, 1.0)}})
    assert v.verdict == "kill" and v.opposite


NAN = float("nan")


def _r(effects, **rule):
    return {**MRULE, **rule}, {"effects": effects, "controls": {}}


def test_unhashable_or_malformed_blocks_do_not_crash():
    rule, res = _r({"best": _block(-4.5, 1.0), "diag": {"point": 1, "lo": [1], "hi": {"a": 2}, "n": [3]}})
    assert evaluate(rule, res).verdict in ("pass", "provisional")  # no TypeError
    assert evaluate(MRULE, {"effects": "oops"}).verdict == "provisional"
    assert evaluate(MRULE, {"effects": {"best": _block(-4.5, 1.0)}, "controls": []}).verdict in ("pass", "provisional")


def test_non_finite_numbers_fail_closed():
    for blk in ({"point": NAN, "lo": -2.3, "hi": -0.1, "n": 300}, {"point": -1.2, "lo": NAN, "hi": -0.1, "n": 300},
                {"point": -1.2, "lo": -2.3, "hi": float("inf"), "n": 300}):
        v = evaluate(MRULE, {"effects": {"best": blk}}, None, n_declared=3)
        assert v.verdict == "provisional", blk
    assert evaluate({**MRULE, "null_value": NAN}, {"effects": {"best": _block(-4, 1)}}).verdict == "invalid"


def test_inverted_or_zero_width_ci_is_provisional():
    for lo, hi in ((-0.5, -2.0), (-1.0, -1.0)):
        assert evaluate(MRULE, {"effects": {"best": {"point": -1.0, "lo": lo, "hi": hi, "n": 300}}}).verdict == "provisional"


def test_multiplicity_tests_the_threshold_not_the_null():
    # superiority margin: CI lower bound 0.051 clears 0.05 at n=1 but not after correction over 5
    rule = {**MRULE, "comparator": "gt", "threshold": 0.05}
    blk = {"point": 0.175, "lo": 0.051, "hi": 0.299, "n": 300}
    assert evaluate(rule, {"effects": {"best": blk}}).verdict == "pass"
    assert evaluate(rule, {"effects": {"best": blk}}, n_declared=5).verdict == "provisional"
    # non-inferiority margin below the null: a comfortable result is not penalized
    ni = {**MRULE, "comparator": "gt", "threshold": -0.1}
    blk = {"point": 0.0, "lo": -0.05, "hi": 0.05, "n": 300}
    assert evaluate(ni, {"effects": {"best": blk}}, n_declared=2).verdict == "pass"


def test_ratio_metrics_are_corrected_on_the_log_scale():
    rule = {**MRULE, "comparator": "gt", "threshold": 1.0, "null_value": 1.0}
    odds = {"point": 2.0, "lo": 1.2, "hi": 3.3, "n": 300}
    assert evaluate(rule, {"effects": {"best": odds}}, n_declared=2).verdict == "pass"
    assert evaluate(rule, {"effects": {"best": odds}}, n_declared=20).verdict == "provisional"


def test_agent_controlled_alpha_and_counts_cannot_loosen_or_crash_the_guard():
    effects = {"best": _block(-2.1, 1.0), "other": _block(0.2, 1.0)}
    for alpha in (1.0, NAN, 0, -1, None, "x", 0.5):
        assert evaluate(MRULE, {"effects": effects}, alpha=alpha).verdict == "provisional", alpha
    assert evaluate(MRULE, {"effects": {"best": _block(-4.5, 1.0)}}, n_declared=10 ** 400).verdict == "provisional"
    assert evaluate(MRULE, {"effects": {"best": _block(-4.5, 1.0)}}, n_declared=-5).verdict == "pass"
    for bad in ("3", 2.5, None, 0, True):
        evaluate(MRULE, {"effects": effects}, n_declared=bad)  # must not raise
    # a stricter alpha applies even with one comparison
    assert evaluate(MRULE, {"effects": {"best": _block(-2.5, 1.0)}}, alpha=0.001).verdict == "provisional"


def test_ci_free_rule_still_kills_but_cannot_pass_many():
    rule = {**MRULE, "ci_must_exclude": False}
    many = {"best": {"point": -3.0, "n": 300}, "other": {"point": 1.0, "n": 300}}
    assert evaluate(rule, {"effects": many}).verdict == "provisional"
    assert evaluate(rule, {"effects": {"best": {"point": 0.5, "n": 300}, "other": {"point": 1.0, "n": 300}}}).verdict == "kill"


def test_control_margin_is_preregistered_tolerance():
    res = {"effects": {"delta": {"point": 0.3, "lo": 0.2, "hi": 0.4, "n": 300}},
           "controls": {"shuf": {"point": -0.006, "lo": -0.012, "hi": -0.0012}}}  # CV-style drift below 0
    assert evaluate(RULE, res, ["shuf"]).verdict == "invalid"
    assert evaluate({**RULE, "control_margin": 0.01}, res, ["shuf"]).verdict == "pass"
    assert evaluate({**RULE, "control_margin": -5}, res, ["shuf"]).verdict == "invalid"


def test_control_margin_is_capped_by_the_kernel():
    leaky = {"effects": {"delta": {"point": 0.3, "lo": 0.25, "hi": 0.35, "n": 400}},
             "controls": {"shuf": {"point": 0.29, "lo": 0.24, "hi": 0.34}}}
    for m in (0.3, 1e9):
        assert evaluate({**RULE, "control_margin": m}, leaky, ["shuf"]).verdict == "invalid"


def test_schema_pattern_rejects_descriptions_as_control_names():
    import json
    from pathlib import Path
    from rlab.schema import validate
    sch = json.loads((Path(__file__).parent.parent / "schemas" / "protocol.json").read_text())
    sub = {"type": "object", "properties": {"negative_controls": sch["properties"]["negative_controls"]}}
    assert validate({"negative_controls": ["permuted_bmi"]}, sub) == []
    assert validate({"negative_controls": ["permuted_bmi: replay the whole pipeline"]}, sub)


def test_control_without_interval_says_so():
    res = {"effects": {"delta": {"point": 0.06, "lo": 0.04, "hi": 0.08, "n": 300}},
           "controls": {"shuf": {"point": 0.0, "lo": 0.0, "hi": 0.0}}}
    v = evaluate(RULE, res, ["shuf"])
    assert v.verdict == "invalid" and "without an interval" in v.reason
