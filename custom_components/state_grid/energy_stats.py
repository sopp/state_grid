"""国家电网日用电 → HA 能源面板（外部统计）导入模块。

需求说明:  docs/能源面板接入-需求说明.md
开发文档:  docs/能源面板接入-开发文档.md

核心机制
--------
- 每个户号一条外部统计，ID: ``state_grid:energy_{consNo}``
- 能源面板按 ``sum[n] - sum[n-1]`` 计算日用电，因此每行的 ``sum`` 必须是
  **截至当日结束的累计值**（游标法链式累加），而不是当日电量本身
- 每个日期只在首次出现时导入一次，已导入的日期不回头修正（历史数据
  若出错，用 reset 服务重建）
- 数据源: ``recent_30_daily_ele_list``（滚动 30 天窗口，升序，止于昨天，
  与 ``daily_lasted_date`` / ``daily_ele_num`` 同源）
- 导入挂载在 coordinator 刷新后，幂等（无新日期即为空操作，不增加 API 调用）
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import DOMAIN
from .utils.logger import LOGGER
from .utils.store import async_load_from_store, async_save_to_store

STORAGE_KEY = "state_grid.energy_cursor"
SERVICE_RESET = "reset_energy_statistics"

# 国网数据为北京时间；时间戳统一用北京时区当地午夜，不跟随 HA 时区设置
_BEIJING_TZ = dt_util.get_time_zone("Asia/Shanghai")


def _statistic_id(cons_no: str) -> str:
    return f"{DOMAIN}:energy_{cons_no}"


def _statistic_metadata(statistic_id: str, name: str) -> dict[str, Any]:
    """按 HA 版本构造统计元数据。

    - HA 2025.10+：使用 ``mean_type`` + ``unit_class``（``has_mean`` 已弃用，
      2026.11 起必须用新字段）
    - HA 2025.10 之前：使用 ``has_mean`` 布尔
    """
    metadata: dict[str, Any] = {
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "name": name,
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        "has_sum": True,
    }
    try:
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.util.unit_conversion import EnergyConverter

        metadata["mean_type"] = StatisticMeanType.NONE
        metadata["unit_class"] = EnergyConverter.UNIT_CLASS
    except ImportError:
        # 旧版 HA：只有 has_mean/has_sum 布尔字段
        metadata["has_mean"] = False
    return metadata


def _beijing_midnight(day: str) -> datetime:
    """'YYYYMMDD' → 北京时间当日 00:00（时区感知，recorder 内部会转 UTC）。"""
    naive = datetime.strptime(day, "%Y%m%d")
    return naive.replace(tzinfo=_BEIJING_TZ)


def _recorder_instance(hass: HomeAssistant):
    from homeassistant.components.recorder import get_instance

    return get_instance(hass)


async def async_import_energy_statistics(
    hass: HomeAssistant,
    door_accounts: list[dict],
    data_by_cons_no: dict[str, dict],
) -> None:
    """推进所有户号的能源统计游标（幂等，无新日期则为空操作）。

    door_accounts:   get_door_account_list()（含 elecAddr_dst 等户信息）
    data_by_cons_no: coordinator.data（consNo_dst → 数据字典）
    """
    if not door_accounts or not data_by_cons_no:
        return

    try:
        cursor = await async_load_from_store(hass, STORAGE_KEY)
    except Exception:
        LOGGER.exception("读取能源统计游标失败，按全新导入处理")
        cursor = {}
    if not isinstance(cursor, dict):
        cursor = {}

    changed = False
    for account in door_accounts:
        cons_no = account.get("consNo_dst")
        if not cons_no:
            continue
        data = data_by_cons_no.get(cons_no)
        if not data:
            continue
        try:
            if await _async_import_one(hass, cursor, account, data):
                changed = True
        except Exception:
            LOGGER.exception("导入能源统计失败: 户号 %s", cons_no)

    if changed:
        try:
            await async_save_to_store(hass, STORAGE_KEY, cursor)
        except Exception:
            LOGGER.exception("保存能源统计游标失败")


async def _async_import_one(
    hass: HomeAssistant,
    cursor: dict,
    account: dict,
    data: dict,
) -> bool:
    """导入单个户号的新日期并推进游标；有进展返回 True。"""
    graph = data.get("recent_30_daily_ele_list") or []
    if not graph:
        return False

    # 一致性校验：图表最新一条（升序列表的最后一个）必须与
    # daily_lasted_date / daily_ele_num 一致，防止数据错位时误导入
    expected_date = data.get("daily_lasted_date")
    expected_ele = data.get("daily_ele_num")
    newest = graph[-1]
    if expected_date:
        if newest.get("day") != str(expected_date).replace("-", ""):
            LOGGER.warning(
                "能源统计跳过户号 %s: 图表最新日期 %s 与 daily_lasted_date %s 不一致",
                account.get("consNo_dst"), newest.get("day"), expected_date,
            )
            return False
    if expected_ele is not None:
        try:
            if abs(float(newest.get("ele") or 0) - float(expected_ele)) > 0.005:
                LOGGER.warning(
                    "能源统计跳过户号 %s: 图表最新电量 %s 与 daily_ele_num %s 不一致",
                    account.get("consNo_dst"), newest.get("ele"), expected_ele,
                )
                return False
        except (TypeError, ValueError):
            pass

    cons_no = account["consNo_dst"]
    stat_id = _statistic_id(cons_no)
    state = cursor.get(cons_no) or {}
    last_day = state.get("last_day")  # 'YYYY-MM-DD' 或 None
    last_sum = state.get("last_sum")  # float 或 None

    # 游标缺失但统计已有数据：从 recorder 最后一行恢复，
    # 避免下次导入按"基准 0"重写导致历史跳变
    if last_day is None:
        last_row = await _async_get_last_row(hass, stat_id)
        if last_row is not None:
            recovered = _cursor_from_row(last_row)
            if recovered is None:
                LOGGER.warning(
                    "能源统计跳过户号 %s: 统计 %s 已有数据但游标恢复失败",
                    cons_no, stat_id,
                )
                return False
            last_day, last_sum = recovered

    # 构造待导入日期（升序、晚于游标），sum 链式累加
    pending: list[tuple[str, float, float]] = []
    running: float | None = last_sum
    cursor_day = last_day.replace("-", "") if last_day else None
    for entry in graph:
        day = str(entry.get("day") or "")
        if len(day) != 8 or not day.isdigit():
            continue
        if cursor_day is not None and day <= cursor_day:
            continue
        try:
            ele = float(entry.get("ele") or 0)
        except (TypeError, ValueError):
            ele = 0.0
        if running is None:
            running = 0.0  # 全新统计：首日基准 0（面板上首日显示完整电量，行为正确）
        running = round(running + ele, 2)
        pending.append((day, ele, running))

    if not pending:
        return False

    stats = [
        {"start": _beijing_midnight(day), "state": ele, "sum": total}
        for day, ele, total in pending
    ]
    name = f"国家电网 {account.get('elecAddr_dst') or cons_no} 日用电"
    try:
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
        )

        # 注意：async_add_external_statistics 是 @callback，直接调用，不需要 await
        async_add_external_statistics(
            hass, _statistic_metadata(stat_id, name), stats
        )
    except Exception:
        LOGGER.exception("导入能源统计失败: %s", stat_id)
        return False

    # 导入成功才推进游标
    last_day, last_sum = pending[-1][0], pending[-1][2]
    cursor[cons_no] = {
        "last_day": f"{last_day[:4]}-{last_day[4:6]}-{last_day[6:]}",
        "last_sum": last_sum,
    }
    LOGGER.info(
        "能源统计导入 %s 共 %d 天，游标推进至 %s（累计 %.2f kWh）",
        stat_id, len(pending), last_day, last_sum,
    )
    return True


async def _async_get_last_row(
    hass: HomeAssistant, statistic_id: str
) -> dict[str, Any] | None:
    """返回统计的最后一行（无数据/出错返回 None）。"""
    try:
        from homeassistant.components.recorder.statistics import (
            get_last_statistics,
        )

        result = await _recorder_instance(hass).async_add_executor_job(
            get_last_statistics,
            hass,
            1,
            statistic_id,
            False,  # 不做单位换算，取库内原始值
            {"sum"},
        )
        rows = (result or {}).get(statistic_id) or []
        return rows[0] if rows else None
    except Exception:
        LOGGER.debug("查询统计 %s 最后一行失败", statistic_id, exc_info=True)
        return None


def _cursor_from_row(row: dict[str, Any]) -> tuple[str, float] | None:
    """从 recorder 行恢复 (last_day, last_sum)，失败返回 None。

    行中的 start 自 HA 2023.3 起为 UTC 时间戳（int/float）。
    """
    start = row.get("start")
    total = row.get("sum")
    if start is None or total is None:
        return None
    try:
        if isinstance(start, (int, float)):
            start_dt = dt_util.utc_from_timestamp(start)
        elif isinstance(start, datetime):
            start_dt = dt_util.as_utc(start)
        else:
            return None
        last_day = start_dt.astimezone(_BEIJING_TZ).strftime("%Y-%m-%d")
        return last_day, float(total)
    except (TypeError, ValueError, OverflowError):
        return None


async def async_register_reset_service(hass: HomeAssistant) -> None:
    """注册重置服务（单实例集成，重复注册时跳过）。"""
    if hass.services.has_service(DOMAIN, SERVICE_RESET):
        return

    async def handle_reset(call: ServiceCall) -> None:
        await _async_reset(hass, call.data.get("cons_no"))

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET,
        handle_reset,
        schema=vol.Schema({vol.Optional("cons_no"): cv.string}),
    )


async def _async_reset(hass: HomeAssistant, cons_no: str | None) -> None:
    """删除外部统计并重置游标；下次数据刷新时自动按基准 0 全量重导入。

    仅当统计删除成功后才重置游标：若删除失败而游标已重置，
    下次刷新会按基准 0 覆盖现有统计行，导致历史跳变。
    """
    data_client = hass.data.get(DOMAIN)
    ids: list[str] = []
    if cons_no:
        ids = [_statistic_id(cons_no)]
    elif data_client is not None:
        ids = [
            _statistic_id(acc["consNo_dst"])
            for acc in data_client.get_door_account_list()
            if acc.get("consNo_dst")
        ]

    if ids:
        try:
            await _recorder_instance(hass).async_clear_statistics(ids)
        except Exception:
            LOGGER.exception("删除能源统计失败，游标未重置，请重试: %s", ids)
            return

    cursor = await async_load_from_store(hass, STORAGE_KEY) or {}
    if cons_no:
        cursor.pop(cons_no, None)
    else:
        cursor.clear()
    await async_save_to_store(hass, STORAGE_KEY, cursor)

    LOGGER.warning(
        "能源统计已重置: %s（下次数据刷新时按基准 0 重新导入）", ids or "全部"
    )
