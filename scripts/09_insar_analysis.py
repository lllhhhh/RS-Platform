"""
09_insar_analysis.py - InSAR 形变监测模块

功能：
使用 ESA SNAP / esa_snappy 对两幅 Sentinel-1 SLC 影像执行 InSAR 处理流程，
生成形变图、相干性图和形变分析报告。

InSAR 处理链：
1. Apply Orbit File — 对主/从影像应用精密轨道文件
2. Back-Geocoding — 将从影像配准到主影像
3. Interferogram Formation — 计算干涉相位
4. Topographic Phase Removal — 去除地形相位分量
5. Goldstein Phase Filtering — 相位滤波降噪
6. Terrain-Correction — 地理编码输出

使用方法：
    # 交互式选择主从影像
    python scripts/09_insar_analysis.py --data-dir ./data --polarization vv

    # 直接指定主从影像
    python scripts/09_insar_analysis.py --master path/master.tif --slave path/slave.tif --polarization vv
"""

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

# JVM 堆内存设为 28GB（系统 31.5GB，留 3.5GB 给 OS）
os.environ["JAVA_TOOL_OPTIONS"] = "-Xmx28g"


def _ensure_dem_files():
    """检查并预下载所需的 SRTM DEM 文件。"""
    import requests

    dem_dir = Path.home() / ".snap" / "auxdata" / "dem" / "SRTM90" / "tiff"
    dem_dir.mkdir(parents=True, exist_ok=True)

    required = ["srtm_59_07.zip", "srtm_60_07.zip", "srtm_60_08.zip"]
    base_url = "https://download.esa.int/step/auxdata/dem/SRTM90/tiff"

    for fname in required:
        fpath = dem_dir / fname
        if fpath.exists() and fpath.stat().st_size > 1000:
            continue
        print(f"  [DEM] 下载 {fname}...")
        try:
            r = requests.get(f"{base_url}/{fname}", timeout=120, stream=True)
            if r.status_code == 200:
                with open(fpath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                print(f"  [DEM] {fname} 下载完成 ({fpath.stat().st_size // (1024*1024)} MB)")
            else:
                print(f"  [DEM] {fname} 下载失败: HTTP {r.status_code}")
        except Exception as e:
            print(f"  [DEM] {fname} 下载失败: {e}")


def _ensure_java_home() -> bool:
    """自动检测并设置 JAVA_HOME（ESA SNAP JRE）。"""
    if os.environ.get("JAVA_HOME"):
        return True

    candidates = [
        r"D:\esa-snap\jre",
        r"C:\esa-snap\jre",
        r"/usr/local/snap/jre",
        r"/opt/snap/jre",
    ]
    for path in candidates:
        if os.path.isdir(path):
            os.environ["JAVA_HOME"] = path
            print(f"[InSAR] 自动设置 JAVA_HOME={path}")
            return True

    return False


def list_slc_scenes(data_dir: Path) -> list:
    """
    列出可用的 SLC 影像。

    优先查找 SAFE 目录（InSAR 需要完整元数据），
    其次查找 merged 目录的 TIF，最后查找 mosaicked 目录。

    Args:
        data_dir: 数据目录

    Returns:
        list: SLC 影像路径列表
    """
    # 优先从 SAFE 目录查找（InSAR 需要完整 SAR 元数据）
    s1_slc_dir = data_dir / "downloads" / "s1_slc"
    if s1_slc_dir.exists():
        safe_dirs = sorted(s1_slc_dir.glob("*.SAFE"))
        if safe_dirs:
            return safe_dirs

    # 其次从 merged 目录查找 SLC 合成影像
    merged_dir = data_dir / "merged"
    if merged_dir.exists():
        slc_files = sorted(merged_dir.glob("*_S1_merged.tif"))
        if slc_files:
            return slc_files

    # 兼容 mosaicked 目录的裁剪影像
    mosaicked_dir = data_dir / "mosaicked"
    if mosaicked_dir.exists():
        slc_files = sorted(mosaicked_dir.glob("*_clipped.tif"))
        if slc_files:
            return slc_files

    return []


def _get_safe_display_name(safe_path: Path) -> str:
    """从 SAFE 路径提取简洁的显示名称。"""
    name = safe_path.name
    if name.endswith(".SAFE"):
        name = name[:-5]
    return name


def select_scenes_interactive(slc_files: list) -> tuple:
    """
    交互式选择主影像和从影像。

    Args:
        slc_files: 可用 SLC 影像路径列表

    Returns:
        tuple: (master_path, slave_path)
    """
    print("\n" + "=" * 60)
    print("可用 SLC 影像列表")
    print("=" * 60)

    for i, f in enumerate(slc_files):
        print(f"  {i + 1}. {f.name if f.suffix == '.tif' else _get_safe_display_name(f)}")

    print("-" * 60)
    print("请选择两幅影像进行 InSAR 分析（先选主影像，再选从影像）")
    print("提示: 主影像应为时间较早的影像")

    while True:
        try:
            master_input = input("\n请输入主影像序号: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return None, None

        try:
            master_idx = int(master_input) - 1
            if 0 <= master_idx < len(slc_files):
                break
            print(f"序号超出范围，请输入 1~{len(slc_files)}")
        except ValueError:
            print("请输入数字序号")

    while True:
        try:
            slave_input = input("请输入从影像序号: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return None, None

        try:
            slave_idx = int(slave_input) - 1
            if 0 <= slave_idx < len(slc_files):
                if slave_idx == master_idx:
                    print("主从影像不能相同，请重新选择")
                    continue
                break
            print(f"序号超出范围，请输入 1~{len(slc_files)}")
        except ValueError:
            print("请输入数字序号")

    master_path = slc_files[master_idx]
    slave_path = slc_files[slave_idx]

    print(f"\n主影像: {master_path.name}")
    print(f"从影像: {slave_path.name}")

    return master_path, slave_path


def run_insar(
    master_path: Path,
    slave_path: Path,
    polarization: str = "vv",
    output_dir: Path = None,
) -> dict:
    """
    执行 InSAR 处理流程。

    snappy 处理链：
    Read → Apply-Orbit-File → Back-Geocoding → Interferogram-Formation →
    TopoPhaseRemoval → Goldstein-Filter → Terrain-Correction → 导出

    Args:
        master_path: 主影像路径
        slave_path: 从影像路径
        polarization: 极化通道（"vv" 或 "vh"）
        output_dir: 输出目录

    Returns:
        dict: 处理结果信息
    """
    if not _ensure_java_home():
        print("[错误] 未找到 JAVA_HOME，esa_snappy 需要 JVM 支持")
        print("  请设置环境变量: set JAVA_HOME=D:\\esa-snap\\jre")
        print("  或安装 ESA SNAP Desktop: https://step.esa.int/")
        sys.exit(1)

    from esa_snappy import ProductIO, GPF, HashMap

    if output_dir is None:
        output_dir = master_path.parent.parent / "insar"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 判断是否为 SAFE 目录
    is_safe = master_path.suffix.upper() == ".SAFE" or (master_path.is_dir() and master_path.name.upper().endswith(".SAFE"))

    # 生成输出文件名前缀
    if is_safe:
        master_stem = master_path.name.replace(".SAFE", "").replace(".safe", "")
        slave_stem = slave_path.name.replace(".SAFE", "").replace(".safe", "")
    else:
        master_stem = master_path.stem.replace("_clipped", "").replace("_S1_merged", "")
        slave_stem = slave_path.stem.replace("_clipped", "").replace("_S1_merged", "")
    # 截取简短名称（取日期部分）
    master_short = master_stem.split("_T")[0] if "_T" in master_stem else master_stem[:50]
    slave_short = slave_stem.split("_T")[0] if "_T" in slave_stem else slave_stem[:50]
    prefix = f"ifg_{master_short}_vs_{slave_short}"

    print(f"\n{'=' * 60}")
    print(f"InSAR 处理开始")
    print(f"{'=' * 60}")
    print(f"主影像: {master_path.name}")
    print(f"从影像: {slave_path.name}")
    print(f"极化通道: {polarization.upper()}")
    print(f"输出目录: {output_dir}")
    print(f"{'=' * 60}")

    # ========== Step 0: 确保轨道文件可用 ==========
    print("\n[InSAR] Step 1/10: 检查轨道文件...")
    from scripts.orbit_downloader import ensure_orbit_files
    import re

    # 从 SAFE 目录名提取日期
    def _extract_date_from_safe(safe_path):
        name = safe_path.name if safe_path.is_dir() else safe_path.stem
        match = re.search(r'(\d{8})T\d{6}', name)
        if match:
            d = match.group(1)
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return None

    master_date = _extract_date_from_safe(master_path)
    slave_date = _extract_date_from_safe(slave_path)

    dates_to_fetch = []
    if master_date:
        dates_to_fetch.append(master_date)
    if slave_date and slave_date != master_date:
        dates_to_fetch.append(slave_date)

    if dates_to_fetch:
        orbit_results = ensure_orbit_files(dates_to_fetch, platform="S1A")
        if orbit_results:
            print(f"  轨道文件就绪: {list(orbit_results.keys())}")
        else:
            print("  [警告] 未能获取轨道文件，InSAR 精度可能受影响")

    # ========== Step 1: 读取主从影像 ==========
    print("\n[InSAR] Step 2/10: 读取主从影像...")
    if is_safe:
        # SAFE 格式：读取 manifest.safe
        manifest_master = master_path / "manifest.safe"
        manifest_slave = slave_path / "manifest.safe"
        print(f"  读取 SAFE 格式: {manifest_master}")
        master_product = ProductIO.readProduct(str(manifest_master))
        slave_product = ProductIO.readProduct(str(manifest_slave))
    else:
        master_product = ProductIO.readProduct(str(master_path))
        slave_product = ProductIO.readProduct(str(slave_path))

    # 显示波段信息
    master_bands = list(master_product.getBandNames())
    slave_bands = list(slave_product.getBandNames())
    print(f"  主影像波段数: {len(master_bands)}")
    print(f"  从影像波段数: {len(slave_bands)}")

    # 显示匹配的极化通道波段
    pol_upper = polarization.upper()
    matched_bands = [b for b in master_bands if pol_upper in b.upper()]
    if matched_bands:
        print(f"  匹配 {pol_upper} 极化波段: {matched_bands[:6]}...")

    # ========== Step 2: 应用轨道文件 ==========
    print("[InSAR] Step 3/10: 应用轨道文件...")
    orbit_params = HashMap()
    orbit_params.put("orbitType", "Sentinel Precise (Auto Download)")
    orbit_params.put("polyDegree", "3")

    orbit_success = False
    try:
        master_product = GPF.createProduct(
            "Apply-Orbit-File", orbit_params, master_product
        )
        print("  主影像轨道文件应用成功")
        orbit_success = True
    except Exception as e:
        print(f"  主影像轨道文件跳过: {e.__class__.__name__}")

    try:
        slave_product = GPF.createProduct(
            "Apply-Orbit-File", orbit_params, slave_product
        )
        print("  从影像轨道文件应用成功")
    except Exception as e:
        print(f"  从影像轨道文件跳过: {e.__class__.__name__}")

    if not orbit_success:
        print("\n  [警告] 轨道文件未能下载，InSAR 精度可能受影响")
        print("  [提示] 可手动下载轨道文件到: C:\\Users\\<用户名>\\.snap\\auxdata\\Orbits\\Sentinel-1\\")
        print("  [提示] 下载地址: https://s1qc.asf.alaska.edu/aux_poeorb/")

    # ========== Step 3: TOPS Split（选择子条带）==========
    subswath = "IW2"
    pol_upper = polarization.upper()
    print(f"[InSAR] Step 4/10: TOPS Split（子条带: {subswath}, 极化: {pol_upper}）...")

    split_params = HashMap()
    split_params.put("subswath", subswath)
    split_params.put("polarization", pol_upper)
    split_params.put("selectedPolarisations", pol_upper)

    master_split = GPF.createProduct("TOPSAR-Split", split_params, master_product)
    slave_split = GPF.createProduct("TOPSAR-Split", split_params, slave_product)
    print(f"  主影像 Split 波段: {list(master_split.getBandNames())}")

    # ========== Step 4: Back-Geocoding + 干涉图 ==========
    print("[InSAR] Step 5/10: Back-Geocoding + 干涉图生成...")

    bg_params = HashMap()
    bg_params.put("demName", "SRTM 3Sec")
    bg_params.put("demResamplingMethod", "BILINEAR_INTERPOLATION")
    bg_params.put("maskOutAreaWithoutElevation", "true")

    source_products = HashMap()
    source_products.put("master", master_split)
    source_products.put("slave", slave_split)
    coregistered = GPF.createProduct("Back-Geocoding", bg_params, source_products)

    # 诊断: 检查配准后从影像是否有有效数据
    import numpy as np
    coreg_bands = list(coregistered.getBandNames())
    slave_bands = [b for b in coreg_bands if 'slv' in b.lower()]
    if slave_bands:
        band = coregistered.getBand(slave_bands[0])
        w = min(1000, band.getRasterWidth())
        h = min(1000, band.getRasterHeight())
        x0 = (band.getRasterWidth() - w) // 2
        y0 = (band.getRasterHeight() - h) // 2
        data = np.zeros(w * h, dtype=np.float32)
        band.readPixels(x0, y0, w, h, data)
        valid_count = int(np.count_nonzero(data))
        if valid_count == 0:
            print("  [警告] 配准后从影像数据全为零！两幅影像可能不重叠")
            print("  [提示] 请选择同一轨道位置的影像对")
        else:
            print(f"  配准验证: 从影像有效像素 {valid_count}/{w*h}")

    ifg_params = HashMap()
    ifg_params.put("subtractFlatEarthPhase", "true")
    ifg_params.put("srpPolynomialDegree", "5")
    ifg_params.put("srpNumberPoints", "501")
    ifg_params.put("orbitDegree", "3")
    ifg_params.put("includeCoherence", "true")
    interferogram = GPF.createProduct("Interferogram", ifg_params, coregistered)

    # ========== Step 5: TOPS Deburst ==========
    print("[InSAR] Step 6/10: TOPS Deburst...")
    deburst_params = HashMap()
    deburst_params.put("selectedPolarisations", pol_upper)
    deburst = GPF.createProduct("TOPSAR-Deburst", deburst_params, interferogram)
    print("  Deburst 完成")

    # ========== Step 6: Goldstein 相位滤波（可选）==========
    print("[InSAR] Step 7/10: Goldstein 相位滤波...")
    filtered = deburst
    filter_params = HashMap()
    filter_params.put("alpha", "0.5")
    filter_params.put("FFTSizeString", "64")
    filter_params.put("windowSizeString", "3")
    filter_params.put("useCoherenceMask", "false")

    for op_name in ["GoldsteinPhaseFiltering", "GoldsteinPhaseFilter", "GoldsteinFilter"]:
        try:
            filtered = GPF.createProduct(op_name, filter_params, deburst)
            print(f"  Goldstein 滤波完成 (算子: {op_name})")
            break
        except Exception:
            continue
    else:
        print("  [警告] Goldstein 滤波不可用")
        print("  [提示] 请通过 SNAP Desktop → Tools → Plugin Manager 更新 S1-InSAR 插件")

    # ========== Step 7: 相位解缠（可选，需外部 snaphu）==========
    print("[InSAR] Step 8/10: 相位解缠...")
    import shutil
    import subprocess

    # 检测 snaphu 路径
    snaphu_cmd = shutil.which("snaphu") or shutil.which("snaphu.exe")
    if not snaphu_cmd:
        # 检查 SNAP 附带的 snaphu
        snap_snaphu = Path(r"D:\esa-snap\snaphu-v2.0.4_win64\bin\snaphu.exe")
        if snap_snaphu.exists():
            snaphu_cmd = str(snap_snaphu)
    unwrapped = filtered  # 默认使用滤波后的结果（无解缠）

    if snaphu_cmd:
        try:
            snaphu_dir = output_dir / "snaphu"
            snaphu_dir.mkdir(parents=True, exist_ok=True)

            # SnaphuExport
            export_params = HashMap()
            export_params.put("targetFolder", str(snaphu_dir))
            export_params.put("statCostMode", "DEFO")
            export_params.put("initMethod", "MST")
            export_params.put("numberOfTileRows", "1")
            export_params.put("numberOfTileCols", "1")
            GPF.createProduct("SnaphuExport", export_params, filtered)

            config_files = list(snaphu_dir.glob("*snaphu.conf"))
            if config_files:
                print(f"  运行 SNAPHU: {snaphu_cmd}")
                proc = subprocess.run(
                    [snaphu_cmd, "-f", str(config_files[0])],
                    capture_output=True, text=True, timeout=1800,
                )
                if proc.returncode == 0:
                    import_params = HashMap()
                    import_params.put("targetFolder", str(snaphu_dir))
                    unwrapped = GPF.createProduct("SnaphuImport", import_params, filtered)
                    print("  相位解缠完成")
                else:
                    print(f"  [警告] SNAPHU 失败: {proc.stderr[:200]}")
        except Exception as e:
            print(f"  [警告] 相位解缠失败: {e.__class__.__name__}: {e}")
    else:
        print("  [提示] snaphu 未安装，跳过相位解缠（使用缠绕相位）")

    # ========== Step 8: 地形相位去除（Topographic Phase Removal）==========
    print("[InSAR] Step 9/10: 地形相位去除（DEM）...")
    _ensure_dem_files()
    try:
        topo_params = HashMap()
        topo_params.put("orbitDegree", "3")
        topo_params.put("demName", "SRTM 3Sec")
        topo_params.put("demResamplingMethod", "BILINEAR_INTERPOLATION")
        unwrapped = GPF.createProduct("TopoPhaseRemoval", topo_params, unwrapped)
        print("  地形相位去除完成（SRTM 3Sec DEM）")
    except Exception as e:
        print(f"  [警告] 地形相位去除失败: {e.__class__.__name__}: {e}")
        print("  [提示] 形变结果可能包含地形相位分量")

    # ========== Step 9: 地形校正（地理编码）==========
    print("[InSAR] Step 10/10: 地形校正（Terrain-Correction）...")
    try:
        tc_params = HashMap()
        tc_params.put("demName", "SRTM 3Sec")
        tc_params.put("pixelSpacingInMeter", "100.0")
        result = GPF.createProduct("Terrain-Correction", tc_params, unwrapped)
        print("  地形校正完成")
    except Exception as e:
        print(f"  [警告] 地形校正失败: {e.__class__.__name__}: {e}")
        print("  [提示] 将输出斜距坐标系的干涉结果")
        result = unwrapped

    # 记录相位波段名（TC 后可能丢失，需要从 TC 前的产品获取）
    unwrapped_bands = list(unwrapped.getBandNames())
    phase_band_name = None
    for pattern in ["unwrapped_phase", "unwrapped", "phase", "ifg"]:
        for b in unwrapped_bands:
            if pattern in b.lower():
                phase_band_name = b
                break
        if phase_band_name:
            break
    print(f"  相位波段: {phase_band_name}")

    print("  InSAR 处理完成")

    # ========== 导出 GeoTIFF ==========
    print("\n[InSAR] 导出 GeoTIFF...")
    import numpy as np

    output_prefix = output_dir / prefix
    output_files = {}

    # 1. 导出相干性图（从 TC 后的产品）
    result_bands = list(result.getBandNames())
    print(f"  TC 后波段: {result_bands}")

    coherence_bands = [b for b in result_bands if "coh" in b.lower() or "coherence" in b.lower()]
    if coherence_bands:
        coh_path = output_dir / f"{prefix}_coherence.tif"
        coh_params = HashMap()
        coh_params.put("sourceBands", coherence_bands[0])
        coh_product = GPF.createProduct("Subset", coh_params, result)
        ProductIO.writeProduct(coh_product, str(coh_path), "GeoTIFF")
        output_files["coherence"] = str(coh_path)
        size_mb = coh_path.stat().st_size / (1024 * 1024)
        print(f"  相干性图: {coh_path.name} ({size_mb:.1f} MB)")

    # 2. 导出相位图
    phase_bands = [b for b in result_bands if "phase" in b.lower()]
    if phase_bands:
        phase_path = output_dir / f"{prefix}_phase.tif"
        phase_params = HashMap()
        phase_params.put("sourceBands", phase_bands[0])
        phase_product = GPF.createProduct("Subset", phase_params, result)
        ProductIO.writeProduct(phase_product, str(phase_path), "GeoTIFF")
        output_files["phase"] = str(phase_path)
        size_mb = phase_path.stat().st_size / (1024 * 1024)
        print(f"  相位图: {phase_path.name} ({size_mb:.1f} MB)")

    # 3. 导出解缠相位图
    unw_bands = [b for b in result_bands if "unw" in b.lower()]
    if unw_bands:
        unw_path = output_dir / f"{prefix}_unwrapped_phase.tif"
        unw_params = HashMap()
        unw_params.put("sourceBands", unw_bands[0])
        unw_product = GPF.createProduct("Subset", unw_params, result)
        ProductIO.writeProduct(unw_product, str(unw_path), "GeoTIFF")
        output_files["unwrapped_phase"] = str(unw_path)
        size_mb = unw_path.stat().st_size / (1024 * 1024)
        print(f"  解缠相位图: {unw_path.name} ({size_mb:.1f} MB)")

    # 4. 形变图（mm）— 直接用 SNAP API 分块读取相位，numpy 计算形变
    WAVELENGTH_M = 0.055465763

    if phase_band_name:
        try:
            import rasterio
            from rasterio.windows import Window

            # 用 SNAP API 分块读取相位数据并计算形变
            phase_band_obj = unwrapped.getBand(phase_band_name)
            pw, ph = phase_band_obj.getRasterWidth(), phase_band_obj.getRasterHeight()
            print(f"  相位波段尺寸: {pw}x{ph}")

            # 用相位波段尺寸创建输出文件（无地理参考，后续重投影）
            disp_meta = {
                "driver": "GTiff",
                "dtype": "float32",
                "width": pw,
                "height": ph,
                "count": 1,
                "nodata": float("nan"),
            }

            disp_path = output_dir / f"{prefix}_deformation_mm.tif"
            with rasterio.open(str(disp_path), "w", **disp_meta) as dst:
                chunk_size = 512
                for ji in range(0, ph, chunk_size):
                    for jj in range(0, pw, chunk_size):
                        h = min(chunk_size, ph - ji)
                        w = min(chunk_size, pw - jj)

                        # SNAP API 分块读取
                        chunk = np.zeros(w * h, dtype=np.float32)
                        phase_band_obj.readPixels(jj, ji, w, h, chunk)
                        chunk = chunk.reshape(h, w)

                        valid = ~np.isnan(chunk) & (chunk != 0)
                        disp_chunk = np.full((h, w), np.nan, dtype=np.float32)
                        disp_chunk[valid] = -chunk[valid] * WAVELENGTH_M / (4.0 * np.pi) * 1000.0

                        window = Window(col_off=jj, row_off=ji, width=w, height=h)
                        dst.write(disp_chunk, 1, window=window)

                    # 进度
                    pct = min(100, int((ji + chunk_size) / ph * 100))
                    print(f"\r  形变计算进度: {pct}%", end="", flush=True)
                print()

            output_files["deformation_mm"] = str(disp_path)
            size_mb = disp_path.stat().st_size / (1024 * 1024)
            print(f"  形变图: {disp_path.name} ({size_mb:.1f} MB)")
        except Exception as e:
            print(f"  [警告] 形变图计算失败: {e.__class__.__name__}: {e}")

    # ========== 计算统计信息 ==========
    print("\n[InSAR] 计算形变分析统计...")
    stats = compute_insar_stats(result, result_bands, output_files)

    # 保存处理参数
    stats["processing_info"] = {
        "master": master_path.name,
        "slave": slave_path.name,
        "polarization": polarization,
        "dem": "SRTM 3Sec",
        "processed_at": datetime.now().isoformat(),
    }

    # 导出报告
    report_path = output_dir / f"{prefix}_report.json"
    export_report(stats, report_path)

    # 重投影到 EPSG:4326（SNAP TC 对 TOPS SLC 的 EPSG:4326 有 bug）
    try:
        import rasterio
        from rasterio.warp import calculate_default_transform, reproject, Resampling
        print("\n[InSAR] 重投影到 EPSG:4326...")
        for tif_path in output_dir.glob(f"{prefix}*.tif"):
            if "_epsg4326" in tif_path.name:
                continue
            try:
                with rasterio.open(tif_path) as src:
                    if src.crs and src.crs.to_epsg() == 4326:
                        continue  # 已经是 EPSG:4326
                    transform, width, height = calculate_default_transform(
                        src.crs, "EPSG:4326", src.width, src.height, *src.bounds
                    )
                    kwargs = src.meta.copy()
                    kwargs.update(crs="EPSG:4326", transform=transform, width=width, height=height)
                    out_path = tif_path.parent / f"{tif_path.stem}_4326.tif"
                    with rasterio.open(out_path, "w", **kwargs) as dst:
                        for i in range(1, src.count + 1):
                            reproject(
                                source=rasterio.band(src, i),
                                destination=rasterio.band(dst, i),
                                src_transform=src.transform,
                                src_crs=src.crs,
                                dst_transform=transform,
                                dst_crs="EPSG:4326",
                                resampling=Resampling.nearest,
                            )
                tif_path.unlink()
                out_path.rename(tif_path)
                print(f"  重投影: {tif_path.name}")
            except Exception as e:
                print(f"  [提示] {tif_path.name} 重投影失败: {e}")
    except Exception as e:
        print(f"  [提示] 重投影跳过: {e}")

    # 清理
    master_product.dispose()
    slave_product.dispose()
    result.dispose()

    print(f"\n{'=' * 60}")
    print(f"InSAR 处理完成！")
    print(f"{'=' * 60}")
    print(f"输出目录: {output_dir}")
    for name, path in output_files.items():
        print(f"  {name}: {Path(path).name}")
    print(f"  报告: {report_path.name}")

    return {"output_dir": str(output_dir), "files": output_files, "report": str(report_path)}


def compute_insar_stats(result_product, band_names: list, output_files: dict) -> dict:
    """
    计算 InSAR 结果的统计信息。

    Args:
        result_product: snappy 处理结果产品
        band_names: 波段名称列表
        output_files: 输出文件路径字典

    Returns:
        dict: 统计信息
    """
    import rasterio

    stats = {
        "deformation": {},
        "coherence": {},
    }

    # 从输出文件读取并计算统计
    if "deformation" in output_files:
        try:
            with rasterio.open(output_files["deformation"]) as src:
                data = src.read(1)
                valid = data[~np.isnan(data) & (data != 0)]
                if valid.size > 0:
                    stats["deformation"] = {
                        "mean_m": float(np.mean(valid)),
                        "std_m": float(np.std(valid)),
                        "max_uplift_m": float(np.max(valid)),
                        "max_subsidence_m": float(np.min(valid)),
                        "median_m": float(np.median(valid)),
                        "valid_pixels": int(valid.size),
                        "total_pixels": int(data.size),
                    }
        except Exception as e:
            print(f"  [警告] 形变统计计算失败: {e}")

    if "coherence" in output_files:
        try:
            with rasterio.open(output_files["coherence"]) as src:
                data = src.read(1)
                valid = data[~np.isnan(data) & (data != 0)]
                if valid.size > 0:
                    low_coherence = valid[valid < 0.3]
                    stats["coherence"] = {
                        "mean": float(np.mean(valid)),
                        "std": float(np.std(valid)),
                        "low_coherence_ratio": float(len(low_coherence) / valid.size),
                        "valid_pixels": int(valid.size),
                    }
        except Exception as e:
            print(f"  [警告] 相干性统计计算失败: {e}")

    # 如果没有形变波段，尝试从完整产品中读取相位并估算
    if not stats["deformation"] and "phase" in output_files:
        try:
            with rasterio.open(output_files["phase"]) as src:
                data = src.read(1)
                valid = data[~np.isnan(data) & (data != 0)]
                if valid.size > 0:
                    wavelength = 0.055465763  # Sentinel-1 C 波段波长（米）
                    deformation = -wavelength / (4 * np.pi) * valid
                    stats["deformation"] = {
                        "mean_m": float(np.mean(deformation)),
                        "std_m": float(np.std(deformation)),
                        "max_uplift_m": float(np.max(deformation)),
                        "max_subsidence_m": float(np.min(deformation)),
                        "median_m": float(np.median(deformation)),
                        "valid_pixels": int(valid.size),
                        "note": "从干涉相位估算（C 波段波长 5.55cm）",
                    }
        except Exception as e:
            print(f"  [警告] 相位转形变计算失败: {e}")

    return stats


def export_report(stats: dict, output_path: Path) -> None:
    """
    导出 InSAR 分析报告（JSON 格式）。

    Args:
        stats: 统计信息字典
        output_path: 输出文件路径
    """
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"  分析报告: {output_path.name}")


def main():
    """主函数：解析参数并执行 InSAR 分析。"""
    parser = argparse.ArgumentParser(
        description="InSAR 形变监测工具（基于 ESA SNAP / snappy）",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="数据目录",
    )
    parser.add_argument(
        "--master",
        type=str,
        default=None,
        help="主影像路径（不提供则交互选择）",
    )
    parser.add_argument(
        "--slave",
        type=str,
        default=None,
        help="从影像路径（不提供则交互选择）",
    )
    parser.add_argument(
        "--polarization",
        type=str,
        default="vv",
        choices=["vv", "vh"],
        help="极化通道（默认 vv）",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="输出目录（默认 data/insar）",
    )

    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir) if args.output_dir else data_dir / "insar"

    print("=" * 60)
    print("RS-Platform: InSAR 形变监测")
    print("=" * 60)

    # 确定主从影像
    if args.master and args.slave:
        master_path = Path(args.master)
        slave_path = Path(args.slave)
    else:
        slc_files = list_slc_scenes(data_dir)
        if len(slc_files) < 2:
            print(f"[错误] 需要至少 2 幅 SLC 影像，当前找到 {len(slc_files)} 幅")
            print(f"  请先通过管线下载 SLC 影像：")
            print(f"  python scripts/06_pipeline.py --satellite sentinel1 --s1-product slc --bbox ...")
            return

        master_path, slave_path = select_scenes_interactive(slc_files)
        if master_path is None:
            return

    # 执行 InSAR 处理
    try:
        result = run_insar(
            master_path=master_path,
            slave_path=slave_path,
            polarization=args.polarization,
            output_dir=output_dir,
        )
    except Exception as e:
        print(f"\n[错误] InSAR 处理失败: {e}")
        traceback.print_exc()
        return

    print(f"\n处理完成！请查看输出目录: {result['output_dir']}")


if __name__ == "__main__":
    main()
