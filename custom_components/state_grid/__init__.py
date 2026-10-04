from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.components.webhook import (
    async_register as webhook_register,
    async_unregister as webhook_unregister,
)
from aiohttp import web
import secrets
from .const import DOMAIN
from .utils.logger import LOGGER
from .utils.store import async_load_from_store
from .data_client import StateGridDataClient
from .config_flow import StateGridConfigFlow

PLATFORMS: list[Platform] = [Platform.SENSOR]
CONF_PUSH_WEBHOOK = "push_webhook_id"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """当用户在 UI 里点击"添加集成"并完成配置时调用。"""
    config = await async_load_from_store(hass, "state_grid.config") or None
    data_client = StateGridDataClient(hass=hass, config=config)

    # 配置优先级：entry.options > entry.data > 存储中的 config
    # entry.options 是用户在"配置"按钮中修改的最新值
    merged = {**(entry.data or {}), **(entry.options or {})}
    # LLM 三项与备用邮箱已经从本集成移除（网页登录链整段删了）；旧 entry.data 里
    # 残留的那些键会被直接忽略，不读也不回写
    if "refresh_interval" in merged:
        try:
            data_client.refresh_interval = max(12, int(merged["refresh_interval"]))
        except (ValueError, TypeError):
            pass

    hass.data[DOMAIN] = data_client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _async_setup_push_webhook(hass, entry, data_client)
    return True


async def _async_setup_push_webhook(hass: HomeAssistant, entry: ConfigEntry,
                                    data_client: StateGridDataClient) -> None:
    """注册 sidecar 推送入口：/api/webhook/<id>。

    id 是随机 128 位，首次 setup 生成后写进 entry.data 持久化，用户不用在任何界面里填。
    只允许内网推送（sidecar 和 HA 在同一台 NAS 局域网上），外网访问直接拒。
    """
    webhook_id = (entry.data or {}).get(CONF_PUSH_WEBHOOK)
    if not webhook_id:
        webhook_id = secrets.token_hex(16)
        hass.config_entries.async_update_entry(
            entry, data={**(entry.data or {}), CONF_PUSH_WEBHOOK: webhook_id})
    # 每次 setup 都打一遍，日志滚没了还能去 .storage/core.config_entries 里捞。
    # 用 warning 级：这台 HA 容器的控制台只放 WARNING 以上，info 级的地址等于没打。
    LOGGER.warning("sidecar 推送地址: /api/webhook/%s", webhook_id)

    async def handle_push(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        try:
            bundle = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "body 不是 JSON"}, status=400)
        # 这一句只在真收到 HTTP 推送时才会出现，是"sidecar 到底有没有在供数"的唯一正证；
        # 供数本身另有 ingest_push 那行带来源的日志，两边对得上才算真有人在推
        LOGGER.warning("收到 webhook 推送：来自 %s，载荷 %d 项",
                       request.remote, len((bundle or {}).get("items") or []))
        count = await data_client.ingest_push(bundle)
        # 先回响应再刷新：解析 800 行数据要几秒，别让 sidecar 干等一个可能超时的大请求
        coordinator = data_client.coordinator
        if coordinator is not None:
            hass.async_create_task(coordinator.async_refresh())
        else:
            LOGGER.warning("收到 sidecar 推送但 coordinator 还没就绪，数据留在缓存里等下一次轮询")
        return web.json_response({"ok": True, "stored": count, "meta": data_client.push_meta})

    webhook_register(hass, DOMAIN, "state_grid sidecar push", webhook_id, handle_push, local_only=True)
    entry.async_on_unload(lambda: webhook_unregister(hass, webhook_id))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载集成时调用。"""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.pop(DOMAIN, None)
    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options 更新时触发，重新加载集成使配置立即生效。"""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """ConfigEntry 版本迁移。

    HA 在打开 options 配置页时，如果 entry.version < config_flow.VERSION，
    会调用此方法。如果不实现，HA 会报错 500。
    """
    target_version = StateGridConfigFlow.VERSION
    LOGGER.info("ConfigEntry 迁移: 版本 %s -> %s", entry.version, target_version)
    # 我们不需要做任何数据结构变换，直接升级版本号即可
    # 因为所有字段都是 Optional，旧版本数据能兼容新版本
    if entry.version < target_version:
        hass.config_entries.async_update_entry(entry, version=target_version)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """移除集成时不删除存储文件。"""
    return None
