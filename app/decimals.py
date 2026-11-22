"""精确十进制数值：保留原始 JSON 字面量，容量核算不产生二进制浮点误差。

链路容量与入口流量都以 JSON 数字传递（支持科学计数法）。直接用 float 解析会出现
``0.1 + 0.2 == 0.30000000000000004`` 这类二进制误差，导致“刚好满载”被误判为超载。
因此：

- 请求体用 ``json.loads(..., parse_float=ExactDecimal.parse)`` 解析，每个数字保留
  原始十进制字面量（``ExactDecimal.raw``），全程用 :class:`decimal.Decimal` 精确运算；
- 响应由 :func:`dumps` 序列化，数值按原始字面量原样回写（录入 0.1 就回 0.1，
  录入 1e3 就回 1e3），合计等派生值按十进制规范形式输出；
- 即便合计超过 float64 的有限范围（> 1.7976931348623157e308，即 Infinity），
  Decimal 仍按有限十进制数精确表示并输出为合法 JSON 数字（如 ``2E+309``）。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Annotated

from pydantic import PlainSerializer, WithJsonSchema

# JSON 数字的最大解析精度（有效数字）。默认 28 位会静默截断超长数字，
# 提高到 128 位以保证录入值与合计都按精确十进制处理。
DECIMAL_PRECISION = 128

# JSON 数字中允许出现的字符（含科学计数法、正负号、小数点）
_NUMBER_CHARS = set("0123456789.+-eE")

# pydantic 在 json 序列化模式下会把 Decimal 强制转成字符串。为让响应仍输出
# JSON 数字，json 模式下把 ExactDecimal 包成该标记 dict，由
# ExactJSONResponse.render 递归还原回 Decimal 后再编码为数字。
DECIMAL_MARKER = "__exact_decimal__"


class ExactDecimal(Decimal):
    """带原始 JSON 字面量的 Decimal。

    ``raw`` 为请求中该数字的原始文本（如 ``"0.20000000000000001"``、``"1e3"``），
    用于响应时原样回写，保证“查回方案再录入”得到相同的十进制值。
    Decimal 运算结果退化为普通 :class:`~decimal.Decimal`，按规范形式输出。
    """

    __slots__ = ("_raw",)

    def __new__(cls, value: str | Decimal, raw: str | None = None) -> "ExactDecimal":
        self = super().__new__(cls, value)
        self._raw = raw if raw is not None else str(self)
        return self

    @property
    def raw(self) -> str:
        return self._raw

    @staticmethod
    def parse(text: str) -> "ExactDecimal":
        """``json.loads`` 的 parse_float 钩子：把数字字面量解析为 ExactDecimal。"""
        return ExactDecimal(text, text)


def _json_mode_serializer(value: "ExactDecimal") -> dict[str, str]:
    """pydantic json 序列化模式下的标记包装（render 层用 restore_decimal_markers 还原）。"""
    return {DECIMAL_MARKER: value.raw if isinstance(value, ExactDecimal) else str(value)}


# 供 pydantic 模型字段使用的精确十进制类型：
# - 校验/输入：接受 ExactDecimal（请求通道）或被归一化后的 Decimal
# - python 序列化：保留 Decimal 对象
# - json 序列化：输出标记 dict，由 ExactJSONResponse 还原为 JSON 数字
# - JSON Schema：声明为 number，使 OpenAPI 文档可正常生成
ExactDecimalField = Annotated[
    ExactDecimal,
    PlainSerializer(_json_mode_serializer, return_type=dict, when_used="json"),
    WithJsonSchema({"type": "number"}, mode="serialization"),
    WithJsonSchema({"type": "number"}, mode="validation"),
]


def restore_decimal_markers(value: Any) -> Any:
    """递归把 pydantic json 模式产生的标记 dict 还原为 ExactDecimal。"""
    if isinstance(value, dict):
        if set(value.keys()) == {DECIMAL_MARKER} and isinstance(value[DECIMAL_MARKER], str):
            text = value[DECIMAL_MARKER]
            return ExactDecimal(text, text)
        return {key: restore_decimal_markers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [restore_decimal_markers(item) for item in value]
    return value


def format_total(total: Decimal) -> ExactDecimal:
    """规范化链路合计的展示形式，同时保持精确值不变。

    - 去掉高精度运算产生的尾随零（``2.000...E+309`` -> ``2E+309``）；
    - 整数值输出整数字面量（``1.2E+2`` -> ``120``），更符合运维读数习惯；
    - 非整数保留精确十进制形式（``0.30000000000000001`` 原样）。
    返回 ExactDecimal 以便响应通道按确定字面量输出。
    """
    normalized = total.normalize()
    exponent = normalized.as_tuple().exponent
    if isinstance(exponent, int) and exponent >= 0:
        # 整数值：常规整数（不超过 16 位）展开为纯整数字面量（1.2E+2 -> "120"）；
        # 超大整数（如 2E+309，展开会产生数百个零）保留规范化科学计数法。
        integral = int(normalized)
        text = str(integral) if len(str(integral)) <= 16 else str(normalized)
    else:
        text = str(normalized)
    return ExactDecimal(normalized, text)


def _is_number_text(text: str) -> bool:
    return bool(text) and all(ch in _NUMBER_CHARS for ch in text)


def _encode_decimal(obj: Decimal) -> str:
    """把 Decimal 编码为合法 JSON 数字文本。

    - ExactDecimal 且原始文本仍是合法 JSON 数字：原样回写（保留 0.1、1e3 等写法）；
    - 运算得到的普通 Decimal 或合计值：按 Decimal 规范形式输出
      （Decimal('0.30000000000000001') -> "0.30000000000000001"，
        Decimal('2E+309') -> "2E+309"）。
    """
    if isinstance(obj, ExactDecimal) and _is_number_text(obj.raw):
        return obj.raw
    return str(obj)


def _encode_value(value: Any) -> str:
    if isinstance(value, Decimal):
        return _encode_decimal(value)
    if isinstance(value, dict):
        items = ",".join(
            f"{json.dumps(key, ensure_ascii=False)}:{_encode_value(val)}"
            for key, val in value.items()
        )
        return "{" + items + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode_value(item) for item in value) + "]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, default=str)


def dumps(obj: Any) -> str:
    """JSON 序列化：Decimal/ExactDecimal 输出为 JSON 数字而非字符串。"""
    return _encode_value(obj)


def loads(text: str | bytes) -> Any:
    """JSON 反序列化：所有数字（含科学计数法）保留为精确 Decimal。"""
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    return json.loads(text, parse_float=ExactDecimal.parse, parse_int=ExactDecimal.parse)
