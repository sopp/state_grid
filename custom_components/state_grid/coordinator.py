from __future__ import annotations
from datetime import timedelta
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .data_client import StateGridDataClient
from .const import DOMAIN
from .utils.logger import LOGGER


class StateGridCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=300)
        )
        self.data_client: StateGridDataClient = hass.data[DOMAIN]

    async def _async_update_data(self):
        # 智能判断是否需要强制刷新：
        # - 首次安装（powerUserList 为空）：必须强制刷新，否则永远拉不到数据
        # - sidecar 刚推来一批浏览器抓的响应：必须刷新一次，不然这批数据没人消费就过期了
        # - 重启场景（有缓存数据）：不强制刷新，由 refresh_data 内部 12 小时判断决定
        #   这样可以避免重启就触发 API 调用，消耗 RK001 日额度
        has_cached_data = bool(self.data_client.powerUserList)
        # push_pending 由 ingest_push 点亮、refresh_data 开头熄灭，是"有一次刷新欠着"的一次性标记；
        # 用 push_cache 非空当条件不行：里面可能有本轮用不上的形状，会让每轮都被强制刷
        push_pending = bool(getattr(self.data_client, "push_pending", False))
        force_refresh = not has_cached_data or push_pending
        await self.data_client.refresh_data(force_refresh=force_refresh)
        return self.data_client.get_door_account()
