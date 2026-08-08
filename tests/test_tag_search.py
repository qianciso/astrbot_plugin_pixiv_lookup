from __future__ import annotations

import pytest
from astrbot_plugin_pixiv_lookup.tag_search import (
    BuiltinTagAliasProvider,
    parse_tag_command_args,
)


def test_parse_tag_separators_signs_and_builtin_aliases():
    query = parse_tag_command_args("黑发，碧蓝航线 +阿米娅 -R18")

    assert query.required_tags == ("黑发", "碧蓝航线")
    assert query.optional_tags == ("阿米娅",)
    assert query.excluded_tags == ("R18",)
    assert query.translated_required_tags == ("黒髪", "アズールレーン")
    assert query.translated_optional_tags == ("アーミヤ(アークナイツ)",)
    assert query.translated_excluded_tags == ("R18",)
    assert query.number == 1 and query.mode == "pick" and query.page == 1


def test_parse_missing_mode_treats_second_numeric_suffix_as_page():
    query = parse_tag_command_args("黑发 2 2")

    assert (query.number, query.mode, query.page) == (2, "pick", 2)
    assert "未填写模式参数" in query.parse_notice


def test_parse_explicit_latest_and_page():
    query = parse_tag_command_args("黑发 20 1 3")
    assert (query.number, query.mode, query.page) == (20, "latest", 3)


def test_latest_has_twenty_item_limit_but_pick_does_not():
    with pytest.raises(ValueError, match="20"):
        parse_tag_command_args("黑发 21 1")
    query = parse_tag_command_args("黑发 100 0 2")
    assert (query.number, query.mode, query.page) == (100, "pick", 2)


def test_more_than_ten_tags_are_discarded_in_input_order():
    query = parse_tag_command_args(" ".join(f"tag{i}" for i in range(1, 13)))
    assert query.required_tags == tuple(f"tag{i}" for i in range(1, 11))
    assert query.discarded_tags == ("tag11", "tag12")


def test_alias_conversion_can_be_disabled_and_provider_is_extendable():
    class Alias(BuiltinTagAliasProvider):
        def translate(self, tag: str) -> str:
            return "custom" if tag == "测试" else super().translate(tag)

    raw = parse_tag_command_args("黑发", translate_enabled=False)
    custom = parse_tag_command_args("测试", alias_provider=Alias())
    assert raw.search_word == "黑发"
    assert custom.search_word == "custom"


def test_builtin_tsv_maps_common_chinese_and_quoted_english_attributes():
    provider = BuiltinTagAliasProvider()

    assert provider.load_error is None
    assert len(provider.aliases) == 229
    assert provider.translate("棕发") == "茶髪"
    assert provider.translate("GLASSES") == "眼鏡"
    assert provider.translate("未知属性") == "未知属性"

    query = parse_tag_command_args(
        '"black hair", +"blue eyes" -"white hair"',
        alias_provider=provider,
    )
    assert query.required_tags == ("black hair",)
    assert query.optional_tags == ("blue eyes",)
    assert query.excluded_tags == ("white hair",)
    assert query.translated_required_tags == ("黒髪",)
    assert query.translated_optional_tags == ("碧眼",)
    assert query.translated_excluded_tags == ("白髪",)


def test_alias_tsv_supports_comments_bom_nfkc_and_rejects_conflicts(tmp_path):
    path = tmp_path / "aliases.tsv"
    path.write_text(
        "\ufeff# 可选注释\nalias\ttarget\nBlack Hair\t黒髪\n黑发\t黒髪\n",
        encoding="utf-8",
    )
    provider = BuiltinTagAliasProvider(path, strict=True)
    assert provider.translate("ＢＬＡＣＫ　ＨＡＩＲ") == "黒髪"

    path.write_text(
        "alias\ttarget\nBlack Hair\t黒髪\nblack hair\t白髪\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="冲突"):
        BuiltinTagAliasProvider(path, strict=True)


@pytest.mark.parametrize(
    "raw",
    ["", "-R18", "+", "!!!", "黑发 -黑发", "黑发 1 2 3 4", '"black hair'],
)
def test_invalid_tag_queries_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_tag_command_args(raw)
