"""FastAPI 应用入口：CORS、路由注册、生命周期管理。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    advisory, analysis, backtest, market, monitor, paper, scope, screening,
    strategies, watchlist,
)
from app.config import BASE_DIR, get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("sas")

#: 前端生产构建产物目录（cpolar 单端口部署时由后端托管）
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：后台预热股票池 + 启动调度器。

    预热放后台线程执行，不阻塞应用就绪——否则开机自启场景下
    服务显示 Running 但端口未监听（东财/降级链可能要几十秒），
    health 检查失败、公网访问 502。
    """
    logger.info("SAS 后端启动（股票池后台预热中）")

    async def warmup() -> None:
        try:
            # 使用服务层 get_stock_pool（主源不可用时自动降级为持久化快照代码表）
            from app.data.service import get_stock_pool

            pool = await asyncio.to_thread(get_stock_pool)
            logger.info("股票池预热完成，共 %d 只股票", len(pool))
        except Exception as exc:  # noqa: BLE001
            logger.warning("股票池预热失败（将在首次请求时重试）: %s", exc)

    warmup_task = asyncio.create_task(warmup())

    # 加载自定义策略（用户此前保存的参数化策略）
    try:
        from app.strategies.custom import load_custom_strategies

        load_custom_strategies()
    except Exception as exc:  # noqa: BLE001
        logger.warning("自定义策略加载失败: %s", exc)

    # 启动定时任务（快照刷新 / 异动检测 / 收盘自动选股）
    from app.monitor.scheduler import scheduler

    scheduler.start()
    yield
    warmup_task.cancel()
    scheduler.shutdown()
    logger.info("SAS 后端关闭")


def create_app() -> FastAPI:
    """应用工厂。"""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        description="A股智能分析系统 · 仅供学习研究，不构成投资建议",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS：允许 Vite 开发服务器访问
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 路由注册（各模块统一挂 /api 前缀）
    app.include_router(market.router, prefix="/api")
    app.include_router(screening.router, prefix="/api")
    app.include_router(backtest.router, prefix="/api")
    app.include_router(analysis.router, prefix="/api")
    app.include_router(watchlist.router, prefix="/api")
    app.include_router(monitor.router, prefix="/api")
    app.include_router(strategies.router, prefix="/api")
    app.include_router(advisory.router, prefix="/api")
    app.include_router(paper.router, prefix="/api")
    app.include_router(scope.router, prefix="/api")

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "disclaimer": settings.disclaimer}

    # ------------------------------------------------------------------
    # 前端静态托管（生产部署：cpolar 只需穿透 8000 单端口）
    # dist 不存在时（纯后端开发模式）跳过，不影响 /api 与 /docs
    # ------------------------------------------------------------------
    if FRONTEND_DIST.is_dir():
        # 静态资源（JS/CSS/图片）单独挂载，长缓存友好
        assets_dir = FRONTEND_DIST / "assets"
        if assets_dir.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=assets_dir),
                name="assets",
            )

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(FRONTEND_DIST / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str) -> FileResponse:
            """SPA history 模式回退：非 API 路径统一返回 index.html。

            注册在最后，/api/* 已注册路由优先匹配，不会拦截 API。
            """
            return FileResponse(FRONTEND_DIST / "index.html")

        logger.info("前端静态托管已启用: %s", FRONTEND_DIST)

    return app


app = create_app()
