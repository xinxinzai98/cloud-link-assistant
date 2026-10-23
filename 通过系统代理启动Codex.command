#!/bin/zsh
# Only supply proxy variables to a newly started client; never stop a running one.
set -eu

app_target='/Applications/ChatGPT.app'
if [[ ! -d "$app_target" ]]; then
    print -u2 '未找到本机 Codex 桌面客户端。'
    exit 1
fi

proxy_snapshot=$(/usr/sbin/scutil --proxy)
proxy_enabled=$(print -r -- "$proxy_snapshot" | /usr/bin/awk '$1 == "HTTPSEnable" {print $3; exit}')
proxy_host=$(print -r -- "$proxy_snapshot" | /usr/bin/awk '$1 == "HTTPSProxy" {print $3; exit}')
proxy_port=$(print -r -- "$proxy_snapshot" | /usr/bin/awk '$1 == "HTTPSPort" {print $3; exit}')
if [[ "$proxy_enabled" != 1 || "$proxy_host" != (127.0.0.1|localhost|::1) || "$proxy_port" != <-> ]]; then
    print -u2 '请先开启回环地址上的系统 HTTPS 代理。'
    exit 1
fi
if (( proxy_port < 1 || proxy_port > 65535 )); then
    print -u2 '系统代理端口无效。'
    exit 1
fi

proxy_authority="$proxy_host"
[[ "$proxy_host" != '::1' ]] || proxy_authority='[::1]'
proxy_url="http://${proxy_authority}:${proxy_port}"
proxy_bypass="${NO_PROXY:-${no_proxy:-localhost,127.0.0.1,::1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12,100.64.0.0/10,.local}}"
# Carry over the system exceptions, including the domain mapped to the cloud relay.
proxy_exceptions=$(print -r -- "$proxy_snapshot" | /usr/bin/awk '
    $1 == "ExceptionsList" {inside = 1; next}
    inside && $1 == "}" {inside = 0}
    inside && $2 == ":" {print $3}
')
for proxy_exception in ${(f)proxy_exceptions}; do
    [[ "$proxy_exception" != '<local>' ]] || continue
    [[ "$proxy_exception" != \*.* ]] || proxy_exception="${proxy_exception#\*}"
    if [[ ",$proxy_bypass," != *",$proxy_exception,"* ]]; then
        proxy_bypass+=",${proxy_exception}"
    fi
done
launch_command=(/usr/bin/open)
for proxy_key in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
    launch_command+=(--env "${proxy_key}=${proxy_url}")
done
launch_command+=(--env "NO_PROXY=${proxy_bypass}" --env "no_proxy=${proxy_bypass}" -a "$app_target")

if [[ "${1:-}" == '--print-env' ]]; then
    for proxy_key in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
        print -r -- "${proxy_key}=${proxy_url}"
    done
    print -r -- "NO_PROXY=${proxy_bypass}"
    print -r -- "no_proxy=${proxy_bypass}"
    exit 0
fi

if [[ "${1:-}" == '--check' ]]; then
    print -r -- "客户端：${app_target}"
    print -r -- "本次启动代理：${proxy_url}"
    print -r -- "本次启动直连范围：${proxy_bypass}"
    print -r -- '检查通过；未启动或停止任何程序，未修改全局配置。'
    exit 0
fi

app_executable=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$app_target/Contents/Info.plist")
if /usr/bin/pgrep -x "$app_executable" >/dev/null 2>&1; then
    print -r -- 'Codex 主客户端仍在运行。请等任务结束后，按 ⌘Q 完整退出，再运行本文件。'
    if [[ -t 0 ]]; then read 'reply?按回车关闭此窗口…'; fi
    exit 1
else
    probe_result=$?
    if (( probe_result != 1 )); then
        print -u2 '无法确认客户端是否退出，已取消启动。'
        exit 1
    fi
fi

"${launch_command[@]}"
print -r -- '已请求以现有系统代理启动 Codex。请在新建本机聊天及云聊天中验证。'
