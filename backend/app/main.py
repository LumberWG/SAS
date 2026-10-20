"""FastAPI 应用入口：CORS、路由注册、生命周期管理。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import analysis, backtest, market, monitor, screening, watchlist
from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("sas")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：预热股票池 + 启动调度器。"""
    logger.info("SAS 后端启动，预热股票池...")
    try:
        from app.data.akshare_source import get_data_source

        pool = get_data_source().get_stock_pool()
        logger.info("股票池预热完成，共 %d 只股票", len(pool))
    except Exception as exc:  # noqa: BLE001
        # 预热失败不阻塞启动（可能是网络问题），首次请求时会重试
        logger.warning("股票池预热失败（将在首次请求时重试）: %s", exc)

    # 启动定时任务（快照刷新 / 异动检测 / 收盘自动选股）
    from app.monitor.scheduler import scheduler

    scheduler.start()
    yield
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

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "disclaimer": settings.disclaimer}

    return app


app = create_app()
