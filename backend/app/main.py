"""FastAPI 应用入口：CORS、路由注册、生命周期管理。"""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    advisory, analysis, backtest, market, monitor, paper, scope, screening,
    settings as settings_api, strategies, watchlist,
)
from app.config import BASE_DIR, get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("sas")

#: 主服务日志文件（`<repo>/backend/logs/backend.log`，10MB×5 轮转）
LOG_FILE = BASE_DIR / "backend" / "logs" / "backend.log"


def _setup_file_logging() -> Path | None:
    """把日志同时落一份文件，返回日志路径（失败返回 None，不影响启动）。

    为什么必须：服务由 `restart_backend.bat` / vbs 以**隐藏窗口**启动，stderr 无人接收，
    于是「评分 xx<25，仅记录不下单」「live ML 无信号，回退 14 因子」这类**决定性判定
    全部只活在内存里**。2026-10-31 复盘「为什么零成交」时，只能靠重跑脚本反推，
    就是因为这里缺一环。

    只在 root 上挂一次（幂等）：uvicorn `--reload` 会重复执行本模块，挂多了会重复写行。
    """
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        for h in logging.getLogger().handlers:
            if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == LOG_FILE:
                return LOG_FILE
        handler = RotatingFileHandler(
            LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)
        # uvicorn 的三个 logger 自带 handler 且 `propagate=False`，不单独挂就收不到
        # 启动信息与访问日志——而那正是「服务到底起没起、谁在打接口」的唯一证据。
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            lg = logging.getLogger(name)
            if any(isinstance(h, RotatingFileHandler) for h in lg.handlers):
                continue
            lg.addHandler(handler)
            lg.setLevel(logging.INFO)
            # 必须断开向 root 的传播：`uvicorn.error` 在 uvicorn 自己 dictConfig 之前
            # `propagate=True`，同一份 handler 又挂在 root 上，一条记录会被写 3 次
            # （实测：uvicorn.error → uvicorn → root 各写一遍）。
            lg.propagate = False
        return LOG_FILE
    except Exception as exc:  # noqa: BLE001 - 日志失败绝不能拖垮启动
        logging.getLogger("sas").warning("文件日志初始化失败（仅控制台输出）: %s", exc)
        return None


_setup_file_logging()

#: 前端生产构建产物目录（cpolar 单端口部署时由后端托管）
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"

#: 不校验访问令牌的接口：健康检查（部署脚本/监控探活）不含业务数据
TOKEN_EXEMPT_PATHS = frozenset({"/api/health"})


async def _token_guard(request: Request, call_next):
    """访问令牌守卫：`.env` 里配了 `API_TOKEN` 才启用。

    为什么需要：站点经 cpolar 隧道公网可达，而设置接口能读配置（含 LLM 模型名）、
    改交易开关、还原全部参数。没有令牌时默认不校验，保证本机开发零配置。
    令牌只从 `.env` 读，且列在 `READONLY_KEYS` 里，不会被 `GET /api/settings` 回显。
    """
    token = (get_settings().api_token or "").strip()
    path = request.url.path
    if (
        not token
        or request.method == "OPTIONS"          # CORS 预检不带自定义头
        or not path.startswith("/api")
        or path in TOKEN_EXEMPT_PATHS
    ):
        return await call_next(request)

    supplied = request.headers.get("x-api-token", "").strip()
    if not supplied:
        auth = request.headers.get("authorization", "")
        if auth[:7].lower() == "bearer ":
            supplied = auth[7:].strip()
    # 定长比较，避免按字节泄露时序
    if not hmac.compare_digest(supplied, token):
        return JSONResponse(
            status_code=401,
            content={"detail": "需要访问令牌：请提供 X-API-Token 请求头（或在页面上输入）"},
        )
    return await call_next(request)


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

    # 重放「系统设置」页保存的运行时覆盖（app_state → Settings 单例）
    try:
        from app.param_specs import apply_persisted_overrides

        apply_persisted_overrides()
    except Exception as exc:  # noqa: BLE001
        logger.warning("运行时配置覆盖重放失败: %s", exc)

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

    # 访问令牌守卫（API_TOKEN 为空时直接放行，本机开发零配置）
    app.middleware("http")(_token_guard)

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
    app.include_router(settings_api.router, prefix="/api")

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

        # index.html 必须每次回源校验：重新构建后 assets 的 hash 全变，
        # 浏览器若抱着旧入口不放，会去请求已经被删掉的 chunk（表现为「页面还是老版本」）
        html_headers = {"Cache-Control": "no-cache, must-revalidate"}

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(FRONTEND_DIST / "index.html", headers=html_headers)

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str) -> FileResponse:
            """SPA history 模式回退：非 API 路径统一返回 index.html。

            注册在最后，/api/* 已注册路由优先匹配，不会拦截 API。
            """
            return FileResponse(FRONTEND_DIST / "index.html", headers=html_headers)

        logger.info("前端静态托管已启用: %s", FRONTEND_DIST)

    return app


app = create_app()
