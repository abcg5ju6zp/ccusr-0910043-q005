"""可发布的异常映射策略版本。

一套异常映射规则可以按三个作用域声明：应用、蓝图与路由。
规则在"草稿"中收集，调用 :meth:`ErrorPolicyRegistry.publish` 后被冻结为
不可变的 :class:`ErrorPolicy` 版本；发布动作以原子方式切换当前版本。

请求在受理时固定（pin）住当时的当前版本，整个处理流程——包括异常处理器
二次抛错、流式响应提交后的补救、超时与取消竞争、审计记录——都引用同一个
不可变快照，因此热更新或回滚不会改变已经进入处理流程的请求所使用的规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable


class RuleScope(Enum):
    """异常映射规则的作用域。

    查找时严格按 ``ROUTE > BLUEPRINT > APPLICATION`` 的优先级组合，
    作用域优先级高于异常类型的继承特异性。
    """

    ROUTE = "route"
    BLUEPRINT = "blueprint"
    APPLICATION = "application"


@dataclass(frozen=True)
class PolicyRule:
    """一条不可变的异常映射规则。"""

    exception: type[BaseException]
    handler: Callable[..., Any]
    scope: RuleScope
    target: str | None

    @property
    def scope_key(self) -> tuple[str, str | None]:
        return (self.scope.value, self.target)


@dataclass(frozen=True)
class PolicyLookup:
    """一次命中结果，携带命中的规则，便于审计引用。"""

    handler: Callable[..., Any]
    rule: PolicyRule


class ErrorPolicy:
    """异常映射策略的不可变发布版本。"""

    def __init__(
        self,
        version: str,
        rules: Iterable[PolicyRule],
        route_to_blueprint: dict[str, str] | None = None,
    ):
        self._version = version
        self._index: dict[
            tuple[type[BaseException], tuple[str, str | None]],
            PolicyRule,
        ] = {}
        for rule in rules:
            key = (rule.exception, rule.scope_key)
            if key in self._index:
                raise ValueError(
                    f"Duplicate error mapping in policy {version!r}: "
                    f"exception={rule.exception!r}, scope={rule.scope.value}"
                    f", target={rule.target!r}"
                )
            self._index[key] = rule
        self._route_to_blueprint = dict(route_to_blueprint or {})
        # 解析缓存写在不可变快照内部，不跨版本共享。
        self._resolved: dict[
            tuple[type[BaseException], str | None],
            PolicyLookup | None,
        ] = {}

    @property
    def version(self) -> str:
        return self._version

    @property
    def rules(self) -> tuple[PolicyRule, ...]:
        return tuple(self._index.values())

    def blueprint_of(self, route_name: str | None) -> str | None:
        """根据注册时记录的归属关系判定路由所属蓝图。"""
        if not route_name:
            return None
        if route_name in self._route_to_blueprint:
            return self._route_to_blueprint[route_name]
        # 回退：蓝图路由名以 "<blueprint>." 为前缀。
        for blueprint in set(self._route_to_blueprint.values()):
            if route_name.startswith(f"{blueprint}."):
                return blueprint
        return None

    def _scope_chain(
        self,
        route_name: str | None,
        blueprint_name: str | None,
    ) -> list[tuple[str, str | None]]:
        chain: list[tuple[str, str | None]] = []
        if route_name:
            chain.append((RuleScope.ROUTE.value, route_name))
        blueprint = blueprint_name or self.blueprint_of(route_name)
        if blueprint:
            chain.append((RuleScope.BLUEPRINT.value, blueprint))
        chain.append((RuleScope.APPLICATION.value, None))
        return chain

    def lookup(
        self,
        exception: BaseException,
        route_name: str | None = None,
        blueprint_name: str | None = None,
    ) -> PolicyLookup | None:
        """按 路由 > 蓝图 > 应用、精确类型先于继承类型 的顺序查找。"""
        exception_class = type(exception)
        cache_key = (exception_class, route_name)
        if cache_key in self._resolved:
            return self._resolved[cache_key]

        scopes = self._scope_chain(route_name, blueprint_name)

        # 第一轮：异常类型精确匹配。
        for scope_key in scopes:
            rule = self._index.get((exception_class, scope_key))
            if rule is not None:
                result = PolicyLookup(rule.handler, rule)
                self._resolved[cache_key] = result
                return result

        # 第二轮：沿 MRO 匹配祖先异常类型。
        for scope_key in scopes:
            for ancestor in type.mro(exception_class):
                rule = self._index.get((ancestor, scope_key))
                if rule is not None:
                    result = PolicyLookup(rule.handler, rule)
                    self._resolved[cache_key] = result
                    return result
                if ancestor is BaseException:
                    break

        self._resolved[cache_key] = None
        return None


@dataclass
class _Draft:
    """策略草稿：发布前的可变收集区，对外不可见。"""

    rules: list[PolicyRule] = field(default_factory=list)
    route_to_blueprint: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_policy(cls, policy: ErrorPolicy | None) -> "_Draft":
        if policy is None:
            return cls()
        return cls(
            rules=list(policy.rules),
            route_to_blueprint=dict(policy._route_to_blueprint),
        )


class PolicyPublishError(Exception):
    """发布策略版本失败。"""


class ErrorPolicyRegistry:
    """管理异常映射策略版本的草稿、发布与回滚。"""

    def __init__(self):
        self._versions: dict[str, ErrorPolicy] = {}
        self._current: ErrorPolicy | None = None
        self._previous: ErrorPolicy | None = None
        self._draft = _Draft()

    @property
    def current(self) -> ErrorPolicy | None:
        """当前已发布版本（快照），无已发布版本时为 ``None``。"""
        return self._current

    @property
    def current_version(self) -> str | None:
        return self._current.version if self._current else None

    @property
    def previous_version(self) -> str | None:
        return self._previous.version if self._previous else None

    def get(self, version: str) -> ErrorPolicy:
        try:
            return self._versions[version]
        except KeyError:
            raise PolicyPublishError(
                f"Unknown error policy version: {version!r}"
            ) from None

    def draft(self, *, based_on: str | None = None) -> None:
        """丢弃当前草稿并重新开始。

        ``based_on`` 省略时：已发布过版本则从当前版本复制全部规则
        （增量热更新的自然语义），否则从空白开始。
        """
        if based_on is not None:
            self._draft = _Draft.from_policy(self.get(based_on))
        else:
            self._draft = _Draft.from_policy(self._current)

    def add(
        self,
        exception: type[BaseException],
        handler: Callable[..., Any],
        scope: RuleScope = RuleScope.APPLICATION,
        target: str | None = None,
        route_names: Iterable[str] | None = None,
        *,
        replace: bool = False,
    ) -> list[PolicyRule]:
        """向当前草稿追加规则，返回实际加入的规则。

        默认重复注册立即报错；``replace=True`` 时用新规则替换同键
        （异常类型 + 作用域目标）旧规则，用于热更新修正映射。
        """
        added: list[PolicyRule] = []
        existing = {
            (rule.exception, rule.scope_key): rule
            for rule in self._draft.rules
        }

        def _append(rule: PolicyRule) -> None:
            dedupe_key = (rule.exception, rule.scope_key)
            old = existing.get(dedupe_key)
            if old is not None:
                if not replace:
                    raise ValueError(
                        "Duplicate exception handler definition on: "
                        f"scope={rule.scope.value}, "
                        f"target={rule.target!r} "
                        f"and exception={rule.exception}"
                    )
                self._draft.rules.remove(old)
            existing[dedupe_key] = rule
            self._draft.rules.append(rule)
            added.append(rule)

        if scope is RuleScope.BLUEPRINT:
            blueprint = target
            if not blueprint:
                raise PolicyPublishError(
                    "Blueprint-scoped rule requires a blueprint name"
                )
            _append(PolicyRule(exception, handler, scope, blueprint))
            for route_name in route_names or ():
                self._draft.route_to_blueprint[route_name] = blueprint
        elif scope is RuleScope.ROUTE:
            names = list(route_names or ([target] if target else ()))
            if not names:
                raise PolicyPublishError(
                    "Route-scoped rule requires at least one route name"
                )
            for route_name in names:
                _append(
                    PolicyRule(exception, handler, RuleScope.ROUTE, route_name)
                )
        else:
            _append(
                PolicyRule(exception, handler, RuleScope.APPLICATION, None)
            )
        return added

    def publish(self, version: str) -> ErrorPolicy:
        """冻结当前草稿为不可变版本并原子切换为当前版本。"""
        if not version:
            raise PolicyPublishError(
                "Cannot publish an error policy without a version name"
            )
        if version in self._versions:
            raise PolicyPublishError(
                f"Error policy version already published: {version!r}"
            )
        policy = ErrorPolicy(
            version,
            self._draft.rules,
            self._draft.route_to_blueprint,
        )
        self._versions[version] = policy
        self._previous = self._current
        self._current = policy
        # 下一版草稿延续当前版本规则，等待增量修改；在途请求持有的
        # 旧快照不受影响。
        self._draft = _Draft.from_policy(policy)
        return policy

    def rollback(self, version: str | None = None) -> ErrorPolicy:
        """回滚到指定版本，或在省略时回滚到上一个已发布版本。

        只改变此后新受理的请求；已经固定旧版本的在途请求不受影响。
        """
        if version is None:
            target = self._previous
        else:
            target = self._versions.get(version)
        if target is None:
            raise PolicyPublishError(
                "No error policy version available for rollback"
            )
        if self._current is not target:
            self._previous = self._current
        self._current = target
        return target

    def pin(self) -> ErrorPolicy | None:
        """请求受理时调用：固定此刻的当前版本快照。"""
        return self._current
