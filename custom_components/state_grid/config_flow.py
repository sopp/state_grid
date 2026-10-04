"""国家电网集成的配置向导：只走 App 通道。

网页那条登录链（验证码、邮箱降级、会话密钥）已经整段删掉——95598 升级后每个响应都用
浏览器里的客户端公钥加密，离线客户端解不开，所以网页那份数据只能由真实浏览器抓
（仓库 state_grid_docker 的 sidecar 容器），本集成不再自己登网页。
"""
import hashlib

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

from .app_api import AppChannel
from .const import DOMAIN
from .data_client import StateGridDataClient
from .utils.logger import LOGGER

USER_HINT = (
    "登录与取数都走国家电网 App 的接口：没有验证码，也不需要大模型。"
    "配好之后按刷新间隔取数，默认 12 小时一次（一天两次）。\n\n"
    "网页那份数据（目前只剩抄表读数一格，而它从站点升级起本身就回空值）要的话"
    "另装 sidecar 浏览器容器：仓库 state_grid_docker，它抓完 POST 给本集成的 webhook。"
)

# App 登录失败的原因 → 配置页的错误键。"密码错了"和"今天被限流"要让用户做的事完全不同，
# 都回一句"登录失败"只会让人反复改密码
APP_ERROR_KEYS = {
    "invalid_auth": "invalid_auth",
    "rate_limited": "rk001_rate_limit",
    "cannot_connect": "cannot_connect",
    "unknown": "app_login_failed",
}


async def app_sign_in(hass, account: str, password: str) -> tuple[bool, str]:
    """用明文密码做一次 App 登录，返回 (是否成功, 配置页错误键)。

    App 接口收的是密码的 md5 摘要，HA 里存的也正是摘要；明文只在这次调用里用完就丢，
    不进配置项、不进 store、不进日志。
    """
    channel = AppChannel(hass, account, hashlib.md5(password.encode()).hexdigest())
    if await channel.async_login(force=True):
        return True, ""
    return False, APP_ERROR_KEYS.get(channel.last_error, "app_login_failed")


class StateGridConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """配置步骤：手机号 + 密码，App 通道登录成功就建条目。"""

    VERSION = 12

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if self.hass.data.get(DOMAIN):
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        phone = ""
        password = ""

        if user_input is not None:
            phone = str(user_input.get("phone", "")).strip()
            password = str(user_input.get("password", ""))

            if not phone or not password:
                errors["base"] = "invalid_auth"
            elif not phone.isdigit():
                errors["base"] = "invalid_phone"
            else:
                ok, err_key = await app_sign_in(self.hass, phone, password)
                if ok:
                    dc = StateGridDataClient(hass=self.hass, config=None)
                    # 凭证只落 store：App 通道每轮取数读的就是这两项。网页那套会话字段
                    # （keyCode/accessToken/userInfo…）已经没有生产者，不再往 store 里写
                    dc.account = phone
                    dc.password = hashlib.md5(password.encode()).hexdigest().upper()
                    try:
                        await dc.save_data()
                    except Exception:
                        LOGGER.exception("保存 state_grid.config 失败，但登录已成功。")
                    self.hass.data[DOMAIN] = dc
                    LOGGER.warning("[配置] App 通道登录成功，接下来按刷新间隔供数")
                    return self.async_create_entry(title=f"国家电网 - {phone}", data={})
                errors["base"] = err_key

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("phone", default=phone): selector(
                        {"text": {"type": "text"}}
                    ),
                    vol.Required("password", default=password): selector(
                        {"text": {"type": "password"}}
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"how_it_works": USER_HINT},
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry):
        """返回选项流程。"""
        return OptionsFlowHandler(entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """集成选项：改国网登录密码、改刷新间隔。"""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # 新版 HA（Python 3.14 + 最新 HA）的 OptionsFlow 基类把 config_entry
        # 设为只读 property（无 setter），同时基类没有定义接受参数的 __init__，
        # 所以：
        #   - super().__init__(config_entry) 会触发 object.__init__() 报 TypeError
        #   - self.config_entry = config_entry 会触发 AttributeError
        # 解决方案：不碰 config_entry 这个名字，用自己的私有属性 _entry 保存。
        self._entry = config_entry

    async def async_step_init(self, user_input=None):
        current = {**(self._entry.data or {}), **(self._entry.options or {})}
        errors: dict[str, str] = {}
        new_data: dict[str, object] = {}
        interval = str(current.get("refresh_interval", 12))

        if user_input is not None:
            raw_interval = user_input.get("refresh_interval")
            interval = str(raw_interval or interval)
            if raw_interval:
                try:
                    new_data["refresh_interval"] = max(12, min(48, int(str(raw_interval).strip())))
                except (ValueError, TypeError):
                    errors["refresh_interval"] = "invalid_interval"

            new_password = str(user_input.get("new_password") or "").strip()
            if new_password and not errors:
                dc = self.hass.data.get(DOMAIN)
                account = str(getattr(dc, "account", "") or "")
                if dc is None or not account:
                    # 运行中没有实例就没人能报出账号，也不该拿空账号去试密码
                    errors["new_password"] = "no_account"
                else:
                    ok, err_key = await app_sign_in(self.hass, account, new_password)
                    if ok:
                        # App 登录已经把新会话写进设备 store；这里再把新摘要落进
                        # state_grid.config，下一轮取数就用它
                        dc.password = hashlib.md5(new_password.encode()).hexdigest().upper()
                        try:
                            await dc.save_data()
                        except Exception:
                            LOGGER.exception("[改密码] App 登录成功但保存 store 失败")
                        LOGGER.warning("[改密码] 新密码经 App 通道验证通过，已写入 store")
                    else:
                        errors["new_password"] = err_key

            if not errors:
                if new_data:
                    dc = self.hass.data.get(DOMAIN)
                    if dc is not None and "refresh_interval" in new_data:
                        dc.refresh_interval = new_data["refresh_interval"]
                    return self.async_create_entry(title="", data=new_data)
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "refresh_interval", default=interval,
                        description="刷新间隔（小时，填 12-48 之间的整数）",
                    ): selector({"text": {"type": "text"}}),
                    vol.Optional(
                        "new_password", default="",
                        description="修改国家电网密码时填写（留空不修改）；填写后会走 App 通道验证一次",
                    ): selector({"text": {"type": "password"}}),
                }
            ),
            errors=errors,
        )
