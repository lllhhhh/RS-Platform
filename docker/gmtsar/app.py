"""
GMTSAR InSAR 处理服务

提供 HTTP API 接口，调用 PyGMTSAR 执行 InSAR 处理。
运行在 Docker 容器中，通过共享卷与主应用交换数据。
"""

import os
import shutil
import traceback
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(
    title="GMTSAR InSAR Service",
    description="基于 PyGMTSAR 的 InSAR 处理服务",
    version="1.0.0",
)

# 数据目录（容器内路径）
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))


class InSARRequest(BaseModel):
    """InSAR 处理请求"""

    task_id: str  # 任务 ID
    master_path: str  # 主影像路径（相对于 data 目录）
    slave_path: str  # 从影像路径
    polarization: str = "vv"  # 极化通道（"vv" 或 "vh"）
    subswath: int = 2  # 子条带（1, 2, 3）
    output_dir: Optional[str] = None  # 输出目录（可选）


class InSARResponse(BaseModel):
    """InSAR 处理响应"""

    status: str  # success / error
    task_id: str
    output_dir: str
    files: dict  # 输出文件列表
    report: dict  # 统计报告
    message: str = ""


@app.get("/health")
async def health_check():
    """健康检查"""
    import shutil

    gmtsar_available = shutil.which("make_s1a_tops") is not None

    # 检查 PyGMTSAR 是否可用
    pygmtsar_available = False
    try:
        import pygmtsar

        pygmtsar_available = True
    except ImportError:
        pass

    return {
        "status": "healthy",
        "gmtsar_available": gmtsar_available,
        "pygmtsar_available": pygmtsar_available,
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists(),
    }


@app.post("/insar/process", response_model=InSARResponse)
async def process_insar(request: InSARRequest):
    """
    执行 InSAR 处理。

    处理链：
    1. 扫描 SLC 场景
    2. 下载轨道文件
    3. 下载 DEM
    4. 配准（Coregistration）
    5. 干涉图生成（含 DEM 去地形相位）
    6. Goldstein 滤波
    7. 相位解缠（Snaphu）
    8. 形变提取
    9. 导出 GeoTIFF
    """
    import numpy as np
    from pygmtsar import S1, Stack

    # 解析路径
    master = DATA_DIR / request.master_path
    slave = DATA_DIR / request.slave_path

    if not master.exists():
        raise HTTPException(status_code=404, detail=f"主影像不存在: {master}")
    if not slave.exists():
        raise HTTPException(status_code=404, detail=f"从影像不存在: {slave}")

    # 输出目录
    if request.output_dir:
        output_dir = DATA_DIR / request.output_dir
    else:
        output_dir = DATA_DIR / "tasks" / request.task_id / "insar"

    # 清理已有输出
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. 扫描 SLC 场景
        print(f"[InSAR] 扫描 SLC 场景...")
        slc_dir = str(master.parent)
        scenes = S1.scan_slc(
            slc_dir,
            polarization=request.polarization.upper(),
            subswath=request.subswath,
        )
        print(f"[InSAR] 找到 {len(scenes)} 个场景")

        # 2. 下载轨道文件
        print(f"[InSAR] 下载轨道文件...")
        S1.download_orbits(slc_dir, scenes)

        # 3. 重新扫描（带轨道信息）
        print(f"[InSAR] 重新扫描场景（含轨道信息）...")
        scenes = S1.scan_slc(
            slc_dir,
            polarization=request.polarization.upper(),
            subswath=request.subswath,
        )

        # 4. 初始化 Stack
        print(f"[InSAR] 初始化 Stack...")
        stack = Stack(basedir=str(output_dir))
        stack.set_scenes(scenes)
        print(f"[InSAR] 参考场景: {stack.reference}")

        # 5. 下载 DEM
        print(f"[InSAR] 下载 DEM...")
        try:
            stack.download_dem()
        except Exception as e:
            print(f"[InSAR] DEM 下载警告: {e}")

        # 6. 配准
        print(f"[InSAR] 配准（Coregistration）...")
        stack.compute_align()
        print(f"[InSAR] 配准完成")

        # 7. 生成干涉对
        print(f"[InSAR] 生成干涉对...")
        dates = scenes.index.tolist()
        pairs = stack.get_pairs([[dates[0], dates[1]]])
        print(f"[InSAR] 干涉对: {dates[0]} -> {dates[1]}")

        # 8. 干涉图生成（含 DEM 去地形相位）
        print(f"[InSAR] 干涉图生成（含 DEM 去地形相位）...")
        stack.compute_interferogram(pairs, "ifg")
        print(f"[InSAR] 干涉图生成完成")

        # 9. 打开结果
        print(f"[InSAR] 打开干涉图结果...")
        ifg = stack.open_stack("ifg")
        print(f"[InSAR] 干涉图波段: {list(ifg.data_vars)}")

        # 10. Goldstein 滤波
        print(f"[InSAR] Goldstein 滤波...")
        phase_filt = stack.goldstein(ifg["phase"], ifg["correlation"])
        print(f"[InSAR] Goldstein 滤波完成")

        # 11. 相位解缠
        print(f"[InSAR] 相位解缠（Snaphu）...")
        unwrapped = stack.unwrap_snaphu(phase_filt, weight=ifg["correlation"])
        print(f"[InSAR] 相位解缠完成")

        # 12. 形变提取（LOS 方向，单位 mm）
        print(f"[InSAR] 形变提取...")
        los_disp = stack.los_displacement_mm(unwrapped)
        print(f"[InSAR] 形变提取完成")

        # 13. 计算统计信息
        valid = los_disp.values[~np.isnan(los_disp.values)]
        report = {
            "deformation": {
                "mean_mm": float(np.mean(valid)) if valid.size > 0 else 0,
                "std_mm": float(np.std(valid)) if valid.size > 0 else 0,
                "max_uplift_mm": float(np.max(valid)) if valid.size > 0 else 0,
                "max_subsidence_mm": float(np.min(valid)) if valid.size > 0 else 0,
                "valid_pixels": int(valid.size),
            }
        }
        print(f"[InSAR] 形变统计: {report['deformation']}")

        # 14. 导出 GeoTIFF
        print(f"[InSAR] 导出 GeoTIFF...")
        stack.export_geotiff(ifg["phase"], "phase")
        stack.export_geotiff(ifg["correlation"], "correlation")
        stack.export_geotiff(phase_filt, "phase_filtered")
        stack.export_geotiff(unwrapped, "unwrapped_phase")
        stack.export_geotiff(los_disp, "deformation_los_mm")
        print(f"[InSAR] 导出完成")

        # 收集输出文件
        files = {}
        for tif in output_dir.glob("*.tif"):
            files[tif.stem] = str(tif.relative_to(DATA_DIR))

        return InSARResponse(
            status="success",
            task_id=request.task_id,
            output_dir=str(output_dir.relative_to(DATA_DIR)),
            files=files,
            report=report,
            message="InSAR 处理完成",
        )

    except Exception as e:
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        print(f"[InSAR] 错误: {error_msg}")

        return InSARResponse(
            status="error",
            task_id=request.task_id,
            output_dir="",
            files={},
            report={},
            message=error_msg,
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
