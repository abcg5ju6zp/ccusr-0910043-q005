"""可发布的异常映射策略版本。

一个策略版本（:class:`ErrorPolicyVersion`）是一张不可变的解析快照：
它把应用级、蓝图级、路由级三层注册的异常处理器合并为一次确定的
解析过程，查找顺序与历史行为保持一致——先精确匹配，再沿异常的
MRO 匹配；同一层内部路由优先于全局，而层级之间
**路由 > 蓝图 > 应用**。

版本一旦发布即冻结。请求受理时固定当时激活（或显式选定）的版本，
此后即使发生热更新或回滚，该请求的客户端响应与审计记录也始终引用
同一个版本，不会出现两者错配。

未配置任何策略版本的项目不创建版本对象，仍走
:class:`sanic.handlers.error.ErrorHandler` 原有的动态查找路径。
"""

from __future__ import annotations

from itertools import count
from typing import TYPE_CHECKING, Any, Callable


if TYPE_CHECKING:
    from sanic.request.types import Request


# 响应头名称：客户端可据此核对响应与审计引用同一版本
ERROR_POLICY_HEADER = "X-Error-Policy"


class _Layer:
    __slots__ = ("scope", "exact", "wildcard")

    def __init__(self, scope: str):
        self.scope = scope
        # 精确注册：异常类 -> 处理器
        self.exact: dict[type[BaseException], Callable[..., Any]] = {}
        # 路由级注册：路由名 -> 异常类 -> 处理器
        self.wildcard: dict[
            str, dict[type[BaseException], Callable[..., Any]]
        ] = {}


class ErrorPolicyVersion:
    """异常映射策略的一个不可变版本。

    组合优先级明确为：路由级规则 > 蓝图级规则 > 应用级规则；
    每层内部先查当前路由的具名注册，再查全局注册，先精确匹配
    异常类型，再沿 MRO 匹配祖先异常。这与
    :meth:`ErrorHandler.lookup` 的历史顺序一致。
    """

    def __init__(self, version_id: str, layers: list[_Layer]):
        self.id = version_id
        # 优先级高的层排在前面
        self._layers = tuple(layers)

    def __repr__(self) -> str:
        return f"<ErrorPolicyVersion {self.id}>"

    def lookup(
        self,
        exception: BaseException,
        route_name: str | None = None,
    ) -> tuple[Callable[..., Any], str] | None:
        """按优先级解析处理器。

        返回 ``(handler, scope)``；本版本没有匹配规则时返回 ``None``。
        """
        exception_class = type(exception)

        # 1. 精确匹配（保留旧实现先精确后 MRO 的顺序）
        for layer in self._layers:
            handler = self._lookup_exact(layer, exception_class, route_name)
            if handler is not None:
                return handler

        # 2. 沿 MRO 匹配祖先类型
        for ancestor in type.mro(exception_class):
            for layer in self._layers:
                handler = self._lookup_exact(layer, ancestor, route_name)
                if handler is not None:
                    return handler
            if exception_class is BaseException or ancestor is BaseException:
                break

        return None

    @staticmethod
    def _lookup_exact(
        layer: _Layer,
        exception_class: type[BaseException],
        route_name: str | None,
    ) -> tuple[Callable[..., Any], str] | None:
        if route_name is not None:
            named = layer.wildcard.get(route_name)
            if named and exception_class in named:
                return (
                    named[exception_class],
                    f"{layer.scope}:{route_name}",
                )
        handler = layer.exact.get(exception_class)
        if handler is not None:
            return handler, layer.scope
        return None


class ErrorPolicyRegistry:
    """持有所有已发布版本并管理激活版本。"""

    def __init__(self):
        self._versions: dict[str, ErrorPolicyVersion] = {}
        self._active_id: str | None = None
        self._previous_id: str | None = None
        self._counter = count(1)

    # ------------------------------------------------------------------ #
    # 发布与激活
    # ------------------------------------------------------------------ #
    def publish(
        self,
        build: Callable[[], list[_Layer]],
        *,
        version_id: str | None = None,
        activate: bool = True,
    ) -> ErrorPolicyVersion:
        """发布一个新版本。

        ``build`` 返回应用、蓝图、路由三层（低优先级在前）。版本对象
        在构建完成后一次性可见，发布过程不会影响在途请求。
        """
        if version_id is None:
            version_id = f"policy-{next(self._counter)}"
        if version_id in self._versions:
            raise ValueError(
                f"Error policy version {version_id!r} already exists"
            )
        version = ErrorPolicyVersion(version_id, build())
        self._versions[version_id] = version
        if activate or self._active_id is None:
            # 首次发布即激活；此后仅在显式要求时切换激活版本，
            # 这样“先发布、后切换”是可表达的状态。
            self._previous_id = self._active_id
            self._active_id = version_id
        return version

    def activate(self, version_id: str) -> ErrorPolicyVersion:
        """把某个已发布版本设为激活版本。"""
        try:
            version = self._versions[version_id]
        except KeyError:
            raise ValueError(
                f"Unknown error policy version {version_id!r}"
            ) from None
        if version_id != self._active_id:
            self._previous_id = self._active_id
            self._active_id = version_id
        return version

    def rollback(self) -> ErrorPolicyVersion | None:
        """回滚到上一个激活版本。

        没有可回滚的历史时返回 ``None``，调用方据此保持现状。
        """
        if self._previous_id is None:
            return None
        target = self._versions.get(self._previous_id)
        if target is None:
            return None
        previous, self._previous_id = self._previous_id, self._active_id
        self._active_id = previous
        return target

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    @property
    def active(self) -> ErrorPolicyVersion | None:
        if self._active_id is None:
            return None
        return self._versions.get(self._active_id)

    @property
    def active_id(self) -> str | None:
        return self._active_id

    @property
    def previous_id(self) -> str | None:
        return self._previous_id

    def get(self, version_id: str) -> ErrorPolicyVersion | None:
        return self._versions.get(version_id)

    def snapshot(self) -> dict[str, Any]:
        """返回版本与激活状态的只读快照，供审计使用。"""
        return {
            "active": self._active_id,
            "previous": self._previous_id,
            "versions": tuple(self._versions.keys()),
        }


def bind_request_policy(
    request: "Request",
    registry: ErrorPolicyRegistry | None,
    *,
    version_id: str | None = None,
) -> ErrorPolicyVersion | None:
    """请求受理时固定所用策略版本。

    优先取请求显式指定的版本（如路由选定的版本），否则取当前激活
    版本。未配置任何版本的项目返回 ``None``，调用方走旧有查找顺序。
    固定结果写入 ``request.error_policy``，请求全生命周期（含异常
    处理、审计、在途热更新与回滚）只引用该对象。
    """
    if registry is None:
        return None
    version: ErrorPolicyVersion | None
    if version_id is not None:
        version = registry.get(version_id)
        if version is None:
            raise ValueError(f"Unknown error policy version {version_id!r}")
    else:
        version = registry.active
    request.error_policy = version
    return version
