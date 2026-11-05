"""任务复杂度判定：规则口径、阈值可配与边界。"""

from __future__ import annotations

from sortie_deck.complexity import (
    ComplexityLimits,
    estimate_complexity,
    module_of,
    modules_of,
)


def test_module_of_strips_repo_prefix_and_aux():
    """任务沙盒的 repos/<name>/ 前缀不算模块；测试与文档归为 aux。"""
    assert module_of("repos/snake/src/server/api.js") == "src/server"
    assert module_of("src/game/engine.js") == "src/game"
    # 文件直接躺在目录下时按目录算模块，避免同目录两文件被判成跨模块
    assert module_of("src/a.js") == "src"
    assert module_of("public/js/app.js") == "public"
    assert module_of("tests/test_a.py") == "aux"
    # 根级配置文件不构成模块
    assert module_of("package.json") == "aux"
    assert modules_of(["tests/a.py", "docs/b.md"]) == []


def test_simple_small_single_module():
    report = estimate_complexity(
        changed_files=["src/server/a.js", "src/server/b.js"],
        diff_added=30,
        diff_removed=10,
    )

    assert report.level == "simple"
    assert report.signals["files"] == 2
    assert report.signals["diff_lines"] == 40
    assert report.signals["modules"] == ["src/server"]
    assert report.score < 0.5


def test_standard_medium_change():
    files = [f"src/server/f{index}.js" for index in range(6)]
    report = estimate_complexity(changed_files=files, diff_added=300)

    assert report.level == "standard"
    assert report.signals["files"] == 6


def test_complex_by_diff_size():
    """> 1500 行判复杂（1500 及以下属于中等）。"""
    report = estimate_complexity(changed_files=["src/server/a.js"], diff_added=1501)

    assert report.level == "complex"
    assert report.signals["diff_lines"] == 1501


def test_threshold_boundaries():
    """边界值：3 文件/500 行仍是简单，10 文件/1500 行是中等，超过才复杂。"""
    simple = estimate_complexity(
        changed_files=["src/a.js", "src/b.js", "src/c.js"], diff_added=500
    )
    assert simple.level == "simple"
    assert simple.signals["diff_lines"] == 500

    ten_files = [f"src/a{i}.js" for i in range(10)]
    standard = estimate_complexity(changed_files=ten_files, diff_added=1500)
    assert standard.level == "standard"
    assert standard.signals["files"] == 10

    complex_by_files = estimate_complexity(changed_files=[f"src/a{i}.js" for i in range(11)], diff_added=10)
    assert complex_by_files.level == "complex"


def test_complex_by_module_spread():
    report = estimate_complexity(
        changed_files=["src/server/a.js", "src/game/b.js", "public/js/c.js"],
        diff_added=50,
    )

    assert report.level == "complex"
    assert len(report.signals["modules"]) == 3


def test_high_risk_keyword_forces_complex():
    """再小的改动，命中支付/权限这类高风险词也不能省自审。"""
    report = estimate_complexity(
        changed_files=["src/server/a.js"],
        diff_added=10,
        brief="给支付流程加回调",
    )

    assert report.level == "complex"
    assert "支付" in report.signals["high_risk_hits"]


def test_english_keyword_uses_word_boundary():
    """auth 不能命中 author。"""
    no_hit = estimate_complexity(
        changed_files=["src/author.js"], diff_added=5, brief="add author field"
    )
    assert no_hit.signals["high_risk_hits"] == []

    hit = estimate_complexity(
        changed_files=["src/login.js"], diff_added=5, brief="add oauth login"
    )
    assert "oauth" in hit.signals["high_risk_hits"]


def test_limits_are_configurable():
    files = ["src/server/a.js", "src/server/b.js"]
    strict = ComplexityLimits(
        simple_max_files=1,
        simple_max_diff_lines=10,
        complex_min_diff_lines=1000,
        complex_min_modules=9,
        high_risk_keywords=(),
    )
    assert estimate_complexity(changed_files=files, diff_added=5, limits=strict).level == "standard"

    loose = ComplexityLimits(
        simple_max_files=10,
        simple_max_diff_lines=100,
        complex_min_diff_lines=1000,
        complex_min_modules=9,
        high_risk_keywords=(),
    )
    assert estimate_complexity(changed_files=files, diff_added=5, limits=loose).level == "simple"


def test_no_changes_is_simple():
    """没有改动就没有可审对象，按 simple 处理（走老逻辑）。"""
    assert estimate_complexity(changed_files=[]).level == "simple"
