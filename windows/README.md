# 云连接助手 Windows x64 中继版

解压 [Releases 中的 Windows ZIP](https://github.com/xinxinzai98/cloud-link-assistant/releases)。包内带 Python。启动前请开启自己的代理客户端。

## 配置代理与检查连接

默认读取 Windows 系统 HTTP／HTTPS 代理。运行 `Configure-Proxy.cmd` 可以输入 `auto` 使用自动模式，也可以填写自己的本机 HTTP／混合地址，例如 `127.0.0.1:10809`。只有 SOCKS5 入口时，请在代理客户端启用 HTTP／混合入口。

设置保存在当前用户的 `%LOCALAPPDATA%\CloudLinkAssistant\proxy-settings.json`。两个启动器及连接检测共用这份设置。修改设置后，本机模型中继的新请求使用新地址，云端中继需要重新启动。

运行 `Check-Connection.cmd`，分别检查代理端口、模型服务和云端服务。它会报告未监听的端口、TLS 证书错误、连接超时和 HTTP 拒绝状态。服务入口返回 `401` 表示已到达登录校验，真实聊天仍使用 Codex 自己的账号。

## 本机聊天

运行 `Start-Model.cmd`。在浏览器打开 `http://127.0.0.1:19847/healthz`，返回 `running: true` 后，备份 `%USERPROFILE%\.codex\config.toml`，在文件顶层设置以下选项并重开 Codex。

```toml
openai_base_url = "http://127.0.0.1:19847"
```

此设置用于内置 `openai` 提供商和 ChatGPT 登录。已有 `openai_base_url` 时先保留原值，避免重复定义。停止使用时先恢复该项并重开 Codex，然后关闭中继窗口。

## 云端中继

运行 `Start-Cloud.cmd`，在 `127.0.0.1:18443` 启动云端 TLS 透传。此端口供已有的云端路由接入。Windows 系统 hosts 中的云端域名须保留正常解析。启动器只运行中继，桌面端的云路由需单独配置。

以下命令可单独检查云端中继。成功通过 TLS 并收到 `401` 表示已到达官方登录校验。

```powershell
curl.exe --noproxy "*" --connect-to "codex-cloud-backend.chatgpt.com:443:127.0.0.1:18443" -I https://codex-cloud-backend.chatgpt.com/
```

## 使用其他代理端口

在解压目录打开 PowerShell，可以临时指定代理端口。下面的参数只对这次进程有效，保存长期设置请使用 `Configure-Proxy.cmd`。

```powershell
.\python\python.exe -B .\windows_bridge.py model --port 19847 --proxy-port 7890
.\python\python.exe -B .\windows_bridge.py cloud --port 18443 --proxy-port 7890
```

从源码运行时，用 `py -3.14` 替换上述命令中的 `.\python\python.exe`。需要登录自启时，可在任务计划程序为各进程创建当前用户的登录任务，并将启动延迟设到代理就绪后。中继日志是解压目录里的 `model.log` 和 `cloud.log`。

源码仓库中运行 `python windows/build.py` 可重新生成 ZIP 和校验文件。解压后，可用包内的 `python\python.exe` 运行源码仓库的 `test_https_compat.py`，检查模型中继的转发行为。
