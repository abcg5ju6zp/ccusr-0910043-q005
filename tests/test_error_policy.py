"""异常映射策略版本的行为测试。

覆盖：应用/蓝图/路由三层优先级组合、请求受理时固定版本、在途
热更新不改变在途请求、策略回滚、异常处理器自身抛错、流式响应已
提交、超时/取消竞争、未配置版本的老项目保持原查找顺序，以及本地
请求下响应头与审计记录引用同一版本。
"""

import asyncio

import pytest

from sanic import Blueprint, Sanic
from sanic.exceptions import (
    Forbidden,
    NotFound,
    ServerError,
    ServiceUnavailable,
)
from sanic.handlers import ErrorHandler
from sanic.handlers.policy import ERROR_POLICY_HEADER
from sanic.response import text


# --------------------------------------------------------------------- #
# 单元测试：版本快照解析优先级
# --------------------------------------------------------------------- #
def test_version_lookup_priority_and_mro():
    class Base(ServerError):
        pass

    class Sub(Base):
        pass

    bp_handler = lambda r, e: "bp"  # noqa: E731
    route_handler = lambda r, e: "route"  # noqa: E731
    base_handler = lambda r, e: "base"  # noqa: E731

    handler = ErrorHandler()
    # 应用级注册 Base；蓝图级为其路由集合注册 Sub；路由级再为 bp.r 覆盖
    handler.add(Base, base_handler, layer="app")
    handler.add(Sub, bp_handler, ["bp.r", "bp.other"], layer="blueprint")
    handler.add(Sub, route_handler, ["bp.r"], layer="route")
    version = handler.publish_policy("v1")

    # 路由 > 蓝图 > 应用
    got, scope = handler.resolve(Sub(), "bp.r", version)
    assert got is route_handler
    assert scope == "route:bp.r"

    # 同蓝图其它路由走蓝图级规则
    got, scope = handler.resolve(Sub(), "bp.other", version)
    assert got is bp_handler
    assert scope == "blueprint:bp.other"

    # 无层级命中时沿 MRO 命中应用级 Base
    got, scope = handler.resolve(Sub(), "app.x", version)
    assert got is base_handler
    assert scope == "app"

    # 未命中
    assert handler.resolve(NotFound(), "app.x", version) == (None, None)


def test_published_version_is_immutable():
    handler = ErrorHandler()
    first = lambda r, e: "first"  # noqa: E731
    second = lambda r, e: "second"  # noqa: E731
    handler.add(ServerError, first)
    v1 = handler.publish_policy("v1")

    # 发布后显式替换应用级处理器，并发布新版本
    handler.add(ServerError, second, replace=True)
    v2 = handler.publish_policy("v2")

    # v1 仍解析到发布时的处理器，不受后续注册影响
    got, _ = handler.resolve(ServerError(), None, v1)
    assert got is first
    got, _ = handler.resolve(ServerError(), None, v2)
    assert got is second


def test_registry_activate_and_rollback():
    handler = ErrorHandler()
    v1 = handler.publish_policy("v1")
    v2 = handler.publish_policy("v2")
    assert handler.policy_registry.active is v2

    assert handler.activate_policy("v1") is v1
    assert handler.policy_registry.active_id == "v1"

    rolled_back = handler.rollback_policy()
    assert rolled_back is v2
    assert handler.policy_registry.active_id == "v2"

    # 再回滚一次则重新应用上一个版本
    assert handler.rollback_policy() is v1

    with pytest.raises(ValueError):
        handler.activate_policy("missing")
    with pytest.raises(ValueError):
        handler.publish_policy("v1")


def test_rollback_without_history_is_noop():
    handler = ErrorHandler()
    assert handler.rollback_policy() is None


# --------------------------------------------------------------------- #
# 集成：本地请求下响应与审计引用同一版本
# --------------------------------------------------------------------- #
def test_response_header_and_audit_reference_same_version(app: Sanic):
    seen = []

    @app.exception(ServerError)
    async def handle_server_error(request, exception):
        return text("mapped", 418)

    @app.route("/boom")
    async def boom(request):
        seen.append(request.error_policy.id)
        raise ServerError("boom")

    app.error_handler.audit_sinks.append(lambda record: seen.append(record))
    version = app.publish_error_policy("v1")

    request, response = app.test_client.get("/boom")

    assert response.status == 418
    assert response.headers[ERROR_POLICY_HEADER] == version.id
    assert request.error_policy is version
    # 受理时处理器运行前即已固定
    assert seen[0] == "v1"

    audit = request.ctx.error_audit
    assert len(audit) == 1
    assert audit[0]["policy"] == "v1"
    assert audit[0]["status"] == 418
    assert audit[0]["exception"] == "ServerError"
    # 响应与审计必须引用同一版本
    assert response.headers[ERROR_POLICY_HEADER] == audit[0]["policy"]
    # sink 与请求上的审计记录一致
    assert seen[1]["policy"] == "v1"


def test_unmapped_exception_is_audited_with_pinned_version(app: Sanic):
    @app.route("/missing")
    async def missing(request):
        raise NotFound("nope")

    app.publish_error_policy("v1")
    request, response = app.test_client.get("/missing")

    assert response.status == 404
    assert response.headers[ERROR_POLICY_HEADER] == "v1"
    audit = request.ctx.error_audit[-1]
    assert audit["policy"] == "v1"
    assert audit["reason"] == "default"
    assert audit["status"] == 404


# --------------------------------------------------------------------- #
# 在途请求固定版本：热更新不影响已受理请求
# --------------------------------------------------------------------- #
def test_hot_reload_does_not_affect_inflight_request(app: Sanic):
    @app.exception(ServerError)
    async def old_mapping(request, exception):
        return text("old", 418)

    state = {}

    @app.route("/swap")
    async def swap(request):
        state["pinned"] = request.error_policy.id

        # 第一个请求受理（版本已固定）后热更新映射；只执行一次
        if not getattr(app.ctx, "reloaded", False):
            # 显式替换应用级规则，并发布新版本
            app.add_error_mapping(ServerError, 422, replace=True)
            app.publish_error_policy("v2")
            app.ctx.reloaded = True
        raise ServerError("boom")

    app.publish_error_policy("v1")

    request, response = app.test_client.get("/swap")

    # 在途请求仍使用受理时的 v1：客户端状态码与审计一致
    assert state["pinned"] == "v1"
    assert response.status == 418
    assert response.text == "old"
    assert response.headers[ERROR_POLICY_HEADER] == "v1"
    assert request.ctx.error_audit[-1]["policy"] == "v1"

    # 热更新之后受理的新请求使用 v2
    request2, response2 = app.test_client.get("/swap")
    assert response2.status == 422
    assert response2.headers[ERROR_POLICY_HEADER] == "v2"
    assert request2.error_policy.id == "v2"


# --------------------------------------------------------------------- #
# 回滚
# --------------------------------------------------------------------- #
def test_rollback_restores_previous_version_for_new_requests(app: Sanic):
    @app.exception(ServerError)
    async def v1_handler(request, exception):
        return text("v1", 418)

    @app.route("/r")
    async def r(request):
        raise ServerError("x")

    app.publish_error_policy("v1")

    # 热更新为另一个状态码映射
    app.add_error_mapping(ServerError, 422, replace=True)
    app.publish_error_policy("v2")

    _, response = app.test_client.get("/r")
    assert response.status == 422

    previous = app.rollback_error_policy()
    assert previous.id == "v1"

    request, response = app.test_client.get("/r")
    assert response.status == 418
    assert response.headers[ERROR_POLICY_HEADER] == "v1"
    assert request.ctx.error_audit[-1]["policy"] == "v1"


# --------------------------------------------------------------------- #
# 处理器自身再次抛错：确定的 500，版本仍固定
# --------------------------------------------------------------------- #
def test_handler_raising_again_gives_deterministic_500(app: Sanic):
    @app.exception(Forbidden)
    async def broken(request, exception):
        raise RuntimeError("kaboom")

    @app.route("/x")
    async def x(request):
        raise Forbidden("no")

    app.publish_error_policy("v1")
    request, response = app.test_client.get("/x")

    assert response.status == 500
    assert response.headers[ERROR_POLICY_HEADER] == "v1"
    audit = request.ctx.error_audit[-1]
    assert audit["reason"] == "handler-failed"
    assert audit["policy"] == "v1"
    assert audit["failure"] == "RuntimeError"


def test_sync_handler_raising_again_is_tagged(app: Sanic):
    @app.exception(Forbidden)
    def broken_sync(request, exception):
        raise ValueError("nope")

    @app.route("/x")
    async def x(request):
        raise Forbidden("no")

    app.publish_error_policy("v1")
    request, response = app.test_client.get("/x")

    assert response.status == 500
    assert response.headers[ERROR_POLICY_HEADER] == "v1"
    assert request.ctx.error_audit[-1]["failure"] == "ValueError"


# --------------------------------------------------------------------- #
# 流式响应已提交：状态码不可改，但审计固定版本
# --------------------------------------------------------------------- #
def test_response_already_committed_is_audited_not_remapped(app: Sanic):
    @app.exception(ServerError)
    async def exception_handler(request, exception):
        return text("should-not-reach-client", 418)

    @app.route("/stream")
    async def stream(request):
        response = await request.respond()
        await response.send("partial")
        raise ServerError("late failure")

    app.publish_error_policy("v1")
    request, response = app.test_client.get("/stream")

    # 已发送的内容保留，异常处理器的结果不发送
    assert "partial" in response.text
    assert "should-not-reach-client" not in response.text
    audit = request.ctx.error_audit[-1]
    assert audit["reason"] == "response-committed"
    assert audit["policy"] == "v1"
    assert audit["suppressed"] is True
    assert audit["status"] is None


# --------------------------------------------------------------------- #
# 超时竞争：超时注入的异常按受理时固定的版本映射
# --------------------------------------------------------------------- #
def test_timeout_race_uses_pinned_policy():
    app = Sanic("test_policy_timeout")
    app.config.RESPONSE_TIMEOUT = 0.5

    @app.exception(ServiceUnavailable)
    def on_timeout(request, exception):
        return text("timed out", 503)

    @app.route("/slow")
    async def slow(request):
        await asyncio.sleep(2)
        return text("OK")

    app.publish_error_policy("v1")
    request, response = app.test_client.get("/slow")

    assert response.status == 503
    assert response.text == "timed out"
    assert response.headers[ERROR_POLICY_HEADER] == "v1"
    assert request.ctx.error_audit[-1]["policy"] == "v1"


def test_cancel_race_uses_pinned_policy(app: Sanic):
    @app.exception(asyncio.CancelledError)
    async def on_cancel(request, exception):
        return text("cancelled", 418)

    @app.route("/cancel")
    async def cancel(request):
        raise asyncio.CancelledError("STOP")

    app.publish_error_policy("v1")
    request, response = app.test_client.get("/cancel")

    assert response.status == 418
    assert response.text == "cancelled"
    assert response.headers[ERROR_POLICY_HEADER] == "v1"
    assert request.ctx.error_audit[-1]["policy"] == "v1"


# --------------------------------------------------------------------- #
# 应用 / 蓝图 / 路由 三层规则组合
# --------------------------------------------------------------------- #
def test_blueprint_and_route_rules_combine_by_priority(app: Sanic):
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.exception(ServerError)
    async def bp_handler(request, exception):
        return text("bp", 401)

    @bp.route("/r", name="r")
    async def bp_route(request):
        raise ServerError("x")

    @bp.route("/other", name="other")
    async def bp_other(request):
        raise ServerError("x")

    @app.route("/app")
    async def app_route(request):
        raise ServerError("x")

    app.blueprint(bp)

    # 路由级映射覆盖蓝图级
    app.add_error_mapping(ServerError, 409, routes=["bp.r"])
    app.publish_error_policy("v1")

    _, response = app.test_client.get("/bp/r")
    assert response.status == 409
    assert response.headers[ERROR_POLICY_HEADER] == "v1"

    request, response = app.test_client.get("/bp/other")
    assert response.status == 401
    assert response.text == "bp"
    assert request.ctx.error_audit[-1]["scope"].startswith("blueprint:")

    # 应用级默认错误页（500），不带蓝图处理器
    _, response = app.test_client.get("/app")
    assert response.status == 500


def test_application_level_status_mapping(app: Sanic):
    @app.route("/z")
    async def z(request):
        raise Forbidden("no")

    app.add_error_mapping(Forbidden, 429)
    app.publish_error_policy("v1")
    request, response = app.test_client.get("/z")

    assert response.status == 429
    assert response.headers[ERROR_POLICY_HEADER] == "v1"
    assert request.ctx.error_audit[-1]["scope"] == "app"


def test_blueprint_level_status_mapping(app: Sanic):
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.route("/z")
    async def z(request):
        raise Forbidden("no")

    app.blueprint(bp)
    app.add_error_mapping(Forbidden, 423, blueprints=["bp"])
    app.publish_error_policy("v1")

    request, response = app.test_client.get("/bp/z")
    assert response.status == 423
    assert request.ctx.error_audit[-1]["scope"].startswith("blueprint:")


def test_unknown_route_name_in_mapping_raises(app: Sanic):
    @app.route("/z")
    async def z(request):
        return text("ok")

    with pytest.raises(Exception):
        app.add_error_mapping(ServerError, 500, routes=["nope.missing"])


# --------------------------------------------------------------------- #
# 未配置版本的老项目：原查找顺序、无版本头、无固定对象
# --------------------------------------------------------------------- #
def test_legacy_project_without_version_keeps_lookup_order(app: Sanic):
    @app.exception(ServerError)
    async def legacy_handler(request, exception):
        return text("legacy", 418)

    @app.route("/old")
    async def old(request):
        raise ServerError("x")

    # 不发布任何版本
    request, response = app.test_client.get("/old")

    assert response.status == 418
    assert response.text == "legacy"
    assert ERROR_POLICY_HEADER not in response.headers
    assert request.error_policy is None
    assert app.error_handler.policy_registry is None
    # 审计以 "legacy" 标记，仍然记录
    assert request.ctx.error_audit[-1]["policy"] == "legacy"


def test_legacy_mro_lookup_order_unchanged():
    class CustomServerError(ServerError):
        pass

    handler = ErrorHandler()

    def server_error_handler(request, exception):
        return text("OK")

    handler.add(ServerError, server_error_handler)

    # 未发布版本时返回处理器本身，与历史行为一致
    assert handler.lookup(CustomServerError(), None) is server_error_handler


# --------------------------------------------------------------------- #
# 发布但不立即激活
# --------------------------------------------------------------------- #
def test_publish_without_activation_keeps_active_version(app: Sanic):
    @app.exception(ServerError)
    async def h(request, exception):
        return text("a", 418)

    @app.route("/r")
    async def r(request):
        raise ServerError("x")

    v1 = app.publish_error_policy("v1")
    v2 = app.publish_error_policy("v2", activate=False)

    assert app.error_handler.policy_registry.active is v1
    assert v2 is not v1

    _, response = app.test_client.get("/r")
    assert response.status == 418
    assert response.headers[ERROR_POLICY_HEADER] == "v1"

    app.activate_error_policy("v2")
    _, response = app.test_client.get("/r")
    assert response.headers[ERROR_POLICY_HEADER] == "v2"


# --------------------------------------------------------------------- #
# ASGI 受理路径同样固定版本
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_asgi_request_pins_policy(app: Sanic):
    @app.exception(ServerError)
    async def handle_server_error(request, exception):
        return text("asgi-mapped", 418)

    @app.route("/boom")
    async def boom(request):
        raise ServerError("boom")

    app.publish_error_policy("v1")

    request, response = await app.asgi_client.get("/boom")

    assert response.status == 418
    assert response.text == "asgi-mapped"
    assert response.headers[ERROR_POLICY_HEADER] == "v1"
    assert request.error_policy.id == "v1"
    assert request.ctx.error_audit[-1]["policy"] == "v1"


def test_replace_invalidates_negative_mro_cache():
    class Sub(ServerError):
        pass

    handler = ErrorHandler()

    # 热更新前无子类处理器：lookup 缓存 (Sub, None) -> None
    assert handler.lookup(Sub(), None) is None

    first = lambda r, e: "first"  # noqa: E731
    second = lambda r, e: "second"  # noqa: E731
    handler.add(ServerError, first, replace=True)
    assert handler.lookup(Sub(), None) is first

    handler.add(ServerError, second, replace=True)
    assert handler.lookup(Sub(), None) is second
