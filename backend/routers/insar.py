"""
insar.py - InSAR 处理 API 路由

提供 InSAR 形变监测的 REST API 端点：
- POST /api/insar/download          下载 SLC 数据
- POST /api/insar/process           执行 InSAR 处理
- GET  /api/insar/tasks             列出所有任务
- GET  /api/insar/tasks/{task_id}   获取任务详情
- GET  /api/insar/health            检查 GMTSAR 服务健康状态
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import APIRouter, BackgroundTasks, HTTPException

from backend.models.insar import (
    InSARDeformationStats,
    InSARListResponse,
    InSARProcessRequest,
    InSARProcessResponse,
    InSARTaskInfo,
    SLCDownloadRequest,
    SLCDownloadResponse,
)

router = APIRouter(prefix="/api/insar", tags=["insar"])

# 任务存储（简单实现，生产环境应使用数据库）
_tasks: dict = {}


def _get_data_dir() -> Path:
    """获取数据目录"""
    return Path(__file__).resolve().parent.parent.parent / "data"


def _run_slc_download_task(task_id: str, request: SLCDownloadRequest):
    """后台执行 SLC 数据下载任务"""
    import traceback

    try:
        # 更新任务状态
        _tasks[task_id]["status"] = "running"

        # 动态导入 SLC 下载模块
        import importlib.util

        scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
        cdse_path = scripts_dir / "cdse_s1_slc.py"
        spec = importlib.util.spec_from_file_location("cdse_s1_slc", cdse_path)
        cdse_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cdse_module)

        # 搜索 SLC 产品
        products = cdse_module.search_slc(
            bbox=request.bbox,
            date_range=request.date_range,
        )

        if not products:
            raise ValueError("未找到符合条件的 SLC 产品")

        # 如果指定了轨道方向，过滤
        if request.orbit_direction:
            products = [p for p in products if p.get("orbit_direction") == request.orbit_direction]
            if not products:
                raise ValueError(f"未找到轨道方向为 {request.orbit_direction} 的 SLC 产品")

        # 确定输出目录
        if request.output_dir:
            output_dir = Path(request.output_dir)
        else:
            output_dir = _get_data_dir() / "tasks" / f"{task_id}_S1_SLC" / "downloads" / "s1_slc"

        # 下载 SLC 数据
        downloaded = cdse_module.download_slc(products, output_dir)

        # 提取波段
        for safe_path in downloaded:
            cdse_module.extract_bands(safe_path.get("path", safe_path))

        # 更新任务状态
        _tasks[task_id]["status"] = "completed"
        _tasks[task_id]["completed_at"] = datetime.now().isoformat()
        _tasks[task_id]["output_dir"] = str(output_dir)
        _tasks[task_id]["downloaded"] = [str(d.get("path", d)) for d in downloaded]

    except Exception as e:
        # 更新任务状态为失败
        _tasks[task_id]["status"] = "failed"
        _tasks[task_id]["error"] = f"{str(e)}\n{traceback.format_exc()}"


def _run_insar_task(task_id: str, request: InSARProcessRequest):
    """后台执行 InSAR 处理任务"""
    import traceback

    try:
        # 更新任务状态
        _tasks[task_id]["status"] = "running"

        # 动态导入 InSAR 模块
        import importlib.util

        scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
        insar_path = scripts_dir / "09_insar_analysis.py"
        spec = importlib.util.spec_from_file_location("insar_analysis", insar_path)
        insar_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(insar_module)

        # 确定数据目录
        data_dir = Path(request.data_dir) if request.data_dir else _get_data_dir()
        slc_dir = Path(request.slc_dir) if request.slc_dir else None

        # 确定主从影像
        if request.master and request.slave:
            master_path = Path(request.master)
            slave_path = Path(request.slave)
        else:
            # 查找 SLC 文件
            slc_files = insar_module.list_slc_scenes(data_dir, slc_dir)
            if len(slc_files) < 2:
                raise ValueError(f"需要至少 2 幅 SLC 影像，当前找到 {len(slc_files)} 幅")

            # 自动选择前两幅
            master_path = slc_files[0]
            slave_path = slc_files[1]

        # 更新任务信息
        _tasks[task_id]["master"] = master_path.name
        _tasks[task_id]["slave"] = slave_path.name

        # 执行 InSAR 处理
        output_dir = Path(request.output_dir) if request.output_dir else None
        result = insar_module.run_insar(
            master_path=master_path,
            slave_path=slave_path,
            polarization=request.polarization,
            output_dir=output_dir,
        )

        # 更新任务状态
        _tasks[task_id]["status"] = "completed"
        _tasks[task_id]["completed_at"] = datetime.now().isoformat()
        _tasks[task_id]["output_dir"] = result.get("output_dir")
        _tasks[task_id]["result"] = result

    except Exception as e:
        # 更新任务状态为失败
        _tasks[task_id]["status"] = "failed"
        _tasks[task_id]["error"] = f"{str(e)}\n{traceback.format_exc()}"


@router.post("/download", response_model=SLCDownloadResponse)
async def download_slc(request: SLCDownloadRequest, background_tasks: BackgroundTasks):
    """
    下载 Sentinel-1 SLC 数据。

    从 Copernicus Data Space Ecosystem (CDSE) 搜索并下载 SLC 数据。
    下载在后台异步执行，立即返回任务 ID。

    Args:
        request: SLC 下载请求参数
    """
    # 生成任务 ID
    task_id = f"slc_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # 创建任务记录
    _tasks[task_id] = {
        "task_id": task_id,
        "type": "slc_download",
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "output_dir": None,
        "downloaded": [],
        "error": None,
    }

    # 在后台执行任务
    background_tasks.add_task(_run_slc_download_task, task_id, request)

    return SLCDownloadResponse(
        status="accepted",
        task_id=task_id,
        message="SLC 数据下载任务已提交，正在后台处理",
    )


@router.get("/health")
async def health_check():
    """
    检查 GMTSAR 服务健康状态。

    返回 GMTSAR Docker 服务的可用性信息。
    """
    try:
        import importlib.util

        scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
        client_path = scripts_dir / "insar_client.py"
        spec = importlib.util.spec_from_file_location("insar_client", client_path)
        client_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client_module)

        is_healthy = client_module.check_service_health()

        return {
            "status": "healthy" if is_healthy else "unavailable",
            "gmtsar_available": is_healthy,
            "message": "GMTSAR 服务就绪" if is_healthy else "GMTSAR 服务不可用，请启动 Docker 容器: docker-compose up -d gmtsar",
        }
    except Exception as e:
        return {
            "status": "error",
            "gmtsar_available": False,
            "message": f"健康检查失败: {str(e)}",
        }


@router.post("/process", response_model=InSARProcessResponse)
async def process_insar(request: InSARProcessRequest, background_tasks: BackgroundTasks):
    """
    执行 InSAR 处理。

    支持三种数据来源方式（优先级：master/slave > slc_dir > data_dir）：
    - master + slave: 直接指定主从影像路径
    - slc_dir: 指定 SLC 数据目录
    - data_dir: 从任务目录中自动查找 SLC 数据

    处理在后台异步执行，立即返回任务 ID。
    """
    # 验证参数
    if not request.master and not request.slc_dir and not request.data_dir:
        raise HTTPException(
            status_code=400,
            detail="必须提供数据来源：master/slave、slc_dir 或 data_dir",
        )

    # 生成任务 ID
    task_id = f"insar_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # 创建任务记录
    _tasks[task_id] = {
        "task_id": task_id,
        "status": "pending",
        "master": request.master or "",
        "slave": request.slave or "",
        "polarization": request.polarization,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "output_dir": None,
        "error": None,
        "result": None,
    }

    # 在后台执行任务
    background_tasks.add_task(_run_insar_task, task_id, request)

    return InSARProcessResponse(
        status="accepted",
        task_id=task_id,
        message="InSAR 处理任务已提交，正在后台处理",
    )


@router.get("/tasks", response_model=InSARListResponse)
async def list_tasks():
    """
    列出所有 InSAR 处理任务。

    返回所有任务的基本信息和状态。
    """
    tasks = []
    for task_id, task_info in _tasks.items():
        tasks.append(
            InSARTaskInfo(
                task_id=task_info["task_id"],
                status=task_info["status"],
                master=task_info["master"],
                slave=task_info["slave"],
                polarization=task_info["polarization"],
                created_at=task_info["created_at"],
                completed_at=task_info.get("completed_at"),
                output_dir=task_info.get("output_dir"),
                error=task_info.get("error"),
            )
        )

    return InSARListResponse(tasks=tasks, total=len(tasks))


@router.get("/tasks/{task_id}", response_model=InSARProcessResponse)
async def get_task(task_id: str):
    """
    获取指定任务的详细信息。

    Args:
        task_id: 任务 ID
    """
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    task_info = _tasks[task_id]

    # 构建形变统计信息
    deformation = None
    if task_info.get("result", {}).get("report", {}).get("deformation"):
        stats = task_info["result"]["report"]["deformation"]
        deformation = InSARDeformationStats(
            mean_mm=stats.get("mean_mm", 0),
            std_mm=stats.get("std_mm", 0),
            max_uplift_mm=stats.get("max_uplift_mm", 0),
            max_subsidence_mm=stats.get("max_subsidence_mm", 0),
            valid_pixels=stats.get("valid_pixels", 0),
        )

    return InSARProcessResponse(
        status=task_info["status"],
        task_id=task_info["task_id"],
        output_dir=task_info.get("output_dir"),
        files=task_info.get("result", {}).get("files", {}),
        deformation=deformation,
        message=task_info.get("error") or "处理完成" if task_info["status"] == "completed" else task_info["status"],
    )
