"""
main.py - FastAPI 后端服务入口

RS-Platform 遥感影像处理系统的后端 API 服务。

功能：
- 提供影像数据的 REST API
- 支持影像列表、详情、切片、概览图查询
- 支持触发新的影像下载任务
- 集成 ZARR 数据服务，高效读取遥感影像

启动方式：
    # 开发模式
    uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

    # 生产模式
    uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 4

API 文档：
    启动后访问 http://localhost:8000/docs 查看 Swagger UI
"""

import sys
from pathlib import Path

# 将项目根目录加入 Python 路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.routers import imagery

# 创建 FastAPI 应用实例
app = FastAPI(
    title="RS-Platform API",
    description="遥感影像处理系统 API - 提供 Sentinel-2 影像的下载、处理和展示服务",
    version="1.0.0",
    docs_url="/docs",      # Swagger UI 地址
    redoc_url="/redoc",    # ReDoc 地址
)

# 配置 CORS 中间件
# 允许前端跨域访问 API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # 允许所有来源（生产环境应限制）
    allow_credentials=True,
    allow_methods=["*"],           # 允许所有 HTTP 方法
    allow_headers=["*"],           # 允许所有请求头
)

# 注册路由
app.include_router(imagery.router)


@app.get("/")
async def root():
    """
    根路径 - 服务健康检查。

    Returns:
        dict: 服务状态信息
    """
    return {
        "service": "RS-Platform API",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health")
async def health_check():
    """
    健康检查端点。

    用于监控系统和服务发现。

    Returns:
        dict: 健康状态
    """
    from config.settings import ZARR_DIR, DOWNLOADS_DIR

    return {
        "status": "healthy",
        "data_dir_exists": ZARR_DIR.parent.exists(),
        "zarr_dir_exists": ZARR_DIR.exists(),
        "downloads_dir_exists": DOWNLOADS_DIR.exists(),
    }
