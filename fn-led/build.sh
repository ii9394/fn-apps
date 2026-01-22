#!/bin/bash

# LED控制应用打包脚本
# 使用方法: ./build.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 读取应用名称和版本
APP_NAME=$(grep "^appname" manifest | awk -F'=' '{print $2}' | tr -d ' ')
VERSION=$(grep "^version" manifest | awk -F'=' '{print $2}' | tr -d ' ')

echo "================================================"
echo "  打包 ${APP_NAME} v${VERSION}"
echo "================================================"

# 清理旧文件
rm -f app.tgz "${APP_NAME}.fpk" "${APP_NAME}-${VERSION}.fpk"

# 0. 转换换行符为 Unix 格式 (LF)
echo ""
echo "[0/4] 转换换行符..."
find app cmd wizard -type f \( -name "*.sh" -o -name "*.cgi" -o -name "main" -o -name "install" -o -name "uninstall" -o -name "config" -o -name "*_init" -o -name "*_callback" \) -exec sed -i 's/\r$//' {} \;
sed -i 's/\r$//' manifest

# 1. 打包 app 目录
echo ""
echo "[1/4] 打包 app 目录..."
cd app
tar -zcvf ../app.tgz ./
cd ..

# 2. 计算 MD5
echo ""
echo "[2/4] 计算 MD5..."
md5sum app.tgz

# 3. 打包为 fpk
echo ""
echo "[3/4] 生成 fpk 包..."
OUTPUT_FILE="${APP_NAME}-${VERSION}.fpk"
tar -zcvf "$OUTPUT_FILE" app.tgz cmd config ICON_256.PNG ICON.PNG manifest wizard

# 清理临时文件
rm -f app.tgz

# 4. 移动到上层目录
echo ""
echo "[4/4] 移动到上层目录..."
mv "$OUTPUT_FILE" ../

# 完成
echo ""
echo "================================================"
echo "  打包完成: ../${OUTPUT_FILE}"
echo "================================================"
ls -lh "../$OUTPUT_FILE"
md5sum "../$OUTPUT_FILE"
