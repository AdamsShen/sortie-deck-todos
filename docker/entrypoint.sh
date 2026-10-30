#!/usr/bin/env sh
# 容器入口：把第一个参数当作要跑的角色，其余参数透传给 sortie。
set -e

case "${1:-api}" in
  api)
    shift || true
    exec sortie api --host "${TDT_HOST:-0.0.0.0}" --port "${TDT_PORT:-8787}" "$@"
    ;;
  worker)
    shift || true
    exec sortie worker "$@"
    ;;
  doctor)
    shift || true
    exec sortie doctor "$@"
    ;;
  shell)
    exec /bin/sh
    ;;
  *)
    exec "$@"
    ;;
esac
