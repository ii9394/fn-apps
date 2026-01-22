#!/bin/bash

# 风扇调控应用打包脚本
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

# 1. 打包 app 目录
echo ""
echo "[1/3] 打包 app 目录..."
cd app
tar -zcvf ../app.tgz ./
cd ..

# 2. 计算 MD5
echo ""
echo "[2/3] 计算 MD5..."
md5sum app.tgz

# 3. 打包为 fpk
echo ""
echo "[3/3] 生成 fpk 包..."
OUTPUT_FILE="${APP_NAME}-${VERSION}.fpk"
tar -zcvf "$OUTPUT_FILE" app.tgz cmd config ICON_256.PNG ICON.PNG manifest wizard

# 清理临时文件
rm -f app.tgz

mv "$OUTPUT_FILE" ../

# 完成
echo ""
echo "================================================"
echo "  打包完成: ../${OUTPUT_FILE}"
echo "================================================"
ls -lh "../$OUTPUT_FILE"
md5sum "../$OUTPUT_FILE"
