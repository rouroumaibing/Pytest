"""日志脱敏:日志落盘前遮蔽口令、令牌、私钥等敏感信息。

基于敏感词根 + 正则的轻量实现,以模块级函数 ``mask_text`` 直接调用,
无需类封装;也可独立使用(如业务日志出口统一过一遍)。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# 敏感 key 词根:key 大小写不敏感地包含任一词根即视为敏感
_DEFAULT_KEY_ROOTS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "authorization",
    "credential",
)

# 私钥块(-----BEGIN ... PRIVATE KEY----- ... -----END ...-----)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

# k=v / k: v 形式(shell 参数、YAML、日志常见)
_KV_RE_TMPL = r"(?P<key>\b(?i:{keys})\w*\b)\s*[=:]\s*(?P<value>[^\s,;]+|\"[^\"]*\"|'[^']*')"

# JSON 风格 "key": "value"
_JSON_RE_TMPL = r"\"(?P<key>(?i:{keys})\w*)\"\s*:\s*\"(?P<value>[^\"]*)\""

# Authorization 头样式:Bearer/Basic 后跟令牌,必须连令牌一起遮蔽,
# 否则 kv 正则只会遮住 "Bearer" 这个单词而泄露真实令牌。
_BEARER_RE = re.compile(r"\b(?i:bearer|basic)\s+\S+")


def _roots(extra_keywords: Iterable[str] = ()) -> tuple[str, ...]:
    """默认词根 + 额外词根,返回去重排序后的元组。"""
    roots = set(_DEFAULT_KEY_ROOTS)
    roots.update(str(k).lower() for k in extra_keywords)
    return tuple(sorted(roots))


def _compile(key_roots: tuple[str, ...]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """按词根集合编译 kv 与 json 两条脱敏正则。"""
    joined = "|".join(re.escape(k) for k in key_roots)
    return re.compile(_KV_RE_TMPL.format(keys=joined)), re.compile(_JSON_RE_TMPL.format(keys=joined))


def mask_text(text: str, extra_keywords: Iterable[str] = (), mask: str = "***") -> str:
    """遮蔽文本中的敏感信息并返回新字符串。

    覆盖三类敏感信息:

    1. ``key=value`` / ``key: value`` 形式(shell 参数、YAML 片段);
    2. ``"key": "value"`` JSON 形式;
    3. PEM 私钥块整块遮蔽。

    Args:
        text: 待脱敏文本。
        extra_keywords: 追加的敏感词根(与默认词根取并集)。
        mask: 替换敏感值所用的掩码。
    """
    if not text:
        return text
    kv_re, json_re = _compile(_roots(extra_keywords))
    text = _PRIVATE_KEY_RE.sub("-----PRIVATE KEY MASKED-----", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(0).split()[0]} {mask}", text)
    text = kv_re.sub(lambda m: f"{m.group('key')}={mask}", text)
    text = json_re.sub(lambda m: f'"{m.group("key")}": "{mask}"', text)
    return text


def mask_mapping(
    mapping: Mapping[str, Any], extra_keywords: Iterable[str] = (), mask: str = "***"
) -> dict[str, Any]:
    """返回遮蔽后的深拷贝字典:敏感 key 的值被掩码替换,结构不变。"""
    return _walk_mapping(dict(mapping), _roots(extra_keywords), mask)


def _walk_mapping(obj: Any, roots: tuple[str, ...], mask: str) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, str) and _is_sensitive_key(str(k), roots):
                out[str(k)] = mask
            else:
                out[str(k)] = _walk_mapping(v, roots, mask)
        return out
    if isinstance(obj, (list, tuple)):
        return [_walk_mapping(i, roots, mask) for i in obj]
    return obj


def _is_sensitive_key(key: str, roots: tuple[str, ...]) -> bool:
    lowered = key.lower()
    return any(root in lowered for root in roots)


DEFAULT_SANITIZER = mask_text
"""进程级默认脱敏器(可调用函数),未显式配置时使用。"""
