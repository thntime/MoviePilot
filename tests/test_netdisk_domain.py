"""
网盘领域模型与分享链接解析的单元测试。
"""

import pytest

from app.domain.netdisk import (
    DISPLAY_ONLY_PLATFORMS,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    is_share_level_error,
    normalize_platform,
    parse_share_link,
)


def test_parse_quark_share_with_password_in_text():
    """
    夸克链接与文案中的提取码都能解析。
    """
    ref = parse_share_link("分享给你 https://pan.quark.cn/s/abc123 提取码：x1y2")
    assert ref is not None
    assert ref.platform is NetdiskPlatform.QUARK
    assert ref.share_id == "abc123"
    assert ref.password == "x1y2"


def test_parse_uc_and_xunlei_and_guangya():
    """
    UC、迅雷与光鸭链接识别到正确平台。
    """
    assert parse_share_link("https://drive.uc.cn/s/def456").platform is NetdiskPlatform.UC
    assert parse_share_link("https://pan.xunlei.com/s/xl123?pwd=9999").platform is NetdiskPlatform.XUNLEI
    assert parse_share_link("https://www.guangyapan.com/s/gy123").platform is NetdiskPlatform.GUANGYA


def test_parse_baidu_strips_leading_one():
    """
    百度分享码去掉开头的 1。
    """
    ref = parse_share_link("https://pan.baidu.com/s/1AbCdEf?pwd=1234")
    assert ref is not None
    assert ref.share_id == "AbCdEf"
    assert ref.password == "1234"


def test_parse_139_and_123_and_115():
    """
    139、123 与 115 的链接形态都能识别。
    """
    c139 = parse_share_link("https://yun.139.com/shareweb/#/w/i/xyz789")
    assert c139 is not None and c139.platform is NetdiskPlatform.PAN139
    pan123 = parse_share_link("https://www.123pan.com/s/2785Vv-T4Ded")
    assert pan123 is not None and pan123.share_id == "2785Vv-T4Ded"
    pan115 = parse_share_link("https://115.com/s/swabc123?password=abcd")
    assert pan115 is not None and pan115.password == "abcd"


def test_parse_123_other_domains_and_tianyi():
    """
    123 云盘的 684/912 域名可识别；天翼云盘 189 独立映射，不能落到移动 139。
    """
    pan123 = parse_share_link("https://www.123912.com/s/pbeojv-9Nnbh")
    assert pan123 is not None
    assert pan123.platform is NetdiskPlatform.PAN123
    assert pan123.share_id == "pbeojv-9Nnbh"
    tianyi = parse_share_link("https://cloud.189.cn/t/i2Un6rRzIVVn")
    assert tianyi is not None
    assert tianyi.platform is NetdiskPlatform.TIANYI
    assert tianyi.share_id == "i2Un6rRzIVVn"


def test_parse_115_command_form_priority():
    """
    115 口令形式优先解析出分享码与提取码。
    """
    ref = parse_share_link("https://115.com/sws8lxs36jf-n307/")
    assert ref is not None
    assert ref.share_id == "sws8lxs36jf"
    assert ref.password == "n307"


def test_parse_unknown_returns_none():
    """
    非网盘链接返回空。
    """
    assert parse_share_link("https://example.com/file.torrent") is None


def test_explicit_password_wins():
    """
    调用方传入的提取码优先于文案解析。
    """
    ref = parse_share_link("https://pan.quark.cn/s/abc123 提取码：x1y2", "zzzz")
    assert ref is not None and ref.password == "zzzz"


def test_normalize_platform_aliases():
    """
    CloudSaver 与用户输入的平台别名收敛到统一枚举。
    """
    assert normalize_platform("baiduPan") is NetdiskPlatform.BAIDU
    assert normalize_platform("yidong") is NetdiskPlatform.PAN139
    assert normalize_platform("115") is NetdiskPlatform.PAN115
    assert normalize_platform("unknown") is None


def test_platform_c139_renamed_to_pan139():
    """
    平台标识统一为 pan139，同时兼容历史 c139。
    """
    assert NetdiskPlatform.PAN139.value == "pan139"
    assert normalize_platform("pan139") is NetdiskPlatform.PAN139
    assert normalize_platform("c139") is NetdiskPlatform.PAN139


@pytest.mark.parametrize(
    ("url", "share_id", "password"),
    [
        ("https://cloud.189.cn/web/share?code=bYB3q2UbIBJb", "bYB3q2UbIBJb", None),
        ("https://cloud.189.cn/t/AbCdEf123", "AbCdEf123", None),
        ("https://h5.cloud.189.cn/share.html#/t/XyZ987", "XyZ987", None),
        ("https://cloud.189.cn/web/share?code=abc123&pwd=9k2m", "abc123", "9k2m"),
        ("天翼分享 https://cloud.189.cn/t/AbCdEf123 访问码：7u8i", "AbCdEf123", "7u8i"),
    ],
)
def test_parse_tianyi_share_link(url: str, share_id: str, password: str | None) -> None:
    """天翼分享链接的三种形态与提取码都要能解析。"""
    ref = parse_share_link(url)
    assert ref is not None
    assert ref.platform is NetdiskPlatform.TIANYI
    assert ref.share_id == share_id
    assert ref.password == password


def test_tianyi_platform_is_not_display_only() -> None:
    """天翼已有适配器，不再属于仅展示平台。"""
    assert normalize_platform("tianyi") is NetdiskPlatform.TIANYI
    assert normalize_platform("189") is NetdiskPlatform.TIANYI
    assert normalize_platform("cloud189") is NetdiskPlatform.TIANYI
    assert "tianyi" not in DISPLAY_ONLY_PLATFORMS


def test_share_level_error_by_code() -> None:
    """分享级判定以错误码优先，直链未就绪不再拉黑分享。"""
    assert is_share_level_error(NetdiskError(NetdiskErrorCode.SHARE_INVALID, "分享不存在"))
    assert not is_share_level_error(
        NetdiskError(NetdiskErrorCode.LINK_NOT_READY, "迅雷直链尚未就绪")
    )
    assert not is_share_level_error(
        NetdiskError(NetdiskErrorCode.UNSUPPORTED, "UC 直链不完整（1/2）")
    )
