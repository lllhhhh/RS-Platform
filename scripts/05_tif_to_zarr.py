"""
05_tif_to_zarr.py - TIF 转 ZARR 格式转换脚本

功能：
1. 读取去云后的 RGB TIF 文件
2. 使用 rioxarray 转换为 xarray.DataArray
3. 设置合适的 chunk 大小（1024x1024）
4. 保存为 ZARR 格式，保留空间坐标和 CRS 信息

为什么转 ZARR：
- ZARR 是为云端/分布式存储设计的数组格式
- 支持分块（chunk）读取，前端只需加载可视区域的数据
- 压缩率高，存储空间更小
- 支持并发读取，多用户同时访问不阻塞
- 与 xarray/dask 生态无缝集成

使用方法：
    python scripts/05_tif_to_zarr.py --data-dir ./data
"""

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path

# 修复 xarray 检测编译扩展模块 __version__ 为 'unknown' 导致 packaging 报错的问题
# xarray 内部会检查所有 duck array 模块的版本（dask, zarr, numcodecs 等）
# 任何模块的 __version__ 为 'unknown' 都会触发此错误
# 必须在 import xarray 之前执行
import importlib
_duck_array_modules = ["dask", "zarr", "numcodecs", "sparse", "cupy"]
for _mod_name in _duck_array_modules:
    try:
        _mod = importlib.import_module(_mod_name)
        if not hasattr(_mod, "__version__") or _mod.__version__ == "unknown":
            setattr(_mod, "__version__", "0.0.0")
    except ImportError:
        pass

import rioxarray
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (
    CLOUD_MASKED_DIR,
    ZARR_CHUNK_SIZE,
    ZARR_DIR,
)

# 检查 zarr 版本，必须是 v2.x（v3 与当前 xarray 不兼容）
import zarr as _zarr_mod
_zarr_ver_str = getattr(_zarr_mod, "__version__", "2.18.0")
ZARR_VERSION = tuple(int(x) for x in _zarr_ver_str.split(".")[:2])
if ZARR_VERSION[0] >= 3:
    print(f"[错误] 检测到 zarr v{_zarr_ver_str}（v3），与 xarray 存在兼容性问题")
    print("  请执行以下命令降级：")
    print('  pip uninstall zarr numcodecs -y && pip install "zarr>=2.16.0,<3" "numcodecs>=0.12.0,<0.15"')
    sys.exit(1)

# 检查 numcodecs 版本（0.15+ 移除了 blosc.cbuffer_sizes，与 zarr v2 不兼容）
import numcodecs as _numcodecs_mod
_numcodecs_ver_str = getattr(_numcodecs_mod, "__version__", "0.13.0")
NUMCODECS_VERSION = tuple(int(x) for x in _numcodecs_ver_str.split(".")[:2])
if NUMCODECS_VERSION[0] == 0 and NUMCODECS_VERSION[1] >= 15:
    print(f"[错误] 检测到 numcodecs v{_numcodecs_ver_str}（>=0.15），与 zarr v2 不兼容")
    print("  请执行以下命令降级：")
    print('  pip install "numcodecs>=0.12.0,<0.15"')
    sys.exit(1)


def convert_tif_to_zarr(
    tif_path: Path,
    output_dir: Path,
    chunk_size: dict = None,
) -> Path:
    """
    将单个 TIF 文件转换为 ZARR 格式。

    转换流程：
    1. 使用 rioxarray 打开 GeoTIFF（保留空间坐标和 CRS）
    2. 设置 chunk 大小（用于分块存储）
    3. 保存为 ZARR 格式
    4. 写入额外的元数据（原始文件名、转换时间等）

    Args:
        tif_path: 输入 TIF 文件路径
        output_dir: ZARR 输出目录
        chunk_size: chunk 大小字典，如 {"x": 1024, "y": 1024}

    Returns:
        Path: ZARR 目录路径
    """
    if chunk_size is None:
        chunk_size = ZARR_CHUNK_SIZE

    # ZARR 输出目录名：与 TIF 同名但扩展名为 .zarr
    zarr_name = tif_path.stem + ".zarr"
    zarr_path = output_dir / zarr_name

    # 如果已存在，检查是否为 v3 格式（损坏的），如果是则删除重建
    if zarr_path.exists():
        # 检查是否包含 v3 标志文件 zarr.json
        if (zarr_path / "zarr.json").exists():
            print(f"  [修复] 检测到 v3 格式的 .zarr，删除后重建: {zarr_name}")
            shutil.rmtree(zarr_path)
        else:
            print(f"  [跳过] {zarr_name} 已存在")
            return zarr_path

    print(f"  [转换] {tif_path.name} → {zarr_name}")

    try:
        # 1. 使用 rioxarray 打开 GeoTIFF
        # rioxarray 会自动解析 CRS、Transform、NoData 等信息
        da = rioxarray.open_rasterio(tif_path)

        # 2. 设置 chunk 大小
        # chunk 用于控制 ZARR 存储的分块策略
        # 前端请求某个区域时，只需加载包含该区域的 chunk
        da = da.chunk(chunk_size)

        # 3. 保存为 ZARR v2 格式
        # mode='w' 表示覆盖写入
        # zarr_format=2 确保使用 v2 格式，避免与 xarray 的兼容性问题
        da.to_zarr(zarr_path, mode="w", zarr_format=2)

        # 4. 写入额外元数据
        metadata = {
            "source_file": str(tif_path.name),
            "bands": ["Red", "Green", "Blue"],
            "chunk_size": chunk_size,
            "crs": str(da.rio.crs),
            "transform": list(da.rio.transform())[:6],  # 仿射变换参数
            "shape": list(da.shape),
            "dtype": str(da.dtype),
        }
        metadata_path = zarr_path / "_rs_metadata.json"
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        # 计算 ZARR 目录大小
        total_size = sum(f.stat().st_size for f in zarr_path.rglob("*") if f.is_file())
        size_mb = total_size / (1024 * 1024)
        print(f"  [完成] {zarr_name} ({size_mb:.1f} MB)")

        return zarr_path

    except Exception as e:
        print(f"  [错误] 转换失败: {e}")
        traceback.print_exc()
        return None


def convert_all_to_zarr(data_dir: Path) -> list:
    """
    将所有去云后的 TIF 文件转换为 ZARR 格式。

    Args:
        data_dir: 数据目录

    Returns:
        list: 所有 ZARR 目录路径
    """
    cloud_masked_dir = data_dir / "cloud_masked"
    mosaicked_dir = data_dir / "mosaicked"
    zarr_dir = data_dir / "zarr"

    # 确保输出目录存在
    zarr_dir.mkdir(parents=True, exist_ok=True)

    # 按优先级扫描 TIF 文件：mosaicked > cloud_masked > merged
    tif_files = []
    if mosaicked_dir.exists():
        tif_files = sorted(mosaicked_dir.glob("*.tif"))
        if tif_files:
            print(f"[ZARR] 使用拼接裁剪后的 TIF: {mosaicked_dir}")

    if not tif_files:
        tif_files = sorted(cloud_masked_dir.glob("*_cloudmasked.tif"))

    if not tif_files:
        # 如果没有去云文件，尝试使用 merged 目录
        merged_dir = data_dir / "merged"
        tif_files = sorted(merged_dir.glob("*_RGB.tif"))
        if tif_files:
            print("[ZARR] 未找到去云文件，使用合成后的 RGB TIF")
        else:
            print("[ZARR] 未找到可转换的 TIF 文件")
            print(f"  检查目录: {mosaicked_dir}, {cloud_masked_dir} 或 {data_dir / 'merged'}")
            return []

    print(f"[ZARR] 找到 {len(tif_files)} 个 TIF 文件待转换")

    # 转换每个文件
    zarr_paths = []
    for tif_path in tif_files:
        result = convert_tif_to_zarr(tif_path, zarr_dir)
        if result:
            zarr_paths.append(result)

    print(f"\n[ZARR] 完成！共转换 {len(zarr_paths)}/{len(tif_files)} 个文件")
    return zarr_paths


def main():
    """主函数：解析参数并执行 TIF → ZARR 转换。"""
    parser = argparse.ArgumentParser(
        description="GeoTIFF → ZARR 格式转换工具",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="数据目录（包含 cloud_masked 子目录）",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        nargs=2,
        default=[ZARR_CHUNK_SIZE["x"], ZARR_CHUNK_SIZE["y"]],
        metavar=("X", "Y"),
        help=f"ZARR chunk 大小 (默认: {ZARR_CHUNK_SIZE['x']} {ZARR_CHUNK_SIZE['y']})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新转换（清除已有的 .zarr 目录）",
    )

    args = parser.parse_args()
    data_dir = Path(args.data_dir)

    # 如果指定了 --force，清除已有的 .zarr 目录
    if args.force:
        zarr_dir = data_dir / "zarr"
        if zarr_dir.exists():
            existing = list(zarr_dir.glob("*.zarr"))
            if existing:
                print(f"[强制模式] 清除 {len(existing)} 个已有 .zarr 目录...")
                for z in existing:
                    shutil.rmtree(z)
                    print(f"  已删除: {z.name}")

    # 更新 chunk 大小
    chunk_size = {"x": args.chunk_size[0], "y": args.chunk_size[1]}

    print("=" * 60)
    print("RS-Platform: TIF → ZARR 转换")
    print("=" * 60)
    print(f"Chunk 大小: {chunk_size['x']} x {chunk_size['y']}")

    zarr_paths = convert_all_to_zarr(data_dir)

    if zarr_paths:
        print(f"\nZARR 文件保存在: {data_dir / 'zarr'}")


if __name__ == "__main__":
    main()
