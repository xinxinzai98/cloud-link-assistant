#!/bin/zsh
set -eu
app_target="${1:-$HOME/Applications/云连接助手 1.0.app}"
task_runtime="$app_target/Contents/Resources/python/bin/python3"
script_directory="${0:A:h}"
if [[ ! -x "$task_runtime" ]]; then
    print -u2 '未找到带内置 Python 的助手，请将 App 完整路径作为参数传入。'
    exit 1
fi
print -r -- '即将安装两个 macOS 原生启动项：登录时打开助手，中继等待登录和系统代理就绪后启动。'
print -r -- '程序副本和恢复状态保存在 /Library/Application Support/CloudLinkAssistant。'
print -r -- '现有中继会短暂重连，原恢复记录保留；只处理既有的一个云域名和 Wi-Fi 代理例外。'
exec /usr/bin/sudo "$task_runtime" -I -B "$script_directory/autostart.py" install --app "$app_target"
