from inspect import iscoroutine
from typing import Any, Callable

from sanic.errorpages import BaseRenderer, TextRenderer, exception_response
from sanic.exceptions import ServerError
from sanic.handlers.policy import (
    ErrorPolicy,
    ErrorPolicyRegistry,
    PolicyLookup,
    RuleScope,
)
from sanic.log import error_logger
from sanic.models.handler_types import RouteHandler
from sanic.request.types import Request
from sanic.response import text
from sanic.response.types import HTTPResponse


POLICY_VERSION_HEADER = "X-Error-Policy-Version"


class ErrorResolution:
    """一次异常映射解析的审计记录。

    响应头与审计记录都引用同一个 ``policy_version``，且该版本就是请求
    受理时固定的不可变快照版本。
    """

    __slots__ = (
        "policy_version",
        "scope",
        "target",
        "exception",
        "handler",
        "delivered",
        "reason",
    )

    def __init__(
        self,
        policy_version: str | None,
        scope: str | None,
        target: str | None,
        exception: type[BaseException],
        handler: str | None,
        delivered: bool = True,
        reason: str = "handler",
    ):
        self.policy_version = policy_version
        self.scope = scope
        self.target = target
        self.exception = exception
        self.handler = handler
        self.delivered = delivered
        self.reason = reason


class ErrorHandler:
    """项目内部接口说明。"""

    def __init__(
        self,
        base: type[BaseRenderer] = TextRenderer,
    ):
        self.cached_handlers: dict[
            tuple[type[BaseException], str | None], RouteHandler | None
        ] = {}
        self.debug = False
        self.base = base
        self.policies = ErrorPolicyRegistry()
        self._auditors: list[
            Callable[[Request, BaseException, ErrorResolution], Any]
        ] = []

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #
    def _full_lookup(self, exception, route_name: str | None = None):
        return self.lookup(exception, route_name)

    def _add(
        self,
        key: tuple[type[BaseException], str | None],
        handler: RouteHandler,
        *,
        replace: bool = False,
    ) -> None:
        if key in self.cached_handlers:
            if replace:
                self.cached_handlers[key] = handler
                return
            exc, name = key
            if name is None:
                name = "__ALL_ROUTES__"

            message = (
                f"Duplicate exception handler definition on: route={name} "
                f"and exception={exc}"
            )
            raise ServerError(message)
        self.cached_handlers[key] = handler

    def add(
        self,
        exception,
        handler,
        route_names: list[str] | None = None,
        *,
        blueprint: str | None = None,
        replace: bool = False,
    ):
        """注册异常映射。

        历史查找表始终按原结构填充（蓝图规则历史上以路由名为键），
        因此从未发布策略版本的项目行为完全不变。同时规则进入策略
        草稿，可通过 :meth:`publish_policy` 冻结发布：
        传入 ``blueprint`` 时按蓝图作用域收集，否则有路由名按路由
        作用域，均无则按应用作用域。``replace=True`` 用于热更新，
        以新映射替换草稿中的同键旧映射。
        """
        if route_names:
            for route in route_names:
                self._add((exception, route), handler, replace=replace)
        else:
            self._add((exception, None), handler, replace=replace)

        if blueprint:
            self.policies.add(
                exception,
                handler,
                scope=RuleScope.BLUEPRINT,
                target=blueprint,
                route_names=route_names,
                replace=replace,
            )
        elif route_names:
            self.policies.add(
                exception,
                handler,
                scope=RuleScope.ROUTE,
                route_names=route_names,
                replace=replace,
            )
        else:
            self.policies.add(exception, handler, replace=replace)

    def add_auditor(
        self,
        auditor: Callable[[Request, BaseException, ErrorResolution], Any],
    ) -> None:
        """注册审计回调；每次按固定版本解析异常映射时调用。"""
        self._auditors.append(auditor)

    # ------------------------------------------------------------------ #
    # Publishing
    # ------------------------------------------------------------------ #
    def publish_policy(self, version: str) -> ErrorPolicy:
        """冻结当前草稿为不可变策略版本并原子切换。"""
        return self.policies.publish(version)

    def rollback_policy(self, version: str | None = None) -> ErrorPolicy:
        """回滚策略版本；仅影响此后新受理的请求。"""
        return self.policies.rollback(version)

    def get_policy(self, version: str) -> ErrorPolicy:
        """按版本号取回已发布的不可变策略快照。"""
        return self.policies.get(version)

    def pin_policy(self) -> ErrorPolicy | None:
        """请求受理时调用：固定此刻已发布的策略版本。

        从未发布过版本时返回 ``None``，请求按历史查找顺序处理。
        """
        return self.policies.pin()

    @property
    def policy_version(self) -> str | None:
        return self.policies.current_version

    # ------------------------------------------------------------------ #
    # Legacy lookup (unchanged behavior for projects without versions)
    # ------------------------------------------------------------------ #
    def lookup(self, exception, route_name: str | None = None):
        """项目内部接口说明。"""
        exception_class = type(exception)

        for name in (route_name, None):
            exception_key = (exception_class, name)
            handler = self.cached_handlers.get(exception_key)
            if handler:
                return handler

        for name in (route_name, None):
            for ancestor in type.mro(exception_class):
                exception_key = (ancestor, name)
                if exception_key in self.cached_handlers:
                    handler = self.cached_handlers[exception_key]
                    self.cached_handlers[(exception_class, route_name)] = (
                        handler
                    )
                    return handler

                if ancestor is BaseException:
                    break
        self.cached_handlers[(exception_class, route_name)] = None
        handler = None
        return handler

    _lookup = _full_lookup

    # ------------------------------------------------------------------ #
    # Version-aware resolution
    # ------------------------------------------------------------------ #
    def _pinned_policy(self, request: Request | None) -> ErrorPolicy | None:
        if request is None:
            return None
        return getattr(request, "error_policy", None)

    def resolve(
        self,
        request: Request | None,
        exception: BaseException,
    ) -> tuple[RouteHandler | None, ErrorResolution]:
        """按请求固定的策略版本解析处理器，并生成审计记录。"""
        policy = self._pinned_policy(request)
        route_name = request.name if request else None
        hit: PolicyLookup | None = None

        if policy is not None:
            hit = policy.lookup(exception, route_name)
            handler = hit.handler if hit else None
            record = ErrorResolution(
                policy_version=policy.version,
                scope=hit.rule.scope.value if hit else None,
                target=hit.rule.target if hit else None,
                exception=type(exception),
                handler=getattr(handler, "__name__", None),
            )
        else:
            handler = self.lookup(exception, route_name)
            record = ErrorResolution(
                policy_version=None,
                scope=None,
                target=None,
                exception=type(exception),
                handler=getattr(handler, "__name__", None),
            )
        return handler, record

    def audit(
        self,
        request: Request | None,
        exception: BaseException,
        record: ErrorResolution,
    ) -> None:
        """落审计记录。记录只追加、不就地修改，避免竞争中被覆盖。"""
        if request is not None:
            ctx = request.ctx
            resolutions = getattr(ctx, "error_policy_resolutions", None)
            if resolutions is None:
                ctx.error_policy_resolutions = []
            ctx.error_policy_resolutions.append(record)
            ctx.error_policy_resolution = record
            for auditor in self._auditors:
                try:
                    auditor(request, exception, record)
                except Exception:  # pragma: no cover - 审计失败不影响响应
                    error_logger.exception("Error policy auditor failed")

    @staticmethod
    def stamp(response: Any, record: ErrorResolution) -> Any:
        """让响应引用与审计记录相同的策略版本。"""
        if record.policy_version is not None:
            try:
                response.headers[POLICY_VERSION_HEADER] = record.policy_version
            except Exception:  # pragma: no cover
                pass
        return response

    def _fallback_text(self, handler, request, record) -> HTTPResponse:
        try:
            url = repr(request.url)
        except AttributeError:  # no cov
            url = "unknown"
        name = getattr(handler, "__name__", "default")
        response_message = (
            'Exception raised in exception handler "%s" for uri: %s'
        )
        error_logger.exception(response_message, name, url)
        # 处理器自身再次抛错：不重新查找规则，沿用固定版本生成
        # 确定性的兜底响应，审计仍引用同一版本。
        record.reason = "handler_failed"
        if self.debug:
            return text(response_message % (name, url), 500)
        return text("An error occurred while handling an error", 500)

    # ------------------------------------------------------------------ #
    # Response generation
    # ------------------------------------------------------------------ #
    def response(self, request, exception):
        """项目内部接口说明。"""
        handler, record = self.resolve(request, exception)
        try:
            if handler:
                response = handler(request, exception)
            else:
                response = None
            if response is None:
                record.reason = "default"
                response = self.default(request, exception)
        except Exception:
            response = self._fallback_text(handler, request, record)

        if iscoroutine(response):
            return self._async_response(
                request, exception, handler, record, response
            )

        self.stamp(response, record)
        self.audit(request, exception, record)
        return response

    async def _async_response(
        self, request, exception, handler, record, coroutine
    ):
        """异步异常处理器：求值后再落审计并尽量加盖版本标记。

        协程返回 ``None`` 时保持历史语义——处理器可能已经通过
        ``request.respond`` 直接写出了流式响应，``None`` 交由应用层
        从 ``request.stream.response`` 取回，不在这里补默认响应。
        """
        try:
            response = await coroutine
        except Exception:
            response = self._fallback_text(handler, request, record)

        if response is None:
            streamed = getattr(request, "stream", None)
            streamed_response = getattr(streamed, "response", None)
            if streamed_response is not None:
                self.stamp(streamed_response, record)
                record.reason = "streamed"
            self.audit(request, exception, record)
            return None

        self.stamp(response, record)
        self.audit(request, exception, record)
        return response

    def default(self, request: Request, exception: Exception) -> HTTPResponse:
        """项目内部接口说明。"""
        self.log(request, exception)
        fallback = request.app.config.FALLBACK_ERROR_FORMAT
        return exception_response(
            request,
            exception,
            debug=self.debug,
            base=self.base,
            fallback=fallback,
        )

    @staticmethod
    def log(request: Request, exception: Exception) -> None:
        """项目内部接口说明。"""
        quiet = getattr(exception, "quiet", False)
        noisy = getattr(request.app.config, "NOISY_EXCEPTIONS", False)
        if quiet is False or noisy is True:
            try:
                url = repr(request.url)
            except AttributeError:  # no cov
                url = "unknown"

            error_logger.exception(
                "Exception occurred while handling uri: %s", url
            )
