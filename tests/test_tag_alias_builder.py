from __future__ import annotations

import pytest
from astrbot_plugin_pixiv_lookup.tag_search import BuiltinTagAliasProvider

from scripts.build_tag_aliases import (
    SELECTED_TARGETS,
    build_rows,
    is_supported_alias,
    write_tsv,
)


def make_source():
    targets = [
        {"name": name, "synonyms": []}
        for name in SELECTED_TARGETS
    ]
    by_name = {item["name"]: item for item in targets}
    by_name["黒髪"]["synonyms"] = [
        {"name": "黑发"},
        {"name": "black hair"},
        {"name": "くろかみ"},
    ]
    by_name["ドレス"]["synonyms"] = [
        {"name": "dress"},
        {"name": "スカート"},
        {"name": "裙子"},
    ]
    return {"version": 1, "targets": targets}


def test_builder_selects_supported_aliases_and_applies_manual_filters(tmp_path):
    rows, conflicts = build_rows(make_source())
    mapping = dict(rows)

    assert conflicts == []
    assert mapping["黑发"] == "黒髪"
    assert mapping["black hair"] == "黒髪"
    assert "くろかみ" not in mapping
    assert mapping["dress"] == "ドレス"
    assert "スカート" not in mapping
    assert "裙子" not in mapping
    assert mapping["棕发"] == "茶髪"
    assert mapping["碧蓝航线"] == "アズールレーン"

    output = tmp_path / "aliases.tsv"
    write_tsv(output, rows)
    provider = BuiltinTagAliasProvider(output, strict=True)
    assert provider.translate("BLACK HAIR") == "黒髪"


def test_builder_rejects_missing_selected_target():
    source = make_source()
    source["targets"] = source["targets"][:-1]
    with pytest.raises(ValueError, match="缺少"):
        build_rows(source)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("black hair", True), ("黑发", True), ("ねこみみ", False), ("고양이", False)],
)
def test_builder_only_keeps_chinese_or_english_aliases(value, expected):
    assert is_supported_alias(value) is expected
