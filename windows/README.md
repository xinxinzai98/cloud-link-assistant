# 云连接助手 Windows x64 中继版

解压 [Windows ZIP](https://github.com/xinxinzai98/cloud-link-assistant/releases/download/v1.2.1-windows.1/cloud-link-assistant-v1.2.1-windows.1-x64.zip)。包内带 Python。启动前请开启能访问 Codex 官方地址的本机 HTTP 代理。两个启动器都使用 `127.0.0.1:7890` 作为代理入口。

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

在解压目录打开 PowerShell，把示例中的 `7890` 换成现有 HTTP 代理端口。

```powershell
.\python\python.exe -B .\windows_bridge.py model --port 19847 --proxy-port 7890
.\python\python.exe -B .\windows_bridge.py cloud --port 18443 --proxy-port 7890
```

从源码运行时，用 `py -3.14` 替换上述命令中的 `.\python\python.exe`。需要登录自启时，可在任务计划程序为各进程创建当前用户的登录任务，并将启动延迟设到代理就绪后。中继日志是解压目录里的 `model.log` 和 `cloud.log`。

源码仓库中运行 `python windows/build.py` 可重新生成 ZIP 和校验文件。解压后，可用包内的 `python\python.exe` 运行源码仓库的 `test_https_compat.py`，检查模型中继的转发行为。
