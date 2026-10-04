DOMAIN = "state_grid"
PACKAGE_NAME = "custom_components.state_grid"
VERSION = "0.9.3"   # 和 manifest.json 的 version 保持一致：store 里的 dataVersion 读的是这一份
VERSION_STORAGE = 21
STORAGE_KEY = "state_grid.config"

# 流控码：11401 = RK001「网络连接超时（RK001）,请重试！」。它是**按登录标识**计的日额度，
# 换客户端、换 IP 都没用；App 接口面回的是同一个字符串码，所以拿它当"密码没错但今天到顶了"的判据。
RATE_LIMIT_CODES = {"11401"}
