from __future__ import annotations

from collections.abc import Callable
from copy import copy
from inspect import isawaitable
from typing import Any, cast

from sanic.errorpages import BaseRenderer, TextRenderer, exception_response
from sanic.exceptions import ServerError
from sanic.handlers.policy import (
    ERROR_POLICY_HEADER,
    ErrorPolicyRegistry,
    ErrorPolicyVersion,
    _Layer,
    bind_request_policy,
)
from sanic.log import error_logger
from sanic.models.handler_types import RouteHandler
from sanic.request.types import Request
from sanic.response import text
from sanic.response.types import HTTPResponse


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
        # 分层注册：路由 > 蓝图 > 应用。即使从未发布版本也会维护，
        # 但不发布就不会有任何版本化行为，查找仍走 cached_handlers。
        self._layers: dict[str, _Layer] = {
            "route": _Layer("route"),
            "blueprint": _Layer("blueprint"),
            "app": _Layer("app"),
        }
        self.policy_registry: ErrorPolicyRegistry | None = None
        self.audit_sinks: list[Callable[[dict[str, Any]], None]] = []
        # 每个 legacy 键当前所属层级，用于区分“跨层覆盖”与“重复定义”
        self._key_layers: dict[
            tuple[type[BaseException], str | None], str
        ] = {}

    def _full_lookup(self, exception, route_name: str | None = None):
        return self.lookup(exception, route_name)

    def _add(
        self,
        key: tuple[type[BaseException], str | None],
        handler: RouteHandler,
        layer: str = "app",
    ) -> None:
        previous = self._key_layers.get(key)
        if key in self.cached_handlers and previous == layer:
            exc, name = key
            if name is None:
                name = "__ALL_ROUTES__"

            message = (
                f"Duplicate exception handler definition on: route={name} "
                f"and exception={exc}"
            )
            raise ServerError(message)
        # 跨层级（如路由级覆盖蓝图级）属于显式优先级组合，允许覆盖；
        # 未发布版本的老项目不会出现跨层注册，行为保持不变。
        self.cached_handlers[key] = handler
        self._key_layers[key] = layer

    def add(
        self,
        exception,
        handler,
        route_names: list[str] | None = None,
        *,
        layer: str = "app",
        replace: bool = False,
    ):
        """项目内部接口说明。"""
        names: list[str | None] = list(route_names) if route_names else [None]
        keys: list[tuple[type[BaseException], str | None]] = [
            (exception, route) for route in names
        ]
        if replace:
            # 显式热更新：移除同一层、同一作用域的旧映射。已发布版本
            # 持有独立快照，不受影响；在途请求仍引用旧版本。
            old_handlers = set()
            for key in keys:
                old = self.cached_handlers.pop(key, None)
                self._key_layers.pop(key, None)
                if old is not None:
                    old_handlers.add(old)
            target = self._layers[layer]
            if route_names:
                for route in route_names:
                    target.wildcard.setdefault(route, {}).pop(exception, None)
            else:
                target.exact.pop(exception, None)
            # 同时清除指向旧处理器的 MRO 解析缓存，以及被替换异常类型
            # （含其子类）的负缓存，避免旧查找路径残留过期结果。
            for stale_key, stale_handler in list(self.cached_handlers.items()):
                stale_exc, _ = stale_key
                if stale_handler in old_handlers or (
                    stale_handler is None
                    and isinstance(stale_exc, type)
                    and issubclass(stale_exc, exception)
                ):
                    self.cached_handlers.pop(stale_key, None)
                    self._key_layers.pop(stale_key, None)

        for key in keys:
            self._add(key, handler, layer)
        self._store_layer(exception, handler, route_names, layer)

    def _store_layer(
        self,
        exception: type[BaseException],
        handler: RouteHandler,
        route_names: list[str] | None,
        layer: str,
    ) -> None:
        target = self._layers[layer]
        if route_names:
            for route in route_names:
                target.wildcard.setdefault(route, {})[exception] = handler
        else:
            target.exact[exception] = handler

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
    # 策略版本
    # ------------------------------------------------------------------ #
    def ensure_registry(self) -> ErrorPolicyRegistry:
        """项目内部接口说明。"""
        if self.policy_registry is None:
            self.policy_registry = ErrorPolicyRegistry()
        return self.policy_registry

    def build_policy_layers(self) -> list[_Layer]:
        """项目内部接口说明。"""
        # 高优先级在前。只复制映射结构，处理器函数共享引用——需要冻结
        # 的是“异常 -> 处理器”的对应关系，不能 deepcopy 处理器本身
        # （其闭包可能持有不可复制的运行时对象）。
        layers: list[_Layer] = []
        for name in self.remembered_layers:
            source = self._layers[name]
            clone = _Layer(source.scope)
            clone.exact = dict(source.exact)
            clone.wildcard = {
                route: dict(mapping)
                for route, mapping in source.wildcard.items()
            }
            layers.append(clone)
        return layers

    @property
    def remembered_layers(self) -> tuple[str, ...]:
        return ("route", "blueprint", "app")

    def publish_policy(
        self,
        version_id: str | None = None,
        *,
        activate: bool = True,
    ) -> ErrorPolicyVersion:
        """项目内部接口说明。"""
        registry = self.ensure_registry()
        return registry.publish(
            self.build_policy_layers,
            version_id=version_id,
            activate=activate,
        )

    def activate_policy(self, version_id: str) -> ErrorPolicyVersion:
        """项目内部接口说明。"""
        registry = self.ensure_registry()
        return registry.activate(version_id)

    def rollback_policy(self) -> ErrorPolicyVersion | None:
        """项目内部接口说明。"""
        if self.policy_registry is None:
            return None
        return self.policy_registry.rollback()

    def request_policy(
        self, request: Request | None
    ) -> ErrorPolicyVersion | None:
        """返回（并幂等固定）请求所用的策略版本。

        请求在受理时已经固定版本；此处只在请求未经正常受理通道构造
        （例如连接级错误下的空请求）时兜底固定为当前激活版本，绝不用
        新版本覆盖在途请求已固定的旧版本。
        """
        if request is None or self.policy_registry is None:
            return None
        policy = getattr(request, "error_policy", None)
        if policy is None:
            policy = bind_request_policy(request, self.policy_registry)
        return policy

    def resolve(
        self,
        exception: BaseException,
        route_name: str | None,
        policy: ErrorPolicyVersion | None,
    ) -> tuple[RouteHandler | None, str | None]:
        """项目内部接口说明。"""
        if policy is None:
            return self.lookup(exception, route_name), None
        found = policy.lookup(exception, route_name)
        if found is None:
            return None, None
        return found

    # ------------------------------------------------------------------ #
    # 审计
    # ------------------------------------------------------------------ #
    def audit(
        self,
        request: Request | None,
        policy: ErrorPolicyVersion | None,
        **fields: Any,
    ) -> dict[str, Any]:
        """记录一条异常处理审计，返回该记录。"""
        record = {
            "policy": policy.id if policy else "legacy",
            **fields,
        }
        if request is not None:
            records = getattr(request.ctx, "error_audit", None)
            if records is None:
                records = []
                request.ctx.error_audit = records
            records.append(record)
        for sink in self.audit_sinks:
            try:
                sink(record)
            except Exception:
                error_logger.exception("Error audit sink failed")
        return record

    @staticmethod
    def _tag(response, policy: ErrorPolicyVersion | None):
        # 自定义处理器也可能返回 ResponseStream（无 headers 容器），
        # 此时无法在受理阶段写入响应头，仅对实际响应对象打标。
        if policy is not None and hasattr(response, "headers"):
            response.headers[ERROR_POLICY_HEADER] = policy.id
        return response

    def response(self, request, exception):
        """项目内部接口说明。"""
        route_name = request.name if request else None
        policy = self.request_policy(request)
        handler, scope = self.resolve(exception, route_name, policy)
        try:
            if handler:
                outcome = handler(request, exception)
                if isawaitable(outcome):
                    # 协程交由 handle_exception await；await 结果为 None
                    # 表示处理器已自行 respond（如流式），不得再走默认页。
                    return self._finish_async(
                        outcome,
                        request,
                        exception,
                        policy,
                        scope,
                        handler,
                    )
                return self._finish(
                    outcome,
                    request,
                    exception,
                    policy,
                    scope,
                    handler,
                    default_on_none=True,
                )
            return self._finish(
                None,
                request,
                exception,
                policy,
                scope,
                None,
                default_on_none=True,
            )
        except Exception as handler_error:
            return self._handler_failure(
                request, exception, policy, scope, handler, handler_error
            )

    async def _finish_async(
        self,
        coro,
        request,
        exception,
        policy,
        scope,
        handler,
    ):
        try:
            outcome = await coro
        except Exception as handler_error:
            return self._handler_failure(
                request, exception, policy, scope, handler, handler_error
            )
        return self._finish(
            outcome,
            request,
            exception,
            policy,
            scope,
            handler,
            # 保持历史语义：异步处理器返回 None 即自行响应，
            # 由 handle_exception 取 stream.response，不能补发默认页。
            default_on_none=False,
        )

    def _finish(
        self,
        outcome,
        request,
        exception,
        policy,
        scope,
        handler,
        default_on_none: bool,
    ):
        if outcome is None and default_on_none:
            outcome = self.default(request, exception)
        if outcome is None:
            # 处理器已自行提交响应（流式）：无法再写响应头，
            # 但审计仍固定到受理版本。
            self.audit(
                request,
                policy,
                reason="mapped-self-responded",
                scope=scope or "default",
                handler=self._handler_name(handler),
                exception=type(exception).__name__,
                status=None,
            )
            return None
        self.audit(
            request,
            policy,
            reason="mapped" if handler else "default",
            scope=scope or "default",
            handler=self._handler_name(handler),
            exception=type(exception).__name__,
            status=getattr(outcome, "status", None),
        )
        return self._tag(outcome, policy)

    @staticmethod
    def _handler_name(handler) -> str | None:
        return getattr(handler, "__name__", None)

    def _handler_failure(
        self,
        request,
        exception,
        policy,
        scope,
        handler,
        handler_error,
    ) -> HTTPResponse:
        # 处理器自身再次抛错：结果必须确定——固定 500 文本响应，
        # 响应与审计都仍引用请求受理时固定的版本。
        try:
            url = repr(request.url)
        except AttributeError:  # no cov
            url = "unknown"
        response_message = (
            'Exception raised in exception handler "%s" for uri: %s'
        )
        handler_name = getattr(handler, "__name__", repr(handler))
        error_logger.exception(response_message, handler_name, url)
        self.audit(
            request,
            policy,
            reason="handler-failed",
            scope=scope or "default",
            handler=handler_name,
            exception=type(exception).__name__,
            failure=type(handler_error).__name__,
            status=500,
        )

        if self.debug:
            body = response_message % (handler_name, url)
        else:
            body = "An error occurred while handling an error"
        return self._tag(text(body, 500), policy)

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


def status_mapping(status: int) -> RouteHandler:
    """生成一个把异常映射为固定状态码的默认处理器。"""

    def mapping_handler(request: Request, exception: BaseException):
        try:
            mapped = copy(exception)
            mapped.status_code = status  # type: ignore
        except Exception:
            mapped = exception
        response = request.app.error_handler.default(
            request, cast("Exception", mapped)
        )
        response.status = status
        return response

    mapping_handler.__name__ = f"status_mapping_{status}"
    return mapping_handler
