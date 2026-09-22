#!/bin/bash
# Double-click entry for macOS: Finder opens .command files in Terminal.
# It simply runs the self-healing bootstrapper, then keeps the window open
# if anything failed so the message stays visible.

cd "$(dirname "$0")" || exit 1

./bootstrap.sh
STATUS=$?

if [ "$STATUS" -ne 0 ]; then
    echo ""
    echo "启动失败（退出码 $STATUS）。错误详情也写入了 startup-error.log。"
    echo "按回车键关闭本窗口。"
    read -r _
fi
