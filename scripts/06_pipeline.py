"""
06_pipeline.py - 整合管线脚本

功能：
串联所有处理步骤，提供统一的命令行入口：

  Step 1: STAC 搜索与 URL 签名  → 生成 urls.txt + metadata.json
  Step 2: ARIA2 批量下载        → 下载原始波段 TIF（含 Token 自动刷新）
  Step 3: 波段合成              → B02+B03+B04 合成为 RGB TIF
  Step 4: SCL 去云              → 使用场景分类层去除云像素
  Step 5: TIF → ZARR            → 转换为高效的分块存储格式

使用方法：
    python scripts/06_pipeline.py \
        --bbox 116.0 39.0 117.0 40.0 \
        --date "2024-01-01/2024-06-30" \
        --cloud-cover 20 \
        --output ./data

    # 或使用默认参数（北京市区域）
    python scripts/06_pipeline.py
"""

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (
    BOUNDARIES_DIR,
    DEFAULT_AOI_PATH,
    DEFAULT_BBOX,
    DEFAULT_CLOUD_COVER_MAX,
    DEFAULT_DATE_RANGE,
    MIN_COVERAGE_RATIO,
)


def _import_script(script_name: str):
    """
    动态导入以数字开头的脚本模块。

    Python 不允许模块名以数字开头，因此使用 importlib 动态加载。
    """
    scripts_dir = Path(__file__).resolve().parent
    script_path = scripts_dir / f"{script_name}.py"
    spec = importlib.util.spec_from_file_location(script_name, script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 动态导入各步骤模块
_search_module = _import_script("01_search_and_sign")
_download_module = _import_script("02_aria2_download")
_merge_module = _import_script("03_band_merge")
_cloud_mask_module = _import_script("04_cloud_mask")
_zarr_module = _import_script("05_tif_to_zarr")
_mosaic_clip_module = _import_script("07_mosaic_clip")


def run_pipeline(
    bbox: list,
    date_range: str,
    cloud_cover_max: int,
    output_dir: Path,
    skip_download: bool = False,
    aoi_path: str = None,
    adcode: str = None,
    admin_name: str = None,
    min_coverage: float = None,
    auto_select: bool = False,
) -> dict:
    """
    执行完整的遥感影像处理管线。

    管线流程：
    1. 搜索 MPC STAC API，获取符合条件的 Sentinel-2 L2A 影像（含覆盖率计算）
    2. 按日期分析覆盖率，交互式让用户选择要下载的时相（--auto-select 可跳过交互）
    3. 对搜索结果的资产 URL 进行签名（添加 SAS Token）
    4. 使用 ARIA2 批量下载原始波段（B02、B03、B04、SCL）
    5. 将单波段合成为 RGB 三通道 TIF
    6. 使用 SCL 去除云、云阴影、卷云像素
    7. 多景拼接 + 研究区裁剪
    8. 将去云后的 TIF 转换为 ZARR 格式

    Args:
        bbox: 搜索区域边界框 [min_lon, min_lat, max_lon, max_lat]
        date_range: 日期范围字符串
        cloud_cover_max: 最大云量百分比
        output_dir: 输出目录路径
        skip_download: 是否跳过下载步骤（用于测试后续步骤）
        aoi_path: 研究区 SHP 文件路径
        adcode: 行政区划代码（如 110000）
        admin_name: 行政区划名称（如 北京市）
        min_coverage: 最低覆盖率阈值
        auto_select: 是否自动选择最优时相（跳过交互，默认 False）

    Returns:
        dict: 管线执行结果摘要
    """
    from utils.coverage import (
        enrich_items_with_coverage,
        load_aoi_geometry,
        print_coverage_report,
        select_optimal_scenes,
    )
    from utils.datav_boundary import get_admin_boundary

    if min_coverage is None:
        min_coverage = MIN_COVERAGE_RATIO

    start_time = time.time()
    results = {
        "bbox": bbox,
        "date_range": date_range,
        "cloud_cover_max": cloud_cover_max,
        "output_dir": str(output_dir),
        "steps": {},
    }

    # 确保输出目录存在
    output_dir.mkdir(parents=True, exist_ok=True)

    # 加载研究区几何（优先级：adcode > admin-name > aoi > bbox）
    aoi_geom = None
    search_area_desc = ""
    if adcode:
        shp_path = get_admin_boundary(adcode=adcode, output_dir=BOUNDARIES_DIR)
        aoi_geom = load_aoi_geometry(aoi_path=str(shp_path))
        search_area_desc = f"行政区划 adcode={adcode}"
    elif admin_name:
        shp_path = get_admin_boundary(name=admin_name, output_dir=BOUNDARIES_DIR)
        aoi_geom = load_aoi_geometry(aoi_path=str(shp_path))
        search_area_desc = f"行政区划 {admin_name}"
    elif aoi_path:
        print(f"[研究区] 使用 SHP 文件: {aoi_path}")
        aoi_geom = load_aoi_geometry(aoi_path=aoi_path)
        search_area_desc = f"SHP: {aoi_path}"
    else:
        print(f"[研究区] 使用 bbox: {bbox}")
        aoi_geom = load_aoi_geometry(bbox=bbox)
        search_area_desc = f"bbox={bbox}"

    # 保存研究区几何供后续使用
    from shapely.geometry import mapping as shapely_mapping
    aoi_geometry_path = output_dir / "aoi_geometry.json"
    with open(aoi_geometry_path, "w", encoding="utf-8") as f:
        json.dump(shapely_mapping(aoi_geom), f)

    print("\n" + "=" * 60)
    print("RS-Platform: 遥感影像处理管线")
    print("=" * 60)
    print(f"搜索区域: {search_area_desc}")
    print(f"日期范围: {date_range}")
    print(f"最大云量: {cloud_cover_max}%")
    print(f"最低覆盖率: {min_coverage*100:.0f}%")
    print(f"输出目录: {output_dir}")
    print("=" * 60)

    # ============================================================
    # Step 1: STAC 搜索与覆盖率计算
    # ============================================================
    print("\n" + "-" * 40)
    print("Step 1/8: STAC 搜索与覆盖率计算")
    print("-" * 40)

    step1_start = time.time()
    try:
        catalog = _search_module.connect_stac_catalog()
        # 使用 aoi_geom 的 bounds 作为搜索 bbox（当使用行政区划或 SHP 时）
        search_bbox = list(aoi_geom.bounds) if aoi_geom else bbox
        items = _search_module.search_sentinel2(catalog, search_bbox, date_range, cloud_cover_max, aoi_geom)

        if not items:
            print("[管线] 未找到符合条件的影像，管线终止")
            results["steps"]["search"] = {"status": "no_results", "count": 0}
            return results

        items = enrich_items_with_coverage(items, aoi_geom)

        step1_time = time.time() - step1_start
        results["steps"]["search"] = {
            "status": "success",
            "count": len(items),
            "time_sec": round(step1_time, 1),
        }
    except Exception as e:
        print(f"[错误] Step 1 失败: {e}")
        results["steps"]["search"] = {"status": "error", "error": str(e)}
        return results

    # ============================================================
    # Step 2: 场景选择
    # ============================================================
    print("\n" + "-" * 40)
    print("Step 2/8: 场景选择")
    print("-" * 40)

    step2_start = time.time()
    try:
        selected_items, coverage_report = select_optimal_scenes(items, aoi_geom, min_coverage, auto_select=auto_select)
        print_coverage_report(coverage_report)

        if not selected_items:
            print("[管线] 无法选择满足覆盖率要求的场景组合，管线终止")
            results["steps"]["scene_selection"] = {"status": "no_selection"}
            return results

        step2_time = time.time() - step2_start
        results["steps"]["scene_selection"] = {
            "status": "success",
            "strategy": coverage_report.get("strategy"),
            "selected_count": len(selected_items),
            "total_count": len(items),
            "coverage": coverage_report.get("coverage"),
            "time_sec": round(step2_time, 1),
        }

        # 提取选中场景的 ID，后续步骤只处理这些场景
        selected_scene_ids = {item.id for item in selected_items}
    except Exception as e:
        print(f"[错误] Step 2 失败: {e}")
        results["steps"]["scene_selection"] = {"status": "error", "error": str(e)}
        return results

    # ============================================================
    # Step 3: URL 签名
    # ============================================================
    print("\n" + "-" * 40)
    print("Step 3/8: URL 签名")
    print("-" * 40)

    step3_start = time.time()
    try:
        result = _search_module.extract_signed_urls(selected_items, output_dir / "downloads", aoi_geom)
        result["metadata"]["coverage_report"] = coverage_report
        _search_module.save_aria2_input_file(result["urls"], output_dir / "urls.txt")
        _search_module.save_metadata_file(result["metadata"], output_dir / "metadata.json")

        step3_time = time.time() - step3_start
        results["steps"]["sign_urls"] = {
            "status": "success",
            "count": len(selected_items),
            "time_sec": round(step3_time, 1),
        }
    except Exception as e:
        print(f"[错误] Step 3 失败: {e}")
        results["steps"]["sign_urls"] = {"status": "error", "error": str(e)}
        return results

    # ============================================================
    # Step 4: ARIA2 批量下载
    # ============================================================
    if not skip_download:
        print("\n" + "-" * 40)
        print("Step 4/8: ARIA2 批量下载")
        print("-" * 40)

        step4_start = time.time()
        try:
            downloader = _download_module.Aria2Downloader(
                data_dir=output_dir,
                aria2_path=Path(__file__).resolve().parent.parent
                / "aria2-1.37.0-win-64bit-build1"
                / "aria2c.exe",
            )
            downloader.start()

            step4_time = time.time() - step4_start
            results["steps"]["download"] = {
                "status": "success",
                "time_sec": round(step4_time, 1),
            }
        except Exception as e:
            print(f"[错误] Step 4 失败: {e}")
            results["steps"]["download"] = {"status": "error", "error": str(e)}
            return results
    else:
        print("\n[管线] 跳过下载步骤（--skip-download）")
        results["steps"]["download"] = {"status": "skipped"}

    # ============================================================
    # Step 5: 波段合成
    # ============================================================
    print("\n" + "-" * 40)
    print("Step 5/8: 波段合成")
    print("-" * 40)

    step5_start = time.time()
    try:
        merged_files = _merge_module.merge_all_scenes(output_dir, scene_ids=selected_scene_ids)

        step5_time = time.time() - step5_start
        results["steps"]["merge"] = {
            "status": "success",
            "count": len(merged_files),
            "time_sec": round(step5_time, 1),
        }
    except Exception as e:
        print(f"[错误] Step 5 失败: {e}")
        results["steps"]["merge"] = {"status": "error", "error": str(e)}
        return results

    # ============================================================
    # Step 6: SCL 去云处理
    # ============================================================
    print("\n" + "-" * 40)
    print("Step 6/8: SCL 去云处理")
    print("-" * 40)

    step6_start = time.time()
    try:
        masked_files = _cloud_mask_module.process_all_merged_scenes(output_dir, scene_ids=selected_scene_ids)

        step6_time = time.time() - step6_start
        results["steps"]["cloud_mask"] = {
            "status": "success",
            "count": len(masked_files),
            "time_sec": round(step6_time, 1),
        }
    except Exception as e:
        print(f"[错误] Step 6 失败: {e}")
        results["steps"]["cloud_mask"] = {"status": "error", "error": str(e)}
        return results

    # ============================================================
    # Step 7: 多景拼接 + 研究区裁剪
    # ============================================================
    print("\n" + "-" * 40)
    print("Step 7/8: 拼接 + 裁剪")
    print("-" * 40)

    step7_start = time.time()
    try:
        mosaicked_paths = _mosaic_clip_module.process_mosaic_clip(output_dir, min_coverage=min_coverage, scene_ids=selected_scene_ids)

        step7_time = time.time() - step7_start
        results["steps"]["mosaic_clip"] = {
            "status": "success" if mosaicked_paths else "skipped",
            "output_paths": [str(p) for p in mosaicked_paths] if mosaicked_paths else [],
            "count": len(mosaicked_paths),
            "time_sec": round(step7_time, 1),
        }
    except Exception as e:
        print(f"[错误] Step 7 失败: {e}")
        results["steps"]["mosaic_clip"] = {"status": "error", "error": str(e)}
        return results

    # ============================================================
    # Step 8: TIF → ZARR 转换
    # ============================================================
    print("\n" + "-" * 40)
    print("Step 8/8: TIF → ZARR 转换")
    print("-" * 40)

    step8_start = time.time()
    try:
        zarr_paths = _zarr_module.convert_all_to_zarr(output_dir)

        step8_time = time.time() - step8_start
        results["steps"]["zarr_convert"] = {
            "status": "success",
            "count": len(zarr_paths),
            "time_sec": round(step8_time, 1),
        }
    except Exception as e:
        print(f"[错误] Step 8 失败: {e}")
        results["steps"]["zarr_convert"] = {"status": "error", "error": str(e)}
        return results

    # ============================================================
    # 管线完成
    # ============================================================
    total_time = time.time() - start_time
    results["total_time_sec"] = round(total_time, 1)

    print("\n" + "=" * 60)
    print("管线执行完成！")
    print("=" * 60)
    print(f"总耗时: {total_time:.1f} 秒")
    print(f"\n输出文件位置:")
    print(f"  原始波段: {output_dir / 'downloads'}")
    print(f"  合成 TIF: {output_dir / 'merged'}")
    print(f"  去云 TIF: {output_dir / 'cloud_masked'}")
    print(f"  拼接裁剪: {output_dir / 'mosaicked'}")
    print(f"  ZARR 数据: {output_dir / 'zarr'}")
    print(f"  元数据: {output_dir / 'metadata.json'}")
    print(f"  研究区几何: {output_dir / 'aoi_geometry.json'}")
    print("=" * 60)

    return results


def main():
    """主函数：解析参数并执行管线。"""
    parser = argparse.ArgumentParser(
        description="RS-Platform 遥感影像处理管线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 使用默认参数（北京市区域）
  python scripts/06_pipeline.py

  # 使用 bbox
  python scripts/06_pipeline.py \\
      --bbox 116.0 39.0 117.0 40.0 \\
      --date "2024-01-01/2024-06-30" \\
      --cloud-cover 20 \\
      --output ./data

  # 使用 shp 文件
  python scripts/06_pipeline.py \\
      --aoi ./data/beijing_boundary.shp \\
      --date "2024-01-01/2024-06-30" \\
      --cloud-cover 20 \\
      --output ./data

  # 使用行政区划 adcode
  python scripts/06_pipeline.py \\
      --adcode 110000 \\
      --date "2024-01-01/2024-06-30"

  # 使用行政区划名称（模糊搜索）
  python scripts/06_pipeline.py \\
      --admin-name "北京市" \\
      --date "2024-01-01/2024-06-30"

  # 跳过下载（仅测试后续处理步骤）
  python scripts/06_pipeline.py --skip-download

  # 自动选择最优时相（跳过交互选择）
  python scripts/06_pipeline.py --auto-select
        """,
    )
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        default=DEFAULT_BBOX,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        help=f"搜索区域边界框 (默认: {DEFAULT_BBOX})",
    )
    parser.add_argument(
        "--aoi",
        type=str,
        default=DEFAULT_AOI_PATH,
        help="研究区 SHP 文件路径",
    )
    parser.add_argument(
        "--adcode",
        type=str,
        default=None,
        help="行政区划代码（如 110000），自动从 DataV 获取边界",
    )
    parser.add_argument(
        "--admin-name",
        type=str,
        default=None,
        help="行政区划名称（如 北京市），模糊搜索并自动获取边界",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=DEFAULT_DATE_RANGE,
        help=f"日期范围 (默认: {DEFAULT_DATE_RANGE})",
    )
    parser.add_argument(
        "--cloud-cover",
        type=int,
        default=DEFAULT_CLOUD_COVER_MAX,
        help=f"最大云量百分比 (默认: {DEFAULT_CLOUD_COVER_MAX})",
    )
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=MIN_COVERAGE_RATIO,
        help=f"最低覆盖率阈值 (默认: {MIN_COVERAGE_RATIO})",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="输出目录 (默认: ./data)",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="跳过下载步骤（用于测试后续处理步骤）",
    )
    parser.add_argument(
        "--auto-select",
        action="store_true",
        help="自动选择最优时相（跳过交互选择）",
    )

    args = parser.parse_args()
    output_dir = Path(args.output)

    run_pipeline(
        bbox=args.bbox,
        date_range=args.date,
        cloud_cover_max=args.cloud_cover,
        output_dir=output_dir,
        skip_download=args.skip_download,
        aoi_path=args.aoi,
        adcode=args.adcode,
        admin_name=args.admin_name,
        min_coverage=args.min_coverage,
        auto_select=args.auto_select,
    )


if __name__ == "__main__":
    main()
