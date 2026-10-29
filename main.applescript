use AppleScript version "2.4"
use framework "AppKit"
use scripting additions

property parent : class "NSObject"
property mainWindow : missing value
property statusLabel : missing value
property detailLabel : missing value
property modelStatusLabel : missing value
property modelDetailLabel : missing value
property antiLabel : missing value
property startButton : missing value
property stopButton : missing value
property restoreButton : missing value
property enableStartupItem : missing value
property disableStartupItem : missing value
property reportLabel : missing value
property quitPanel : missing value
property allowQuit : false
property busy : false
property relayRunning : false
property modelRunning : false
property resourcesDir : ""
property pythonPath : "__CLOUDLINK_PYTHON__"
property bundledPython : __CLOUDLINK_BUNDLED__
property preferredState : ""
property legacyState : "__CLOUDLINK_LEGACY_STATE__"
property activeState : ""

on run
    set allowQuit to false
    set busy to false
    set relayRunning to false
    set resourcesDir to POSIX path of (path to resource "relay.py")
    set resourcesDir to do shell script "/usr/bin/dirname " & quoted form of resourcesDir
    if bundledPython then set pythonPath to resourcesDir & "/python/bin/python3"
    set preferredState to (POSIX path of (path to application support folder from user domain)) & "云连接助手/relay-state"
    set activeState to preferredState
    my makeWindow()
    my makeStartupMenu()
    my refreshState()
    mainWindow's makeKeyAndOrderFront:me
    current application's NSApp's activateIgnoringOtherApps:true
end run

on makeStartupMenu()
    set applicationMenu to (current application's NSApp's mainMenu()'s itemAtIndex:0)'s submenu()
    applicationMenu's setAutoenablesItems:false
    set enableStartupItem to current application's NSMenuItem's alloc()'s initWithTitle:"开启云中继开机启动…" action:"enableStartup:" keyEquivalent:""
    enableStartupItem's setTarget:me
    applicationMenu's insertItem:enableStartupItem atIndex:2
    set disableStartupItem to current application's NSMenuItem's alloc()'s initWithTitle:"关闭云中继开机启动…" action:"disableStartup:" keyEquivalent:""
    disableStartupItem's setTarget:me
    applicationMenu's insertItem:disableStartupItem atIndex:3
    applicationMenu's insertItem:(current application's NSMenuItem's separatorItem()) atIndex:4
end makeStartupMenu

on changeStartup(actionName)
    if busy then return
    my beginAction("正在设置开机启动…")
    try
        set operatorUID to do shell script "/usr/bin/id -u"
        set operatorGID to do shell script "/usr/bin/id -g"
        set commandText to "/usr/bin/env SUDO_UID=" & quoted form of operatorUID & " SUDO_GID=" & quoted form of operatorGID & " " & quoted form of pythonPath & " -I -B " & quoted form of (resourcesDir & "/autostart.py") & " " & quoted form of actionName
        if actionName is "install" then
            set applicationPath to current application's NSBundle's mainBundle()'s bundlePath() as text
            set commandText to commandText & " --app " & quoted form of applicationPath
        end if
        do shell script commandText with administrator privileges
        if actionName is "install" then
            my showReport("云中继开机启动已开启", "中继会等待系统代理后启动。已启用本机 HTTPS/SSE 时，会在两条中继就绪后打开 Codex。仍可手动停止。")
        else
            my showReport("云中继开机启动已关闭", "云中继已停止并恢复网络设置。本机 HTTPS/SSE 独立控制，可在窗口中恢复或停止。")
        end if
    on error errorText number errorNumber
        if errorNumber is not -128 then my showReport("开机启动设置未完成", errorText)
    end try
    set busy to false
    my refreshState()
end changeStartup

on enableStartup:sender
    my changeStartup("install")
end enableStartup:

on disableStartup:sender
    my changeStartup("disable")
end disableStartup:

on makeLabel(theText, fontSize, x, y, w, h, boldText)
    set field to current application's NSTextField's labelWithString:theText
    field's setFrame:{{x, y}, {w, h}}
    if boldText then
        field's setFont:(current application's NSFont's boldSystemFontOfSize:fontSize)
    else
        field's setFont:(current application's NSFont's systemFontOfSize:fontSize)
    end if
    field's setMaximumNumberOfLines:0
    field's setLineBreakMode:(current application's NSLineBreakByWordWrapping)
    mainWindow's contentView()'s addSubview:field
    return field
end makeLabel

on makeButton(theText, actionName, x, y, w)
    set button to current application's NSButton's alloc()'s initWithFrame:{{x, y}, {w, 34}}
    button's setTitle:theText
    button's setBezelStyle:(current application's NSBezelStyleRounded)
    button's setTarget:me
    button's setAction:actionName
    mainWindow's contentView()'s addSubview:button
    return button
end makeButton

on makeWindow()
    set mainWindow to current application's NSWindow's alloc()'s initWithContentRect:{{0, 0}, {600, 740}} styleMask:7 backing:2 defer:false
    mainWindow's setTitle:"云连接助手"
    mainWindow's setReleasedWhenClosed:false
    mainWindow's |center|()
    my makeLabel("云连接助手", 25, 28, 680, 540, 34, true)
    set subtitle to my makeLabel("Codex 云连接、本机 HTTPS/SSE 与 Antigravity 授权入口", 13, 28, 654, 540, 24, false)
    subtitle's setTextColor:(current application's NSColor's secondaryLabelColor())
    my makeLabel("Codex 云中继", 17, 28, 608, 350, 28, true)
    set statusLabel to my makeLabel("检查中…", 15, 430, 608, 140, 28, true)
    set detailLabel to my makeLabel("", 13, 28, 520, 544, 80, false)
    set startButton to my makeButton("启动", "startRelay:", 24, 473, 112)
    set stopButton to my makeButton("停止", "stopRelay:", 148, 473, 112)
    my makeButton("刷新状态", "refreshClicked:", 272, 473, 112)
    set restoreButton to my makeButton("恢复设置", "restoreRelay:", 396, 473, 176)
    set separator to current application's NSBox's alloc()'s initWithFrame:{{28, 456}, {544, 1}}
    separator's setBoxType:(current application's NSBoxSeparator)
    mainWindow's contentView()'s addSubview:separator
    my makeLabel("本机聊天 HTTPS/SSE", 17, 28, 418, 350, 28, true)
    set modelStatusLabel to my makeLabel("检查中…", 15, 430, 418, 140, 28, true)
    set modelDetailLabel to my makeLabel("", 12, 28, 345, 544, 68, false)
    my makeButton("启用本机优化", "enableModel:", 24, 300, 172)
    my makeButton("恢复原连接", "restoreModel:", 208, 300, 172)
    my makeButton("停止本机中继…", "stopModel:", 392, 300, 180)
    set modelSeparator to current application's NSBox's alloc()'s initWithFrame:{{28, 276}, {544, 1}}
    modelSeparator's setBoxType:(current application's NSBoxSeparator)
    mainWindow's contentView()'s addSubview:modelSeparator
    my makeLabel("Antigravity 登录与授权", 17, 28, 228, 540, 28, true)
    set antiLabel to my makeLabel("正在检查安装状态…", 12, 28, 203, 544, 23, false)
    antiLabel's setTextColor:(current application's NSColor's secondaryLabelColor())
    my makeButton("代理启动 / 登录入口", "openAntigravity:", 24, 163, 212)
    my makeButton("检查授权网络", "checkAntigravity:", 248, 163, 176)
    set reportLabel to my makeLabel("检查结果将显示在这里。Google 登录和授权在官方窗口完成。", 12, 28, 47, 544, 108, false)
    reportLabel's setTextColor:(current application's NSColor's secondaryLabelColor())
    set quitPanel to current application's NSView's alloc()'s initWithFrame:{{20, 35}, {560, 122}}
    set quitTitle to current application's NSTextField's labelWithString:"退出助手时如何处理中继？"
    quitTitle's setFrame:{{8, 78}, {540, 28}}
    quitPanel's addSubview:quitTitle
    repeat with buttonInfo in {{"取消", "cancelQuit:", 4}, {"保留后台并退出", "keepAndQuit:", 124}, {"停止全部并退出", "stopAndQuit:", 336}}
        set quitButton to current application's NSButton's alloc()'s initWithFrame:{{item 3 of buttonInfo, 25}, {200, 34}}
        if item 1 of buttonInfo is "取消" then quitButton's setFrame:{{4, 25}, {108, 34}}
        quitButton's setTitle:(item 1 of buttonInfo)
        quitButton's setBezelStyle:(current application's NSBezelStyleRounded)
        quitButton's setTarget:me
        quitButton's setAction:(item 2 of buttonInfo)
        quitPanel's addSubview:quitButton
    end repeat
    mainWindow's contentView()'s addSubview:quitPanel
    quitPanel's setHidden:true
    set footer to my makeLabel("关闭窗口保留两条中继；选择停止前请先结束聊天。", 11, 28, 14, 544, 20, false)
    footer's setTextColor:(current application's NSColor's secondaryLabelColor())
end makeWindow

on showReport(titleText, messageText)
    quitPanel's setHidden:true
    reportLabel's setHidden:false
    reportLabel's setStringValue:(titleText & linefeed & messageText)
end showReport

on relayCommand(actionName, stateDir)
    return quoted form of pythonPath & " -B " & quoted form of (resourcesDir & "/relay.py") & " " & actionName & " --state-dir " & quoted form of stateDir
end relayCommand

on modelCommand(actionName)
    set applicationPath to current application's NSBundle's mainBundle()'s bundlePath() as text
    return quoted form of pythonPath & " -I -B " & quoted form of (resourcesDir & "/model_service.py") & " " & quoted form of actionName & " --app " & quoted form of applicationPath
end modelCommand

on refreshModelState()
    try
        set report to do shell script my modelCommand("ui-status")
        set modelRunning to paragraph 1 of report is "running"
        modelStatusLabel's setStringValue:(paragraph 2 of report)
        set oldDelimiters to AppleScript's text item delimiters
        set AppleScript's text item delimiters to linefeed
        set modelText to (paragraphs 3 thru -1 of report) as text
        set AppleScript's text item delimiters to oldDelimiters
        modelDetailLabel's setStringValue:modelText
    on error errorText
        modelStatusLabel's setStringValue:"状态不可用"
        modelDetailLabel's setStringValue:errorText
    end try
end refreshModelState

on changeModel(actionName)
    if busy then return
    my beginAction("正在更新本机连接…")
    try
        set resultText to do shell script my modelCommand(actionName)
        my showReport("本机连接", resultText)
    on error errorText
        my showReport("本机连接未完成", errorText)
    end try
    set busy to false
    my refreshState()
end changeModel

on enableModel:sender
    my changeModel("enable")
end enableModel:

on restoreModel:sender
    my changeModel("restore")
end restoreModel:

on stopModel:sender
    set alert to current application's NSAlert's alloc()'s init()
    alert's setMessageText:"恢复配置并停止本机中继？"
    alert's setInformativeText:"请先结束正在运行的聊天。仍连接本机中继的会话会被中断；重开 Codex 后使用原连接方式。"
    alert's addButtonWithTitle:"取消"
    alert's addButtonWithTitle:"恢复并停止"
    if (alert's runModal() as integer) is 1001 then my changeModel("stop")
end stopModel:

on refreshState()
    if busy then return
    try
        set activeState to preferredState
        set report to do shell script my relayCommand("ui-status", preferredState)
        if legacyState is not "" and paragraph 1 of report is not "running" then
            set legacyReport to do shell script my relayCommand("ui-status", legacyState)
            if paragraph 1 of legacyReport is "running" or paragraph 1 of legacyReport is "needs_restore" then
                set report to legacyReport
                set activeState to legacyState
            end if
        end if
        set phase to paragraph 1 of report
        set relayRunning to phase is "running"
        statusLabel's setStringValue:(paragraph 2 of report)
        set oldDelimiters to AppleScript's text item delimiters
        set AppleScript's text item delimiters to linefeed
        set detailText to (paragraphs 3 thru -1 of report) as text
        set AppleScript's text item delimiters to oldDelimiters
        detailLabel's setStringValue:detailText
        if relayRunning then
            statusLabel's setTextColor:(current application's NSColor's systemGreenColor())
        else if phase is "needs_restore" then
            statusLabel's setTextColor:(current application's NSColor's systemOrangeColor())
        else
            statusLabel's setTextColor:(current application's NSColor's secondaryLabelColor())
        end if
        startButton's setEnabled:(not relayRunning)
        stopButton's setEnabled:relayRunning
        restoreButton's setEnabled:(not relayRunning)
        set antiInfo to do shell script quoted form of pythonPath & " -B " & quoted form of (resourcesDir & "/antigravity_helper.py") & " info"
        antiLabel's setStringValue:antiInfo
        set startupInstalled to (do shell script "if /bin/test -f /Library/LaunchDaemons/local.hive.cloudlinkassistant.autorelay.plist; then /usr/bin/printf yes; else /usr/bin/printf no; fi") is "yes"
        enableStartupItem's setEnabled:(not startupInstalled)
        disableStartupItem's setEnabled:startupInstalled
    on error errorText
        statusLabel's setStringValue:"状态不可用"
        detailLabel's setStringValue:errorText
    end try
    my refreshModelState()
end refreshState

on beginAction(messageText)
    set busy to true
    startButton's setEnabled:false
    stopButton's setEnabled:false
    restoreButton's setEnabled:false
    statusLabel's setStringValue:messageText
    current application's NSRunLoop's currentRunLoop()'s runUntilDate:(current application's NSDate's dateWithTimeIntervalSinceNow:0.05)
end beginAction

on startRelay:sender
    my refreshState()
    if relayRunning then return
    my beginAction("正在启动…")
    try
        do shell script "/bin/mkdir -p " & quoted form of ((POSIX path of (path to application support folder from user domain)) & "云连接助手")
        set operatorUID to do shell script "/usr/bin/id -u"
        set operatorGID to do shell script "/usr/bin/id -g"
        set managedStartup to (do shell script "if /bin/test -f /Library/LaunchDaemons/local.hive.cloudlinkassistant.autorelay.plist; then /usr/bin/printf yes; else /usr/bin/printf no; fi") is "yes"
        if managedStartup then
            set commandText to "/bin/launchctl kickstart system/local.hive.cloudlinkassistant.autorelay"
        else
            set commandText to "/usr/bin/env SUDO_UID=" & quoted form of operatorUID & " SUDO_GID=" & quoted form of operatorGID & " " & my relayCommand("restart", preferredState) & " --proxy-bypass-service 'Wi-Fi'"
        end if
        do shell script commandText with administrator privileges
    on error errorText number errorNumber
        if errorNumber is not -128 then my showReport("启动未完成", errorText)
    end try
    set busy to false
    my refreshState()
end startRelay:

on stopRelay:sender
    my beginAction("正在停止…")
    try
        do shell script my relayCommand("stop", activeState)
    on error errorText
        my showReport("停止未完成", errorText)
    end try
    set busy to false
    my refreshState()
end stopRelay:

on restoreRelay:sender
    my beginAction("正在恢复…")
    try
        do shell script my relayCommand("restore", activeState) with administrator privileges
    on error errorText number errorNumber
        if errorNumber is not -128 then my showReport("恢复未完成", errorText)
    end try
    set busy to false
    my refreshState()
end restoreRelay:

on refreshClicked:sender
    my refreshState()
end refreshClicked:

on openAntigravity:sender
    try
        set messageText to do shell script quoted form of pythonPath & " -B " & quoted form of (resourcesDir & "/antigravity_helper.py") & " open"
        my showReport("Antigravity 登录与授权", messageText)
    on error errorText
        my showReport("无法打开 Antigravity", errorText)
    end try
end openAntigravity:

on checkAntigravity:sender
    my showReport("授权网络检查", "正在通过系统代理检查，请稍候…")
    my beginAction("检查网络…")
    try
        set messageText to do shell script quoted form of pythonPath & " -B " & quoted form of (resourcesDir & "/antigravity_helper.py") & " check"
        my showReport("授权网络检查", messageText)
    on error errorText
        my showReport("检查未完成", errorText)
    end try
    set busy to false
    my refreshState()
end checkAntigravity:

on idle
    if mainWindow is not missing value and not busy then
        if mainWindow's isVisible() as boolean then my refreshState()
    end if
    return 5
end idle

on reopen
    if mainWindow is not missing value then
        my refreshState()
        mainWindow's makeKeyAndOrderFront:me
        current application's NSApp's activateIgnoringOtherApps:true
    end if
end reopen

on quit
    if allowQuit then continue quit
    if busy then return
    my refreshState()
    if relayRunning or modelRunning then
        reportLabel's setHidden:true
        quitPanel's setHidden:false
        mainWindow's makeKeyAndOrderFront:me
        current application's NSApp's activateIgnoringOtherApps:true
        return
    end if
    continue quit
end quit

on cancelQuit:sender
    quitPanel's setHidden:true
    reportLabel's setHidden:false
end cancelQuit:

on keepAndQuit:sender
    set allowQuit to true
    current application's NSApp's terminate:me
end keepAndQuit:

on stopAndQuit:sender
    my changeModel("stop")
    my stopRelay:sender
    if not relayRunning and not modelRunning then my keepAndQuit:sender
end stopAndQuit:
