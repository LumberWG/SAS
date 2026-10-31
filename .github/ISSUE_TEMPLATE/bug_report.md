---
name: Bug 报告
about: 功能异常、报错、数据不对
labels: [bug]
---

## 现象

<!-- 你期望发生什么，实际发生了什么 -->

## 复现步骤

1.
2.
3.

## 环境

- 系统：Windows / Linux，版本
- Python：`python -V` 输出
- 依赖安装方式：`requirements.txt` / `requirements.lock.txt`
- 运行方式：`uvicorn app.main:app` / nssm 服务 / `scripts\dev\start_dev.py`
- 是否设置了 `API_TOKEN`（是 / 否，**不要贴值**）

## 关键日志

```
# backend\logs\backend.log 里出错前后的 30~50 行（先脱敏：账户号、令牌、公网地址）
```

## 数据状态（选股/回测/模拟盘类问题必填）

- K 线缓存是否已构建（`data\klines` 下文件数）
- 是否首跑（首跑缓存不全时结果偏少是正常现象）
- 相关开关当前值：`GET /api/settings` 里对应键的 `value` 与 `source`
  （**`source` 必须是 `app_state`/`env` 之一才能确认真的生效**）

## 已排除

<!-- 例如：不是 14 因子喂了没有指标的 frame、不是弱市闸门拦截（regime.paused_count=0） -->
