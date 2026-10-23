#!/bin/zsh
set -eu
support_directory='/Library/Application Support/CloudLinkAssistant'
if [[ ! -x "$support_directory/python/bin/python3" ]]; then
    print -u2 '未找到已安装的开机启动组件。'
    exit 1
fi
exec /usr/bin/sudo "$support_directory/python/bin/python3" -I -B "$support_directory/autostart.py" disable
