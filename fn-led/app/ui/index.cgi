#!/bin/bash

# ============================================================================
# File Name       : index.cgi
# Description     : CGI script for serving static files and proxying API.
# ============================================================================

BASE_PATH="/var/apps/fn-led/target/www"

URI_NO_QUERY="${REQUEST_URI%%\?*}"
REL_PATH="/"

case "$URI_NO_QUERY" in
  *index.cgi*)
    REL_PATH="${URI_NO_QUERY#*index.cgi}"
    ;;
esac

if [ -z "$REL_PATH" ] || [ "$REL_PATH" = "/" ]; then
  REL_PATH="/index.html"
fi

# API 代理
if [[ $REL_PATH == /api* ]]; then
  BACKEND_UNIX_SOCKET="${BACKEND_UNIX_SOCKET:-/usr/local/apps/@appdata/fn-led/led.sock}"
  BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
  BACKEND_PORT="${BACKEND_PORT:-28258}"

  if [ -n "$CONTENT_LENGTH" ] && [ "$CONTENT_LENGTH" -gt 0 ] 2>/dev/null; then
    BODY_TMP=$(mktemp)
    dd bs=1 count="$CONTENT_LENGTH" of="$BODY_TMP" 2>/dev/null || cat >"$BODY_TMP"
  else
    BODY_TMP=$(mktemp)
    : >"$BODY_TMP"
  fi

  HDR_TMP=$(mktemp)
  OUT_BODY=$(mktemp)

  curl_args=(-sS -D "$HDR_TMP" -o "$OUT_BODY" -X "$REQUEST_METHOD")
  for hdr in CONTENT_TYPE HTTP_AUTHORIZATION REDIRECT_HTTP_AUTHORIZATION HTTP_ACCEPT HTTP_COOKIE HTTP_USER_AGENT HTTP_REFERER; do
    val="${!hdr}"
    case "$hdr" in
      CONTENT_TYPE) [ -n "$val" ] && curl_args+=(-H "Content-Type: $val") ;;
      HTTP_AUTHORIZATION | REDIRECT_HTTP_AUTHORIZATION) [ -n "$val" ] && curl_args+=(-H "Authorization: $val") ;;
      HTTP_ACCEPT) [ -n "$val" ] && curl_args+=(-H "Accept: $val") ;;
      HTTP_COOKIE) [ -n "$val" ] && curl_args+=(-H "Cookie: $val") ;;
      HTTP_USER_AGENT) [ -n "$val" ] && curl_args+=(-H "User-Agent: $val") ;;
      HTTP_REFERER) [ -n "$val" ] && curl_args+=(-H "Referer: $val") ;;
    esac
  done

  if [ -n "$REMOTE_ADDR" ]; then
    curl_args+=(-H "X-Forwarded-For: $REMOTE_ADDR")
  fi

  case "$REQUEST_METHOD" in
    POST | PUT | PATCH)
      curl_args+=(--data-binary "@$BODY_TMP")
      ;;
  esac

  if [ -n "$BACKEND_UNIX_SOCKET" ] && [ -S "$BACKEND_UNIX_SOCKET" ]; then
    BACKEND_URL="http://localhost${REL_PATH}"
    curl --unix-socket "$BACKEND_UNIX_SOCKET" "${curl_args[@]}" "$BACKEND_URL"
    CURL_EXIT=$?
  else
    BACKEND_URL="http://${BACKEND_HOST}:${BACKEND_PORT}${REL_PATH}"
    curl "${curl_args[@]}" "$BACKEND_URL"
    CURL_EXIT=$?
  fi

  status_line=$(head -n1 "$HDR_TMP" 2>/dev/null || echo "HTTP/1.1 502 Bad Gateway")
  status_code=$(echo "$status_line" | awk '{print $2}' 2>/dev/null || echo "502")
  resp_ct=$(grep -i '^Content-Type:' "$HDR_TMP" | head -n1 | sed -e 's/^[Cc]ontent-[Tt]ype:[[:space:]]*//')
  if [ -z "$resp_ct" ]; then
    resp_ct="application/octet-stream"
  fi

  grep -i -E '^(Set-Cookie:|Cache-Control:|Expires:|Access-Control-Allow-|Content-Disposition:)' "$HDR_TMP" | while read -r h; do
    echo "$h"
  done

  if [ "$CURL_EXIT" -ne 0 ]; then
    echo "Status: 502 Bad Gateway"
    echo "Content-Type: text/plain; charset=utf-8"
    echo ""
    echo "502 Bad Gateway: Backend unavailable"
    rm -f "$HDR_TMP" "$BODY_TMP" "$OUT_BODY"
    exit 0
  fi

  echo "Status: $status_code"
  echo "Content-Type: $resp_ct"
  echo ""
  cat "$OUT_BODY"

  rm -f "$HDR_TMP" "$BODY_TMP" "$OUT_BODY"
  exit 0
fi

# 静态文件
TARGET_FILE="${BASE_PATH}${REL_PATH}"

if echo "$TARGET_FILE" | grep -q '\.\.'; then
  echo "Status: 400 Bad Request"
  echo "Content-Type: text/plain; charset=utf-8"
  echo ""
  echo "Bad Request: Path traversal detected"
  exit 0
fi

if [ ! -f "$TARGET_FILE" ]; then
  echo "Status: 404 Not Found"
  echo "Content-Type: text/plain; charset=utf-8"
  echo ""
  echo "404 Not Found: ${REL_PATH}"
  exit 0
fi

ext="${TARGET_FILE##*.}"
ext_lc="$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')"

case "$ext_lc" in
  html | htm) mime="text/html; charset=utf-8" ;;
  css) mime="text/css; charset=utf-8" ;;
  js) mime="application/javascript; charset=utf-8" ;;
  jpg | jpeg) mime="image/jpeg" ;;
  png) mime="image/png" ;;
  gif) mime="image/gif" ;;
  svg) mime="image/svg+xml" ;;
  json) mime="application/json; charset=utf-8" ;;
  *) mime="application/octet-stream" ;;
esac

size=$(stat -c %s "$TARGET_FILE" 2>/dev/null || echo 0)

printf 'Content-Type: %s\r\n' "$mime"
printf 'Content-Length: %s\r\n' "$size"
printf '\r\n'

if [ "${REQUEST_METHOD:-GET}" = "HEAD" ]; then
  exit 0
fi

cat "$TARGET_FILE"
