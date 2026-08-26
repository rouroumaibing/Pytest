"""日志脱敏器。

在日志落盘前遮蔽口令、令牌、私钥等敏感信息,供 SSHExecutor / BaseClient
共用;也可独立使用(例如业务日志出口统一过一遍)。
"""

from __future__ import annotations

import copy
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


def _compile(key_roots: Iterable[str]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """按词根集合编译 kv 与 json 两条脱敏正则。"""
    joined = "|".join(re.escape(k) for k in sorted(key_roots, key=len, reverse=True))
    return re.compile(_KV_RE_TMPL.format(keys=joined)), re.compile(_JSON_RE_TMPL.format(keys=joined))


class Sanitizer:
    """基于词根 + 正则的日志脱敏器。

    覆盖三类敏感信息:

    1. ``key=value`` / ``key: value`` 形式(shell 参数、YAML 片段);
    2. ``"key": "value"`` JSON 形式;
    3. PEM 私钥块整块遮蔽。

    Example:
        >>> Sanitizer().mask_text("connect db with password=Admin@123 ok")
        'connect db with password=*** ok'

    Args:
        extra_keywords: 追加的敏感词根(与默认词根取并集)。
        mask: 替换敏感值所用的掩码。
    """

    def __init__(self, extra_keywords: Iterable[str] = (), mask: str = "***") -> None:
        roots = set(_DEFAULT_KEY_ROOTS)
        roots.update(str(k).lower() for k in extra_keywords)
        self._roots = tuple(sorted(roots))
        self._mask = mask
        self._kv_re, self._json_re = _compile(self._roots)

    @property
    def keywords(self) -> tuple[str, ...]:
        """当前生效的敏感词根集合(只读)。"""
        return self._roots

    def add_keywords(self, keywords: Iterable[str]) -> None:
        """运行期追加敏感词根。"""
        merged = set(self._roots) | {str(k).lower() for k in keywords}
        if merged != set(self._roots):
            self._roots = tuple(sorted(merged))
            self._kv_re, self._json_re = _compile(self._roots)

    def _sub(self, m: re.Match[str]) -> str:
        return f"{m.group('key')}={self._mask}"

    def mask_text(self, text: str) -> str:
        """遮蔽文本中的敏感信息并返回新字符串。"""
        if not text:
            return text
        text = _PRIVATE_KEY_RE.sub("-----PRIVATE KEY MASKED-----", text)
        text = _BEARER_RE.sub(lambda m: f"{m.group(0).split()[0]} {self._mask}", text)
        text = self._kv_re.sub(self._sub, text)
        text = self._json_re.sub(lambda m: f'"{m.group("key")}": "{self._mask}"', text)
        return text

    def mask_mapping(self, mapping: Mapping[str, Any]) -> dict[str, Any]:
        """返回遮蔽后的深拷贝字典:敏感 key 的值被掩码替换,结构不变。"""
        return self._walk(copy.deepcopy(dict(mapping)))

    def _walk(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                if isinstance(v, str) and self._is_sensitive_key(str(k)):
                    out[str(k)] = self._mask
                else:
                    out[str(k)] = self._walk(v)
            return out
        if isinstance(obj, (list, tuple)):
            return [self._walk(i) for i in obj]
        return obj

    def _is_sensitive_key(self, key: str) -> bool:
        lowered = key.lower()
        return any(root in lowered for root in self._roots)

    def __call__(self, text: str) -> str:
        """``mask_text`` 的快捷方式,便于作为函数传入。"""
        return self.mask_text(text)


DEFAULT_SANITIZER = Sanitizer()
"""进程级默认脱敏器实例,未显式配置时使用。"""
