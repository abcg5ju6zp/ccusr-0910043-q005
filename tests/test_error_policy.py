"""异常映射策略版本的回归测试。

覆盖：
- 应用/蓝图/路由三级规则按 路由 > 蓝图 > 应用 优先级组合；
- 请求受理时固定版本，热更新不影响已在处理流程中的请求；
- 处理器二次抛错、流式响应已提交、超时与取消竞争结果确定；
- 策略回滚只影响此后新受理的请求；
- 未配置版本的老项目按原查找顺序运行；
- 本地请求的响应头与审计记录引用同一版本。
"""

from __future__ import annotations

import asyncio

from asyncio import CancelledError

import pytest

from sanic import Blueprint, Request, Sanic
from sanic.exceptions import (
    BadRequest,
    NotFound,
    ServerError,
    ServiceUnavailable,
)
from sanic.handlers import (
    POLICY_VERSION_HEADER,
    ErrorHandler,
    ErrorPolicy,
    PolicyPublishError,
    RuleScope,
)
from sanic.response import text


class CustomError(ServerError):
    pass


class CustomErrorChild(CustomError):
    pass


# --------------------------------------------------------------------- #
# 基础：三级优先级与响应/审计版本一致
# --------------------------------------------------------------------- #
def _build_priority_app():
    app = Sanic("test_policy_priority")
    bp = Blueprint("bp")

    @app.get("/app")
    async def app_route(request: Request):
        raise CustomError("app")

    @bp.get("/bp")
    async def bp_route(request: Request):
        raise CustomError("bp")

    @bp.get("/child")
    async def child_route(request: Request):
        raise CustomErrorChild("child")

    @app.exception(CustomError)
    async def app_handler(request, exception):
        return text("application", 400)

    @bp.exception(CustomError)
    async def bp_handler(request, exception):
        return text("blueprint", 401)

    # 路由级：即便异常类型更"宽泛"，作用域优先级仍高于蓝图精确匹配。
    async def route_handler(request, exception):
        return text("route", 402)

    app.blueprint(bp)
    app.error_handler.add(
        ServerError, route_handler, route_names=[_child_name(app)]
    )
    return app


def _child_name(app):
    return next(
        route.name
        for route in app.router.routes
        if route.path.strip("/").endswith("child")
    )


def test_scope_priority_and_audit_version():
    app = _build_priority_app()
    app.publish_error_policy("v1")

    # 应用级
    request, response = app.test_client.get("/app")
    assert response.status == 400
    assert response.text == "application"
    assert response.headers[POLICY_VERSION_HEADER] == "v1"
    record = request.ctx.error_policy_resolution
    assert record.policy_version == "v1"
    assert record.scope == RuleScope.APPLICATION.value
    assert record.target is None

    # 蓝图级压过应用级
    request, response = app.test_client.get("/bp")
    assert response.status == 401
    assert response.text == "blueprint"
    assert response.headers[POLICY_VERSION_HEADER] == "v1"
    record = request.ctx.error_policy_resolution
    assert record.scope == RuleScope.BLUEPRINT.value
    assert record.target == "bp"

    # 路由级压过蓝图级，即使蓝图规则是精确类型、路由规则是祖先类型
    request, response = app.test_client.get("/child")
    assert response.status == 402
    assert response.text == "route"
    record = request.ctx.error_policy_resolution
    assert record.scope == RuleScope.ROUTE.value
    assert record.target == _child_name(app)
    assert response.headers[POLICY_VERSION_HEADER] == record.policy_version


def test_same_scope_falls_back_along_mro():
    app = Sanic("test_policy_mro")

    @app.get("/")
    async def h(request):
        raise CustomErrorChild("x")

    @app.exception(ServerError)
    async def handle(request, exception):
        return text("via-ancestor", 418)

    app.publish_error_policy("v9")
    request, response = app.test_client.get("/")
    assert response.status == 418
    assert response.text == "via-ancestor"
    assert request.ctx.error_policy_resolution.policy_version == "v9"


# --------------------------------------------------------------------- #
# 热更新：已受理请求固定旧版本
# --------------------------------------------------------------------- #
def test_inflight_request_pinned_during_hot_update():
    app = Sanic("test_policy_hot_update")

    async def v1_handler(request, exception):
        return text("v1-mapping", 400)

    async def v2_handler(request, exception):
        return text("v2-mapping", 418)

    @app.exception(ServerError)
    async def registered(request, exception):
        return await v1_handler(request, exception)

    @app.get("/")
    async def h(request):
        # 请求已经受理（版本已固定）后，热更新才落地。
        app.error_handler.add(
            ServerError, v2_handler, replace=True
        )
        app.publish_error_policy("v2")
        raise ServerError("boom")

    app.publish_error_policy("v1")

    # 在途请求继续使用受理时固定的 v1
    request, response = app.test_client.get("/")
    assert response.status == 400
    assert response.text == "v1-mapping"
    assert response.headers[POLICY_VERSION_HEADER] == "v1"
    record = request.ctx.error_policy_resolution
    assert record.policy_version == "v1"

    # 此后新受理的请求使用 v2
    @app.get("/later")
    async def later(request):
        raise ServerError("boom")

    request, response = app.test_client.get("/later")
    assert response.status == 418
    assert response.text == "v2-mapping"
    assert response.headers[POLICY_VERSION_HEADER] == "v2"


# --------------------------------------------------------------------- #
# 回滚
# --------------------------------------------------------------------- #
def test_rollback_only_affects_new_requests():
    app = Sanic("test_policy_rollback")

    async def h1(request, exception):
        return text("one", 400)

    async def h2(request, exception):
        return text("two", 401)

    app.error_handler.add(ServerError, h1)
    app.publish_error_policy("v1")
    app.error_handler.add(ServerError, h2, replace=True)
    app.publish_error_policy("v2")

    @app.get("/")
    async def route(request):
        raise ServerError("boom")

    _, response = app.test_client.get("/")
    assert response.text == "two"

    policy = app.rollback_error_policy("v1")
    assert isinstance(policy, ErrorPolicy)
    assert app.error_handler.policy_version == "v1"

    request, response = app.test_client.get("/")
    assert response.status == 400
    assert response.text == "one"
    assert response.headers[POLICY_VERSION_HEADER] == "v1"

    # 省略版本号回滚到上一个（v2）
    app.rollback_error_policy()
    _, response = app.test_client.get("/")
    assert response.text == "two"


def test_publish_and_rollback_reject_bad_versions():
    app = Sanic("test_policy_bad_versions")
    app.publish_error_policy("v1")
    with pytest.raises(PolicyPublishError):
        app.publish_error_policy("v1")
    with pytest.raises(PolicyPublishError):
        app.rollback_error_policy("nope")


# --------------------------------------------------------------------- #
# 未配置版本的老项目：原查找顺序、无版本头
# --------------------------------------------------------------------- #
def test_legacy_project_without_published_version():
    app = Sanic("test_policy_legacy")
    bp = Blueprint("legacy_bp")

    @bp.get("/bp")
    async def bp_route(request):
        raise ServerError("bp")

    @app.get("/app")
    async def app_route(request):
        raise BadRequest("app")

    @app.exception(ServerError)
    async def app_handle(request, exception):
        return text("legacy-app", 400)

    @bp.exception(ServerError)
    async def bp_handle(request, exception):
        return text("legacy-bp", 401)

    app.blueprint(bp)
    # 注意：从不调用 publish_error_policy

    request, response = app.test_client.get("/app")
    assert response.status == 400
    assert POLICY_VERSION_HEADER not in response.headers
    assert request.error_policy is None
    assert request.ctx.error_policy_resolution.policy_version is None

    _, response = app.test_client.get("/bp")
    assert response.status == 401
    assert response.text == "legacy-bp"
    assert POLICY_VERSION_HEADER not in response.headers


# --------------------------------------------------------------------- #
# 处理器自身再次抛错：确定性兜底、审计仍引用同一版本
# --------------------------------------------------------------------- #
def test_handler_raises_again_is_deterministic():
    app = Sanic("test_policy_handler_fails")

    @app.get("/")
    async def route(request):
        raise ServerError("boom")

    @app.exception(ServerError)
    async def broken(request, exception):
        raise ValueError("handler exploded")

    app.publish_error_policy("v1")
    request, response = app.test_client.get("/")
    assert response.status == 500
    assert response.text == "An error occurred while handling an error"
    record = request.ctx.error_policy_resolution
    assert record.policy_version == "v1"
    assert record.reason == "handler_failed"
    assert response.headers[POLICY_VERSION_HEADER] == "v1"


# --------------------------------------------------------------------- #
# 流式响应已提交：无法改写响应，但审计确定记录 delivered=False
# --------------------------------------------------------------------- #
def test_stream_already_committed_audit_is_pinned():
    app = Sanic("test_policy_committed")

    @app.exception(ServerError)
    async def handler(request, exception):
        return text("should-not-reach-client", 400)

    @app.get("/")
    async def route(request):
        response = await request.respond()
        await response.send("partial-body")
        raise ServerError("late boom")

    app.publish_error_policy("v1")
    request, response = app.test_client.get("/")
    # 已发送的内容保留，错误映射的响应体不会到达客户端
    assert "partial-body" in response.text
    record = request.ctx.error_policy_resolution
    assert record.policy_version == "v1"
    assert record.delivered is False
    assert record.reason == "response_committed"


# --------------------------------------------------------------------- #
# 超时竞争：响应超时引发取消后仍按固定版本解析
# --------------------------------------------------------------------- #
def test_response_timeout_uses_pinned_version():
    app = Sanic("test_policy_timeout")
    app.config.RESPONSE_TIMEOUT = 1

    @app.get("/")
    async def slow(request):
        await asyncio.sleep(2)
        return text("ok")

    @app.exception(ServiceUnavailable)
    async def timeout_handler(request, exception):
        return text("timed-out", 503)

    app.publish_error_policy("v1")
    request, response = app.test_client.get("/")
    assert response.status == 503
    assert response.text == "timed-out"
    record = request.ctx.error_policy_resolution
    assert record.policy_version == "v1"
    assert response.headers[POLICY_VERSION_HEADER] == "v1"


# --------------------------------------------------------------------- #
# 取消竞争：处理器内 CancelledError 仍由固定版本映射
# --------------------------------------------------------------------- #
def test_cancelled_error_uses_pinned_version():
    app = Sanic("test_policy_cancel")

    @app.get("/")
    async def route(request):
        raise CancelledError("STOP")

    @app.exception(CancelledError)
    async def cancel_handler(request, exception):
        return text("cancelled", 418)

    app.publish_error_policy("v1")
    request, response = app.test_client.get("/")
    assert response.status == 418
    assert response.text == "cancelled"
    record = request.ctx.error_policy_resolution
    assert record.policy_version == "v1"
    assert response.headers[POLICY_VERSION_HEADER] == "v1"


# --------------------------------------------------------------------- #
# 审计回调看到的版本与响应一致
# --------------------------------------------------------------------- #
def test_external_auditor_references_same_version():
    app = Sanic("test_policy_auditor")
    seen: list = []

    def auditor(request, exception, record):
        seen.append(record)

    app.error_handler.add_auditor(auditor)

    @app.get("/")
    async def route(request):
        raise ServerError("boom")

    @app.exception(ServerError)
    async def handle(request, exception):
        return text("ok", 400)

    app.publish_error_policy("v3")
    _, response = app.test_client.get("/")
    assert len(seen) == 1
    assert seen[0].policy_version == "v3"
    assert response.headers[POLICY_VERSION_HEADER] == seen[0].policy_version


# --------------------------------------------------------------------- #
# 快照不可变：拿到的版本对象在后续发布后规则不变
# --------------------------------------------------------------------- #
def test_published_snapshot_is_immutable():
    app = Sanic("test_policy_immutable")

    async def h1(request, exception):
        return text("one", 400)

    app.error_handler.add(ServerError, h1)
    v1 = app.publish_error_policy("v1")
    n_rules_v1 = len(v1.rules)

    async def h2(request, exception):
        return text("two", 401)

    app.error_handler.add(BadRequest, h2)
    app.publish_error_policy("v2")

    # 旧快照不随新版本改变
    assert len(v1.rules) == n_rules_v1
    assert v1.lookup(BadRequest("x"), None) is None
    assert app.error_handler.get_policy("v1") is v1
    assert app.error_handler.policies.previous_version == "v1"


# --------------------------------------------------------------------- #
# 边界：路由前 404、请求中间件抛错、自定义 ErrorHandler 子类
# --------------------------------------------------------------------- #
def test_404_before_routing_is_still_pinned(app: Sanic):
    @app.get("/")
    async def index(request):
        return text("ok")

    @app.exception(NotFound)
    async def nf(request, exception):
        return text("nf", 404)

    app.publish_error_policy("v1")
    request, response = app.test_client.get("/does-not-exist")
    assert response.status == 404
    assert response.text == "nf"
    record = request.ctx.error_policy_resolution
    assert record.policy_version == "v1"
    assert record.scope == RuleScope.APPLICATION.value
    assert response.headers[POLICY_VERSION_HEADER] == "v1"


def test_request_middleware_error_uses_pinned_version(app: Sanic):
    seen: list = []
    app.error_handler.add_auditor(lambda req, exc, rec: seen.append(rec))

    @app.get("/")
    async def index(request):
        return text("ok")

    @app.on_request
    async def mw(request: Request):
        raise ServerError("mw-boom")

    @app.exception(ServerError)
    async def se(request, exception):
        return text("se", 400)

    app.publish_error_policy("v2")
    _, response = app.test_client.get("/")
    assert response.status == 400
    assert response.text == "se"
    assert response.headers[POLICY_VERSION_HEADER] == "v2"
    # 中间件异常同样按受理时固定的版本解析并审计
    assert seen and seen[0].policy_version == "v2"


def test_custom_error_handler_subclass(app: Sanic):
    handler = MyErrorHandler()
    app.error_handler = handler

    @app.get("/")
    async def h(request):
        raise ServerError("x")

    @app.exception(ServerError)
    async def h2(request, exception):
        return text("ok", 418)

    app.publish_error_policy("v1")
    request, response = app.test_client.get("/")
    assert response.status == 418
    assert response.headers[POLICY_VERSION_HEADER] == "v1"
    assert request.ctx.error_policy_resolution.policy_version == "v1"


class MyErrorHandler(ErrorHandler):
    pass
