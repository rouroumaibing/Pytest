"""可插拔认证策略。

认证与客户端解耦:``AuthStrategy.apply`` 在每次请求前把凭证注入
:class:`requests.Session`;服务端返回 401 时客户端会调用 ``refresh``
刷新凭证并重试一次(如 Token 过期自动换新)。

内置四种策略 + 自定义函数策略,业务侧亦可直接继承扩展::

    class SigAuth(AuthStrategy):
        def apply(self, session):
            session.headers["X-Signature"] = sign(self._secret)

自定义凭证来源(如“先登录换 token”)可用工厂函数在构造前完成,
或实现 ``refresh`` 进行动态续期。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Mapping, Optional, Union

import requests

from atf.utils.log import get_logger

_logger = get_logger("atf.auth")

SessionHook = Callable[[requests.Session], None]


class AuthStrategy(ABC):
    """认证策略基类:把凭证注入 session(典型:设置 header / cookie)。"""

    @abstractmethod
    def apply(self, session: requests.Session) -> None:
        """把当前凭证注入 session(每次请求前调用,应可重复执行)。"""

    def refresh(self, session: requests.Session) -> bool:
        """刷新凭证(客户端收到 401 时调用)。

        Returns:
            是否刷新成功;返回 False 表示策略不支持续期,客户端不再重试。
        """
        return False


class TokenAuth(AuthStrategy):
    """Bearer / 自定义头的令牌认证。

    Args:
        token: 令牌值。
        header: 承载令牌的 header 名,默认 ``Authorization``。
        scheme: 方案前缀(如 ``Bearer``);None 表示裸令牌。
        refresh_fn: 可选的取新令牌函数,无参调用返回新 token。
    """

    def __init__(
        self,
        token: str,
        *,
        header: str = "Authorization",
        scheme: Optional[str] = "Bearer",
        refresh_fn: Optional[Callable[[], str]] = None,
    ) -> None:
        self._token = token
        self._header = header
        self._scheme = scheme
        self._refresh_fn = refresh_fn

    @property
    def token(self) -> str:
        """当前令牌值(测试断言用)。"""
        return self._token

    def apply(self, session: requests.Session) -> None:
        value = f"{self._scheme} {self._token}" if self._scheme else self._token
        session.headers[self._header] = value

    def refresh(self, session: requests.Session) -> bool:
        if self._refresh_fn is None:
            return False
        self._token = self._refresh_fn()
        self.apply(session)
        _logger.info("token refreshed")
        return True


class CookieAuth(AuthStrategy):
    """Cookie 认证(如登录态 Session Cookie)。

    Args:
        cookies: 名值对,注入 session.cookies。
        refresh_fn: 可选的无参函数,返回新的 cookies 字典。
    """

    def __init__(
        self,
        cookies: Mapping[str, str],
        *,
        refresh_fn: Optional[Callable[[], Mapping[str, str]]] = None,
    ) -> None:
        self._cookies = dict(cookies)
        self._refresh_fn = refresh_fn

    def apply(self, session: requests.Session) -> None:
        session.cookies.update(self._cookies)

    def refresh(self, session: requests.Session) -> bool:
        if self._refresh_fn is None:
            return False
        session.cookies.clear()
        self._cookies = dict(self._refresh_fn())
        self.apply(session)
        return True


class ApiKeyAuth(AuthStrategy):
    """API Key 头认证。

    Args:
        key: 密钥值。
        header: header 名,默认 ``X-API-Key``。
    """

    def __init__(self, key: str, *, header: str = "X-API-Key") -> None:
        self._key = key
        self._header = header

    def apply(self, session: requests.Session) -> None:
        session.headers[self._header] = self._key


class CustomAuth(AuthStrategy):
    """函数式自定义策略:任意 ``(session) -> None`` 可调用。

    Args:
        hook: 每次请求前对 session 做注入的函数。
        refresh_hook: 可选的 401 续期函数,返回 True 表示可重试。
    """

    def __init__(
        self,
        hook: SessionHook,
        *,
        refresh_hook: Optional[Callable[[requests.Session], bool]] = None,
    ) -> None:
        self._hook = hook
        self._refresh_hook = refresh_hook

    def apply(self, session: requests.Session) -> None:
        self._hook(session)

    def refresh(self, session: requests.Session) -> bool:
        return self._refresh_hook(session) if self._refresh_hook else False


def build_auth(spec: Mapping[str, object]) -> Optional[AuthStrategy]:
    """按配置字典构造认证策略(供 ConfigLoader 的 YAML 配置直用)。

    支持的 ``type``::

        {type: none}                          -> None
        {type: token, token: xxx}             -> TokenAuth
        {type: token, token: xxx, header: X-Token, scheme: null}
        {type: cookie, cookies: {sid: xxx}}   -> CookieAuth
        {type: apikey, key: xxx, header: X-API-Key}
        {type: custom}                        -> CustomAuth(需显式传 hook,配置态不支持)

    Raises:
        ValueError: type 未知或必填字段缺失。
    """
    kind = str(spec.get("type", "none")).lower()
    if kind == "none":
        return None
    if kind == "token":
        token = spec.get("token")
        if not token:
            raise ValueError("token auth requires 'token'")
        return TokenAuth(
            str(token),
            header=str(spec.get("header", "Authorization")),
            scheme=(str(spec["scheme"]) if spec.get("scheme") is not None else None),
        )
    if kind == "cookie":
        cookies = spec.get("cookies")
        if not isinstance(cookies, Mapping):
            raise ValueError("cookie auth requires mapping 'cookies'")
        return CookieAuth(cookies)
    if kind == "apikey":
        key = spec.get("key")
        if not key:
            raise ValueError("apikey auth requires 'key'")
        return ApiKeyAuth(str(key), header=str(spec.get("header", "X-API-Key")))
    raise ValueError(f"unknown auth type: {kind!r} (custom auth must be built in code)")
