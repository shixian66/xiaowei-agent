"""闭集 Web intent 的解析、授权与同源路径重建。"""

from collections.abc import Iterable
from typing import Final
from urllib.parse import quote, urlencode

from pydantic import ValidationError

from xiaowei_agent.contracts import (
    IdentitySource,
    ProductRole,
    WebReturnIntent,
    WebReturnIntentKind,
)

_QUERY_FIELDS: Final = frozenset({"intent", "task_id", "request_id"})
_ALLOWED_DESTINATIONS: Final = {
    (IdentitySource.LOCAL_ADMIN, ProductRole.ADMIN): frozenset(
        {
            WebReturnIntentKind.WORKBENCH,
            WebReturnIntentKind.SAFE_TASK_DETAIL,
            WebReturnIntentKind.ADMIN_CENTER,
        }
    ),
    (IdentitySource.FEISHU, ProductRole.ADMIN): frozenset(
        {
            WebReturnIntentKind.WORKBENCH,
            WebReturnIntentKind.SAFE_TASK_DETAIL,
            WebReturnIntentKind.ADMIN_CENTER,
        }
    ),
    (IdentitySource.FEISHU, ProductRole.OPERATOR): frozenset(
        {
            WebReturnIntentKind.WORKBENCH,
            WebReturnIntentKind.SAFE_TASK_DETAIL,
        }
    ),
    (IdentitySource.FEISHU, ProductRole.USER): frozenset(
        {WebReturnIntentKind.SAFE_TASK_DETAIL}
    ),
}


class WebNavigationInputError(ValueError):
    """请求没有精确表达一个闭集 Web return intent。"""


def web_task_detail_path(task_id: str) -> str:
    """返回同源详情路径；不接受 base URL 或请求头。"""
    return f"/app/tasks/{quote(task_id, safe='')}"


def web_return_path(intent: WebReturnIntent) -> str:
    """从闭集 intent 重建站内路径；不存在任意 redirect 字符串入口。"""
    if intent.kind is WebReturnIntentKind.WORKBENCH:
        return "/app"
    if intent.kind is WebReturnIntentKind.SAFE_TASK_DETAIL:
        if intent.task_id is None:
            raise RuntimeError("safe task return intent is missing its task id")
        return web_task_detail_path(intent.task_id)
    if intent.kind is WebReturnIntentKind.ADMIN_CENTER:
        return "/admin"
    if intent.request_id is None:
        raise RuntimeError("activation return intent is missing its request id")
    return web_login_path(intent)


def web_login_path(intent: WebReturnIntent) -> str:
    """把闭集 intent 编码到登录入口；编码结果只供本应用重新解析。"""
    query: list[tuple[str, str]] = [("intent", intent.kind.value)]
    if intent.task_id is not None:
        query.append(("task_id", intent.task_id))
    if intent.request_id is not None:
        query.append(("request_id", intent.request_id))
    return f"/login?{urlencode(query)}"


def parse_web_return_intent(
    query_items: Iterable[tuple[str, str]],
) -> WebReturnIntent:
    """严格解析 query；未知字段、重复字段和不匹配引用一律拒绝。"""
    values: dict[str, str] = {}
    for name, value in query_items:
        if name not in _QUERY_FIELDS or name in values:
            raise WebNavigationInputError("web return intent query is invalid")
        values[name] = value
    if not values:
        return WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH)
    kind = values.pop("intent", None)
    if kind is None:
        raise WebNavigationInputError("web return intent kind is required")
    try:
        return WebReturnIntent.model_validate(
            {"kind": kind, **values},
            strict=False,
        )
    except ValidationError:
        raise WebNavigationInputError("web return intent query is invalid") from None


def web_return_intent_allowed(
    *, source: IdentitySource, role: ProductRole, intent: WebReturnIntent
) -> bool:
    """按认证来源、当前角色和 intent 判定最终页面；激活状态不是目的地。"""
    return intent.kind in _ALLOWED_DESTINATIONS.get((source, role), frozenset())


__all__ = [
    "WebNavigationInputError",
    "parse_web_return_intent",
    "web_login_path",
    "web_return_intent_allowed",
    "web_return_path",
    "web_task_detail_path",
]
