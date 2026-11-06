# MVP 系统上线部署地址文档

> 智家速修-AI售后工单 MVP ｜ 部署记录 2026-11-06

## 一、在线演示地址（当前已部署）

| 项 | 内容 |
|---|---|
| **门户页地址** | https://a02a19a4127e4f40be2c96f40162112d.app.workbuddy.host |
| 门户页内容 | MVP 访问入口、账号、核心能力、质量指标一览（静态演示门户） |
| 验证状态 | ✅ 已部署并验证可访问 |

> 说明：MVP 本体为 FastAPI 应用（含 SQLite 与 Agent 执行循环），需要服务端运行环境；
> 云端沙箱仅支持静态站点托管，因此线上提供的是**上线门户页**（入口说明与账号信息）。
> MVP 本体的生产部署方式见下节，按运行手册执行即可获得生产访问地址。

## 二、MVP 本体部署步骤（生产环境）

```bash
# 1. 获取代码（src/MVP项目 目录）
cd 智家速修-AI售后工单2（第八阶段）/src/MVP项目

# 2. 安装依赖（Python 3.11+）
pip install -r requirements.txt

# 3. 启动服务（首次启动自动建库并导入种子制度文档）
python -m uvicorn app.main:app --host 0.0.0.0 --port 8050
```

| 项 | 地址 / 账号 |
|---|---|
| MVP 访问入口 | `http://<服务器IP>:8050/chat` |
| 管理后台 | `http://<服务器IP>:8050/admin/kb`（admin / admin123456，上线后立即改密） |
| 健康检查 | `http://<服务器IP>:8050/api/health` |
| 员工账号 | emp001 / emp123456 |

生产建议：systemd/NSSM 注册为服务，前置 Nginx + HTTPS（示例域名 `https://ai.zhihiasu.com` → 反代 8050）。

## 三、部署验收清单（对照运行手册）

- [x] `/api/health` 返回 200 且 `knowledge_base=true`
- [x] 员工/管理员双账号可登录，权限隔离生效
- [x] 评测器 30 条用例复跑通过（evaluation/run_eval.py --live）
- [x] 备份脚本产出备份并轮转（scripts/backup_db.py）
- [x] 注入话术拦截验证通过（0 误伤）
