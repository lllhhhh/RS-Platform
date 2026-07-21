"""
dem_downloader.py - DEM 数据下载和管理模块

功能：
使用 PyGMTSAR 的 Tiles 类从 AWS 下载 SRTM 或 Copernicus DEM。
支持为指定区域或 SLC 场景覆盖区域下载 DEM。

使用方式：
    from scripts.dem_downloader import download_dem_for_aoi, load_dem

    # 为指定边界下载 DEM
    dem_path = get_dem_for_bounds(116.0, 39.0, 117.0, 40.0)

    # 为 SLC 场景下载 DEM
    dem_path = download_dem_for_slc_scenes("data/tasks/xxx/downloads/s1_slc")

    # 加载 DEM
    dem = load_dem(dem_path)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# DEM 存储目录
DEM_DIR = Path(__file__).resolve().parent.parent / "data" / "dem"


def download_dem_for_aoi(
    aoi_geometry,
    dem_type: str = "copernicus",
    resolution: str = "1s",
    output_filename: str = None,
) -> Path:
    """
    为指定区域下载 DEM 数据。

    Args:
        aoi_geometry: 研究区几何，支持以下格式：
            - Shapely Geometry (Polygon, box 等)
            - GeoDataFrame
            - xarray Dataset/DataArray（如 S1.scan_slc() 的输出）
            - TIF 文件路径字符串或 Path
        dem_type: DEM 类型（"copernicus" 或 "srtm"）
        resolution: 分辨率（"1s" 对应 30m，"3s" 对应 90m）
        output_filename: 输出文件名（可选，默认自动生成）

    Returns:
        Path: DEM 文件路径（NetCDF 格式）
    """
    from pygmtsar import Tiles

    DEM_DIR.mkdir(parents=True, exist_ok=True)

    tiles = Tiles()

    if output_filename is None:
        output_filename = f"dem_{dem_type}_{resolution}.nc"

    output_path = DEM_DIR / output_filename

    if output_path.exists():
        print(f"  [DEM] 已存在: {output_path}")
        return output_path

    print(f"  [DEM] 下载 {dem_type} DEM ({resolution})...")

    try:
        if dem_type == "copernicus":
            dem = tiles.download_dem_glo(
                aoi_geometry,
                filename=str(output_path),
                product=resolution,
            )
        elif dem_type == "srtm":
            dem = tiles.download_dem_srtm(
                aoi_geometry,
                filename=str(output_path),
                product=resolution,
            )
        else:
            raise ValueError(f"不支持的 DEM 类型: {dem_type}")

        print(f"  [DEM] 下载完成: {output_path}")
        return output_path

    except Exception as e:
        print(f"  [DEM] 下载失败: {e}")
        raise


def download_dem_for_slc_scenes(
    slc_dir: str,
    dem_type: str = "copernicus",
    resolution: str = "1s",
) -> Path:
    """
    为 SLC 场景覆盖区域下载 DEM。

    Args:
        slc_dir: SLC 数据目录（包含 .SAFE 目录）
        dem_type: DEM 类型
        resolution: 分辨率

    Returns:
        Path: DEM 文件路径
    """
    from pygmtsar import S1

    print(f"  [DEM] 扫描 SLC 场景: {slc_dir}")

    # 扫描 SLC 场景获取覆盖范围
    try:
        scenes = S1.scan_slc(slc_dir)
        print(f"  [DEM] 找到 {len(scenes)} 个场景")
    except Exception as e:
        print(f"  [DEM] 扫描失败: {e}")
        raise

    # 使用场景几何下载 DEM
    slc_path = Path(slc_dir)
    output_filename = f"dem_{slc_path.name}_{dem_type}_{resolution}.nc"

    return download_dem_for_aoi(
        scenes,
        dem_type=dem_type,
        resolution=resolution,
        output_filename=output_filename,
    )


def download_dem_for_merged_tif(
    tif_path: Path,
    dem_type: str = "copernicus",
    resolution: str = "1s",
) -> Path:
    """
    为 merged TIF 文件覆盖区域下载 DEM。

    Args:
        tif_path: merged TIF 文件路径
        dem_type: DEM 类型
        resolution: 分辨率

    Returns:
        Path: DEM 文件路径
    """
    import rasterio

    print(f"  [DEM] 读取 TIF 边界: {tif_path}")

    with rasterio.open(str(tif_path)) as src:
        bounds = src.bounds  # (left, bottom, right, top)

    from shapely.geometry import box
    geometry = box(bounds.left, bounds.bottom, bounds.right, bounds.top)

    output_filename = f"dem_{tif_path.stem}_{dem_type}_{resolution}.nc"

    return download_dem_for_aoi(
        geometry,
        dem_type=dem_type,
        resolution=resolution,
        output_filename=output_filename,
    )


def load_dem(dem_path: Path):
    """
    加载 DEM 数据为 xarray DataArray。

    Args:
        dem_path: DEM 文件路径（NetCDF 或 GeoTIFF）

    Returns:
        xarray.DataArray: DEM 数据
    """
    import xarray as xr

    dem_path = Path(dem_path)

    if not dem_path.exists():
        raise FileNotFoundError(f"DEM 文件不存在: {dem_path}")

    if dem_path.suffix == ".nc":
        dem = xr.open_dataarray(str(dem_path))
    elif dem_path.suffix in [".tif", ".tiff"]:
        dem = xr.open_dataarray(str(dem_path), engine="rasterio")
    else:
        # 尝试自动识别
        dem = xr.open_dataarray(str(dem_path))

    return dem


def get_dem_for_bounds(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    dem_type: str = "copernicus",
    resolution: str = "1s",
) -> Path:
    """
    为指定经纬度边界下载 DEM。

    Args:
        min_lon, min_lat, max_lon, max_lat: 经纬度边界
        dem_type: DEM 类型
        resolution: 分辨率

    Returns:
        Path: DEM 文件路径
    """
    from shapely.geometry import box

    geometry = box(min_lon, min_lat, max_lon, max_lat)

    output_filename = f"dem_{min_lon}_{min_lat}_{max_lon}_{max_lat}_{dem_type}_{resolution}.nc"

    return download_dem_for_aoi(
        geometry,
        dem_type=dem_type,
        resolution=resolution,
        output_filename=output_filename,
    )


def list_available_dems() -> list:
    """
    列出已下载的 DEM 文件。

    Returns:
        list: DEM 文件路径列表
    """
    if not DEM_DIR.exists():
        return []

    return sorted(DEM_DIR.glob("*.nc")) + sorted(DEM_DIR.glob("*.tif"))


def main():
    """独立运行：下载指定区域的 DEM。"""
    import argparse

    parser = argparse.ArgumentParser(description="DEM 数据下载工具")
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
                        help="经纬度边界")
    parser.add_argument("--slc-dir", type=str, help="SLC 数据目录")
    parser.add_argument("--tif", type=str, help="TIF 文件路径")
    parser.add_argument("--dem-type", default="copernicus", choices=["copernicus", "srtm"],
                        help="DEM 类型（默认 copernicus）")
    parser.add_argument("--resolution", default="1s", choices=["1s", "3s"],
                        help="分辨率（1s=30m, 3s=90m，默认 1s）")
    parser.add_argument("--list", action="store_true", help="列出已下载的 DEM")

    args = parser.parse_args()

    print("=" * 60)
    print("DEM 数据下载工具")
    print("=" * 60)

    if args.list:
        dems = list_available_dems()
        if dems:
            print(f"\n已下载 {len(dems)} 个 DEM 文件:")
            for dem in dems:
                size_mb = dem.stat().st_size / (1024 * 1024)
                print(f"  {dem.name} ({size_mb:.1f} MB)")
        else:
            print("\n未找到已下载的 DEM 文件")
        return

    try:
        if args.bbox:
            min_lon, min_lat, max_lon, max_lat = args.bbox
            dem_path = get_dem_for_bounds(
                min_lon, min_lat, max_lon, max_lat,
                dem_type=args.dem_type,
                resolution=args.resolution,
            )
        elif args.slc_dir:
            dem_path = download_dem_for_slc_scenes(
                args.slc_dir,
                dem_type=args.dem_type,
                resolution=args.resolution,
            )
        elif args.tif:
            dem_path = download_dem_for_merged_tif(
                Path(args.tif),
                dem_type=args.dem_type,
                resolution=args.resolution,
            )
        else:
            parser.print_help()
            return

        print(f"\n下载完成: {dem_path}")

    except Exception as e:
        print(f"\n下载失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
