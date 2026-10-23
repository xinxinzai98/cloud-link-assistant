# 云连接助手

macOS 原生 AppKit 窗口，用现有系统代理连接 Codex 云服务，并提供本机官方 Antigravity 的代理启动与授权网络检查入口。使用 AppleScriptObjC 和 Python 标准库，没有新增第三方 Python 依赖。

## 下载 macOS App

[下载 v1.0.0 Apple Silicon 版](https://github.com/xinxinzai98/cloud-link-assistant/releases/download/v1.0.0/cloud-link-assistant-v1.0.0-macos-arm64.zip) · [发布页与校验文件](https://github.com/xinxinzai98/cloud-link-assistant/releases/tag/v1.0.0)

适用于 M 系列芯片 Mac。Releases 版内置 Python，无需另外安装 Homebrew 或 Python。解压 ZIP，将 App 拖入“应用程序”，开启已有系统代理后打开。Intel Mac 暂未提供经过验证的安装包。

升级已有版本时，先在原入口停止旧中继，再从新版启动。保留旧状态目录及其恢复备份，直到停止和恢复设置都已确认完成。

App 使用临时签名，未完成 Apple 公证。若 macOS 提示无法验证开发者，请核对下载来源与校验值，再按 [Apple 的说明](https://support.apple.com/guide/mac-help/open-a-mac-app-from-an-unknown-developer-mh40616/mac)在“系统设置 → 隐私与安全性”中确认打开这个 App。

## 功能与系统改动

- 启动、停止、查询中继状态，异常后恢复自有设置。
- 仅监听 `127.0.0.1:443`，为 `codex-cloud-backend.chatgpt.com` 临时添加带标记的 hosts 映射。
- GUI 启动时只为 `Wi-Fi` 追加这一个域名的系统代理例外；停止时撤销自有项，保留其他配置。
- 通过回环地址的系统 HTTP 代理发送 CONNECT，透传 TLS 字节，保留客户端的 SNI 和证书校验。
- 通过代理查询加密 DNS，避免 hosts 映射导致上游连接绕回本机。
- 管理进程维护设置和恢复备份，网络转发进程以原普通用户运行。
- 打开 Antigravity；未运行时附带进程级代理参数，已运行时只显示现有窗口。

启动和恢复设置需要 macOS 管理员授权。关闭窗口保留后台；退出时可选择保留后台或停止中继。没有开机自启。

## 环境

- macOS，内置 `osacompile`、`codesign` 和 AppKit；本机使用 Python 3.14 验证，构建脚本要求 Python 3.11+。
- 已开启系统 HTTPS 代理，地址为 `127.0.0.1`、`localhost` 或 `::1`，代理支持 HTTP CONNECT。
- `127.0.0.1:443` 未被其他服务占用；GUI 当前按 `Wi-Fi` 网络服务处理代理例外。
- 使用 Antigravity 入口时，官方应用应安装在 `/Applications/Antigravity.app`。

上述 Python 与编译工具用于源码构建。默认源码构建复用指定的本机 Python；Releases 安装包随 App 打包可搬移的 Python 运行环境。

## 构建

```sh
python3 build.py
```

产物为 `dist/云连接助手 1.0.app` 和 `dist/云连接助手 1.0.zip`。已有产物不会被覆盖，可用 `--output` 指向新目录。构建只编译与签名，不启动 App 或中继。

```sh
python3 build.py --python /absolute/path/to/python3 --output /absolute/path/to/new-output
```

制作可直接下载使用的安装包时，传入完整的可搬移 Python 目录。程序会把解释器、标准库和上游许可证一起复制进 App；运行时按 App 所在位置寻找内部解释器。

```sh
python3 build.py --bundle-python /absolute/path/to/standalone/python --output /absolute/path/to/new-output
```

v1.0.0 的运行环境来源、版本和 SHA-256 见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。打包采用 `ditto`，保留运行环境的符号链接和可执行权限。

默认不探测其他目录中的旧中继。需要接管既有中继时，在保留原状态目录及其恢复备份的前提下构建：

```sh
python3 build.py --legacy-state /absolute/path/to/existing/.state
```

路径只嵌入本机构建产物，源码不包含个人目录。新启动的状态目录为：

```text
~/Library/Application Support/云连接助手/relay-state
```

## 使用

1. 开启现有代理客户端的系统代理，无需 TUN。
2. 打开构建出的 App，查看状态后按需启动，并在系统窗口输入管理员密码。
3. 停止中继后，程序撤销自有 hosts 映射和代理例外。
4. 若显示“需要恢复设置”，保留状态目录和备份，使用“恢复设置”。

Antigravity 的 Google 登录、账号选择和授权在官方应用及浏览器完成。助手不读取或保存登录令牌。匿名 TLS 检查通过只代表网络连通，不代表登录成功、账号地区符合条件或模型可用。地区申请及账号受理也由 Google 处理。

## 验证

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s . -p 'test_*.py' -q
```

现有 19 项测试覆盖 hosts 恢复及并发修改保留、代理例外、DNS 地址校验、TLS 透传和证书校验、隔离进程生命周期，以及 Antigravity 启动参数。测试使用临时目录与本地随机端口；不修改真实 hosts、Wi-Fi 设置或账号，也不启动真实 Antigravity。

构建通过与隔离测试通过不等于真实管理员首启、长期云连接或 Antigravity 登录验收通过。现有用户环境下仍可能因代理和网络中断而重连。

## 源码

- `main.applescript`：原生窗口、状态与启停入口；解释器和可选旧状态路径由构建时填入。
- `relay.py`：固定域名的 CONNECT 中继与设置恢复。
- `antigravity_helper.py`：官方应用入口、进程代理及匿名 TLS 检查。
- `build.py`：编译、临时签名和打包。
- `THIRD_PARTY_NOTICES.md`：随包 Python 的来源与许可证位置。
- `test_relay.py`、`test_antigravity.py`：隔离测试。

Git 只保存源码与说明。运行日志、hosts 备份、账号信息、OAuth 回调、地区申请材料、截图和 App 产物不纳入仓库。
