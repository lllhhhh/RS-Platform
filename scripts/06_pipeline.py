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
from datetime import datetime
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
_s1_preprocess_module = _import_script("08_s1_preprocess")
_cdse_slc_module = _import_script("cdse_s1_slc")
_task_manager = _import_script("task_manager")


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
    satellite: str = "sentinel2",
    s1_product: str = "grd",
    bands: list = None,
) -> dict:
    """
    执行完整的遥感影像处理管线。

    管线流程（9 步）：
    1. 搜索 MPC STAC API，获取符合条件的影像（含覆盖率计算）
    2. 按日期分析覆盖率，交互式让用户选择要下载的时相（--auto-select 可跳过交互）
    3. 对搜索结果的资产 URL 进行签名（添加 SAS Token）
    4. 使用 ARIA2 批量下载原始波段
    5. 波段合成（S2: 用户选择的波段，S1: vv+vh→双通道）
    6. S1 GRD 预处理：轨道文件→定标→滤波→地形校正→dB（仅 Sentinel-1）
    7. 使用 SCL 去除云像素（仅 Sentinel-2，需要 SCL 波段）
    8. 多景拼接 + 研究区裁剪
    9. 将 TIF 转换为 ZARR 格式

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
        satellite: 卫星类型（sentinel1 或 sentinel2，默认 sentinel2）
        s1_product: Sentinel-1 产品类型（grd 或 slc，默认 grd，仅 satellite=sentinel1 时生效）
        bands: 要下载的波段列表（仅 Sentinel-2 有效，为 None 时交互式选择）

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

    # 创建任务目录（如果 output_dir 是默认的 data 目录，则使用任务隔离模式）
    use_task_dir = str(output_dir) == str(Path(__file__).resolve().parent.parent / "data")

    if use_task_dir:
        task_dir = _task_manager.create_task_dir(satellite=satellite, s1_product=s1_product)
        task_id = task_dir.name
        print(f"[任务] 创建任务目录: {task_id}")
        print(f"[任务] 任务路径: {task_dir}")
        output_dir = task_dir
    else:
        task_id = None
        print(f"[输出] 使用自定义输出目录: {output_dir}")

    results = {
        "task_id": task_id,
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
    if satellite != "sentinel1":
        print(f"最大云量: {cloud_cover_max}%")
    print(f"最低覆盖率: {min_coverage*100:.0f}%")
    print(f"输出目录: {output_dir}")
    print("=" * 60)

    # ============================================================
    # S1 SLC: CDSE 搜索 + 升降轨选择 + CDSE 下载
    # ============================================================
    if satellite == "sentinel1" and s1_product == "slc":
        print("\n" + "-" * 40)
        print("Step 1/9: CDSE 搜索 S1 SLC IW 产品")
        print("-" * 40)

        step1_start = time.time()
        try:
            search_bbox = list(aoi_geom.bounds) if aoi_geom else bbox
            cdse_products = _cdse_slc_module.search_slc(
                bbox=search_bbox, date_range=date_range, aoi_geom=aoi_geom
            )

            if not cdse_products:
                print("[管线] 未找到 SLC 产品，管线终止")
                results["steps"]["search"] = {"status": "no_results", "count": 0}
                return results

            step1_time = time.time() - step1_start
            results["steps"]["search"] = {
                "status": "success",
                "count": len(cdse_products),
                "time_sec": round(step1_time, 1),
            }
        except Exception as e:
            print(f"[错误] Step 1 失败: {e}")
            results["steps"]["search"] = {"status": "error", "error": str(e)}
            return results

        # 升降轨选择
        print("\n" + "-" * 40)
        print("Step 2/9: 升降轨选择")
        print("-" * 40)

        if auto_select:
            # 自动模式：选择第一个轨道方向的所有场景
            orbit_dirs = list({p["orbit_direction"] for p in cdse_products})
            selected_slc = [p for p in cdse_products if p["orbit_direction"] == orbit_dirs[0]]
            print(f"[自动选择] {orbit_dirs[0]}，共 {len(selected_slc)} 景")
        else:
            selected_slc = _cdse_slc_module.select_orbit_direction_interactive(cdse_products)

        if not selected_slc:
            print("[管线] 未选择任何场景，管线终止")
            results["steps"]["scene_selection"] = {"status": "no_selection"}
            return results

        results["steps"]["scene_selection"] = {
            "status": "success",
            "strategy": "orbit_direction",
            "selected_count": len(selected_slc),
            "total_count": len(cdse_products),
        }

        # CDSE 下载
        print("\n" + "-" * 40)
        print("Step 3-4/9: CDSE 下载 SLC 产品")
        print("-" * 40)

        if not skip_download:
            try:
                slc_dir = output_dir / "downloads" / "s1_slc"
                downloaded_slc = _cdse_slc_module.download_slc(selected_slc, slc_dir)

                if not downloaded_slc:
                    print("[管线] 下载失败，管线终止")
                    results["steps"]["download"] = {"status": "failed"}
                    return results

                # 从 SAFE 目录提取 VV/VH 波段到 downloads/
                download_dir = output_dir / "downloads"
                download_dir.mkdir(parents=True, exist_ok=True)

                # 为每个 SAFE 产品提取波段并写入 metadata
                metadata = {"scenes": [], "generated_at": datetime.now().isoformat(), "satellite": "sentinel1", "s1_product": "slc"}
                for item in downloaded_slc:
                    bands = _cdse_slc_module.extract_bands(item["path"])
                    if bands:
                        scene_meta = {
                            "scene_id": item["scene_id"],
                            "datetime": item.get("date", ""),
                            "date": item.get("date", ""),
                            "cloud_cover": 0,
                            "orbit_direction": item["orbit_direction"],
                            "satellite": "sentinel1",
                            "bands": {},
                        }
                        for pol_name, tif_path in bands.items():
                            scene_meta["bands"][pol_name] = {
                                "url": "",
                                "filename": tif_path.name,
                                "local_path": str(tif_path),
                            }
                        metadata["scenes"].append(scene_meta)

                _search_module.save_metadata_file(metadata, output_dir / "metadata.json")

                results["steps"]["download"] = {
                    "status": "success",
                    "count": len(downloaded_slc),
                }
                results["steps"]["sign_urls"] = {"status": "skipped", "reason": "cdse_slc"}
            except Exception as e:
                print(f"[错误] CDSE 下载失败: {e}")
                results["steps"]["download"] = {"status": "error", "error": str(e)}
                return results
        else:
            print("\n[管线] 跳过下载步骤（--skip-download）")
            results["steps"]["download"] = {"status": "skipped"}

        selected_scene_ids = None  # CDSE 路径不按 scene_ids 过滤

    # ============================================================
    # S1 GRD / S2: MPC STAC 搜索 + ARIA2 下载
    # ============================================================
    else:
        # Step 1: STAC 搜索
        print("\n" + "-" * 40)
        sat_label = "Sentinel-1 GRD" if satellite == "sentinel1" else "Sentinel-2 L2A"
        print(f"Step 1/9: STAC 搜索与覆盖率计算 ({sat_label})")
        print("-" * 40)

        step1_start = time.time()
        try:
            catalog = _search_module.connect_stac_catalog()
            search_bbox = list(aoi_geom.bounds) if aoi_geom else bbox
            if satellite == "sentinel1":
                items = _search_module.search_sentinel1(catalog, search_bbox, date_range, aoi_geom, product=s1_product)
                selected_bands = ["vv", "vh"]
            else:
                items = _search_module.search_sentinel2(catalog, search_bbox, date_range, cloud_cover_max, aoi_geom)
                # 波段选择
                if bands:
                    selected_bands = _search_module.parse_bands_argument(bands, satellite)
                else:
                    selected_bands = _search_module.select_bands_interactive(satellite)

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

        # Step 2: 场景选择
        print("\n" + "-" * 40)
        print("Step 2/9: 场景选择")
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

            selected_scene_ids = {item.id for item in selected_items}
        except Exception as e:
            print(f"[错误] Step 2 失败: {e}")
            results["steps"]["scene_selection"] = {"status": "error", "error": str(e)}
            return results

        # Step 3: URL 签名
        print("\n" + "-" * 40)
        print("Step 3/9: URL 签名")
        print("-" * 40)

        step3_start = time.time()
        try:
            result = _search_module.extract_signed_urls(selected_items, output_dir / "downloads", aoi_geom, satellite=satellite, selected_bands=selected_bands)
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

        # Step 4: ARIA2 下载
        if not skip_download:
            print("\n" + "-" * 40)
            print("Step 4/9: ARIA2 批量下载")
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
    print("Step 5/9: 波段合成")
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
    # Step 6: S1 GRD 预处理（snappy）
    # ============================================================
    if satellite == "sentinel1" and s1_product == "grd":
        print("\n" + "-" * 40)
        print("Step 6/9: S1 GRD 预处理（轨道文件→定标→滤波→地形校正→dB）")
        print("-" * 40)

        step6_start = time.time()
        try:
            s1_processed = _s1_preprocess_module.preprocess_s1_scenes(output_dir, scene_ids=selected_scene_ids)

            step6_time = time.time() - step6_start
            results["steps"]["s1_preprocess"] = {
                "status": "success" if s1_processed else "skipped",
                "count": len(s1_processed),
                "time_sec": round(step6_time, 1),
            }
        except Exception as e:
            print(f"[错误] Step 6 失败: {e}")
            results["steps"]["s1_preprocess"] = {"status": "error", "error": str(e)}
            return results
    else:
        skip_reason = "sentinel2" if satellite != "sentinel1" else "s1_slc"
        results["steps"]["s1_preprocess"] = {"status": "skipped", "reason": skip_reason}

    # ============================================================
    # Step 7: SCL 去云处理
    # ============================================================
    if satellite == "sentinel1":
        print("\n" + "-" * 40)
        print("Step 7/9: SCL 去云处理（SAR 数据跳过）")
        print("-" * 40)
        results["steps"]["cloud_mask"] = {"status": "skipped", "reason": "sentinel1"}
    else:
        print("\n" + "-" * 40)
        print("Step 7/9: SCL 去云处理")
        print("-" * 40)

        step6_start = time.time()
        try:
            masked_files = _cloud_mask_module.process_all_merged_scenes(output_dir, scene_ids=selected_scene_ids)

            step7_time = time.time() - step7_start
            results["steps"]["cloud_mask"] = {
                "status": "success",
                "count": len(masked_files),
                "time_sec": round(step7_time, 1),
            }
        except Exception as e:
            print(f"[错误] Step 7 失败: {e}")
            results["steps"]["cloud_mask"] = {"status": "error", "error": str(e)}
            return results

    # ============================================================
    # Step 8: 多景拼接 + 研究区裁剪
    # ============================================================
    # Step 8: 拼接 + 裁剪（SLC 数据跳过）
    # ============================================================
    print("\n" + "-" * 40)
    print("Step 8/9: 拼接 + 裁剪")
    print("-" * 40)

    # SLC 数据跳过裁剪（斜距坐标，无地理坐标）
    if satellite == "sentinel1" and s1_product == "slc":
        print("[跳过] SLC 数据跳过裁剪步骤")
        print("[提示] SLC 数据用于 InSAR 分析，InSAR 输出会有正确的地理坐标")
        results["steps"]["mosaic_clip"] = {"status": "skipped", "reason": "s1_slc"}
    else:
        step8_start = time.time()
        try:
            mosaicked_paths = _mosaic_clip_module.process_mosaic_clip(output_dir, min_coverage=min_coverage, scene_ids=selected_scene_ids)

            step8_time = time.time() - step8_start
            results["steps"]["mosaic_clip"] = {
                "status": "success" if mosaicked_paths else "skipped",
                "output_paths": [str(p) for p in mosaicked_paths] if mosaicked_paths else [],
                "count": len(mosaicked_paths),
                "time_sec": round(step8_time, 1),
            }
        except Exception as e:
            print(f"[错误] Step 8 失败: {e}")
            results["steps"]["mosaic_clip"] = {"status": "error", "error": str(e)}
            return results

    # ============================================================
    # Step 9: TIF → ZARR 转换（SLC 数据跳过）
    # ============================================================
    print("\n" + "-" * 40)
    print("Step 9/9: TIF → ZARR 转换")
    print("-" * 40)

    # SLC 数据跳过 ZARR 转换（没有裁剪后的 TIF）
    if satellite == "sentinel1" and s1_product == "slc":
        print("[跳过] SLC 数据跳过 ZARR 转换")
        results["steps"]["zarr_convert"] = {"status": "skipped", "reason": "s1_slc"}
    else:
        step9_start = time.time()
        try:
            zarr_paths = _zarr_module.convert_all_to_zarr(output_dir)

            step9_time = time.time() - step9_start
            results["steps"]["zarr_convert"] = {
                "status": "success",
                "count": len(zarr_paths),
                "time_sec": round(step9_time, 1),
            }
        except Exception as e:
            print(f"[错误] Step 9 失败: {e}")
            results["steps"]["zarr_convert"] = {"status": "error", "error": str(e)}
            return results

    # ============================================================
    # 管线完成
    # ============================================================
    total_time = time.time() - start_time
    results["total_time_sec"] = round(total_time, 1)

    # 更新任务状态（仅在使用任务目录时）
    if task_id:
        _task_manager.update_task_info(task_id, {
            "status": "completed",
            "completed_at": datetime.now().isoformat(),
            "total_time_sec": results["total_time_sec"],
            "results": results,
        })

    print("\n" + "=" * 60)
    print("管线执行完成！")
    print("=" * 60)
    if task_id:
        print(f"任务ID: {task_id}")
    print(f"总耗时: {total_time:.1f} 秒")
    print(f"\n输出文件位置:")
    print(f"  输出目录: {output_dir}")
    print(f"  原始波段: {output_dir / 'downloads'}")
    print(f"  合成 TIF: {output_dir / 'merged'}")
    if satellite != "sentinel1" or s1_product != "slc":
        print(f"  去云 TIF: {output_dir / 'cloud_masked'}")
        print(f"  拼接裁剪: {output_dir / 'mosaicked'}")
        print(f"  ZARR 数据: {output_dir / 'zarr'}")
    print(f"  元数据: {output_dir / 'metadata.json'}")
    print(f"  研究区几何: {output_dir / 'aoi_geometry.json'}")
    print("=" * 60)

    # SLC 数据提示 InSAR 分析
    if satellite == "sentinel1" and s1_product == "slc":
        print("\n" + "=" * 60)
        print("SLC 数据已下载完成！")
        print("=" * 60)
        print("SLC 数据用于 InSAR 形变监测，请执行以下命令进行分析：")
        print(f"  python scripts/09_insar_analysis.py --data-dir {output_dir} --polarization vv")
        print("\nInSAR 分析将输出：")
        print("  - 干涉相位图（*_phase.tif）")
        print("  - 相干性图（*_coherence.tif）")
        print("  - 形变图（*_deformation.tif）")
        print("  - 分析报告（*_report.json）")
        print("=" * 60)

    if task_id:
        print(f"\n查看任务详情: python scripts/task_manager.py info {task_id}")
        print(f"列出所有任务: python scripts/task_manager.py list")

    return results


def main():
    """主函数：解析参数并执行管线。"""
    parser = argparse.ArgumentParser(
        description="RS-Platform 遥感影像处理管线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 使用默认参数（北京市区域）- 自动创建任务目录
  python scripts/06_pipeline.py

  # 使用 bbox
  python scripts/06_pipeline.py --bbox 116.0 39.0 117.0 40.0

  # 使用 shp 文件
  python scripts/06_pipeline.py --aoi ./data/beijing_boundary.shp

  # 使用行政区划 adcode
  python scripts/06_pipeline.py --adcode 110000

  # 使用行政区划名称（模糊搜索）
  python scripts/06_pipeline.py --admin-name "北京市"

  # 指定自定义输出目录（不使用任务隔离）
  python scripts/06_pipeline.py --bbox 116.0 39.0 117.0 40.0 --output ./my_output

  # 自动选择最优时相（跳过交互选择）
  python scripts/06_pipeline.py --auto-select

  # S2 波段选择
  python scripts/06_pipeline.py --bbox 116.0 39.0 117.0 40.0 --bands false_color

  # 下载 Sentinel-1 GRD 影像
  python scripts/06_pipeline.py --satellite sentinel1 --bbox 116.0 39.0 117.0 40.0

  # 下载 Sentinel-1 SLC 影像
  python scripts/06_pipeline.py --satellite sentinel1 --s1-product slc --bbox 116.0 39.0 117.0 40.0

任务管理:
  python scripts/task_manager.py list           # 列出所有任务
  python scripts/task_manager.py info TASK_ID   # 查看任务详情
  python scripts/task_manager.py cleanup --keep 5  # 清理旧任务
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
    parser.add_argument(
        "--satellite",
        type=str,
        default="sentinel2",
        choices=["sentinel1", "sentinel2"],
        help="卫星类型: sentinel1 (SAR) 或 sentinel2 (光学，默认)",
    )
    parser.add_argument(
        "--s1-product",
        type=str,
        default="grd",
        choices=["grd", "slc"],
        help="Sentinel-1 产品类型: grd 或 slc，仅 satellite=sentinel1 时生效",
    )
    parser.add_argument(
        "--bands",
        type=str,
        nargs="+",
        default=None,
        help="要下载的波段列表（如 B02 B03 B04 SCL）或预设名称（如 rgb_scl）。仅 Sentinel-2 有效",
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
        satellite=args.satellite,
        s1_product=args.s1_product,
        bands=args.bands,
    )


if __name__ == "__main__":
    main()
