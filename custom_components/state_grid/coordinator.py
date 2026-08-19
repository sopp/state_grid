from __future__ import annotations
from datetime import timedelta
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .data_client import StateGridDataClient
from .const import DOMAIN
from .utils.logger import LOGGER
from . import energy_stats


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
        # - 重启场景（有缓存数据）：不强制刷新，由 refresh_data 内部 12 小时判断决定
        #   这样可以避免重启就触发 API 调用，消耗 RK001 日额度
        has_cached_data = bool(self.data_client.powerUserList)
        force_refresh = not has_cached_data
        await self.data_client.refresh_data(force_refresh=force_refresh)
        data = self.data_client.get_door_account()
        # 能源面板外部统计导入（幂等：无新日期则为空操作，不增加 API 调用）
        await energy_stats.async_import_energy_statistics(
            self.hass, self.data_client.get_door_account_list(), data
        )
        return data
