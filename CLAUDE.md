# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

RS-Platform 是一个基于 Sentinel-2 卫星数据的遥感影像处理系统。从 Microsoft Planetary Computer (MPC) 搜索、下载影像，经过波段合成、去云、拼接裁剪后转为 ZARR 格式，通过 FastAPI 后端提供 Web 服务。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 运行完整管线（8 步：搜索→场景选择→签名→下载→合成→去云→拼接裁剪→ZARR）
python scripts/06_pipeline.py --bbox 116.0 39.0 117.0 40.0
python scripts/06_pipeline.py --aoi ./data/beijing_boundary.shp  # 使用 SHP 文件
python scripts/06_pipeline.py --adcode 110000                   # 使用 DataV 行政区划
python scripts/06_pipeline.py --admin-name "北京市"              # 模糊搜索行政区划

# 分步执行
python scripts/01_search_and_sign.py --bbox 116.0 39.0 117.0 40.0
python scripts/02_aria2_download.py --data-dir ./data
python scripts/03_band_merge.py --data-dir ./data
python scripts/04_cloud_mask.py --data-dir ./data
python scripts/07_mosaic_clip.py --data-dir ./data
python scripts/05_tif_to_zarr.py --data-dir ./data

# 启动后端 API
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

## 架构

系统分为两大部分：**数据处理管线**（scripts/）和 **Web 后端**（backend/）。

### 数据处理管线（scripts/）

各脚本以数字编号，按顺序执行。`06_pipeline.py` 是整合入口，动态导入各步骤模块（因为模块名以数字开头，Python 不允许直接 import，使用 `importlib` 动态加载）。

管线流程：
1. `01_search_and_sign.py` — 连接 MPC STAC API，搜索 Sentinel-2 L2A 影像，计算每景对研究区的覆盖率，选择最优场景组合（单日优先、跨日补充），生成 urls.txt 和 metadata.json
2. `02_aria2_download.py` — 用 ARIA2 批量下载波段文件，后台线程每 25 分钟刷新 MPC 签名 Token
3. `03_band_merge.py` — 将 B02+B03+B04 合成为 RGB 三通道 TIF
4. `04_cloud_mask.py` — 用 SCL 波段（20m 最近邻重采样到 10m）去除云像素
5. `07_mosaic_clip.py` — 拼接多景影像并按研究区几何裁剪
6. `05_tif_to_zarr.py` — TIF 转 ZARR v2 格式（1024×1024 chunk）

### 覆盖率与场景选择（utils/coverage.py）

- `load_aoi_geometry()` — 加载研究区（bbox 转 Polygon 或读取 SHP 文件）
- `compute_coverage()` / `compute_union_coverage()` — 计算单景/多景对研究区的覆盖率
- `select_optimal_scenes()` — 场景选择策略：先按日期分组，检查每组 union 覆盖率，优先选单日全覆盖的日期（云量最低），否则按日期从近到远累积

### Web 后端（backend/）

- `main.py` — FastAPI 入口，注册路由
- `routers/imagery.py` — 影像 API（列表、详情、切片、概览图、触发下载）
- `services/zarr_service.py` — ZARR 数据读取服务
- `models/imagery.py` — Pydantic 数据模型

## 关键配置（config/settings.py）

- `MPC_STAC_API_URL` — MPC STAC API 端点
- `DATAV_API_BASE` — 阿里云 DataV 行政区划 API 端点
- `SENTINEL2_COLLECTION` — Sentinel-2 L2A 集合名
- `BANDS` — 波段定义（B02/B03/B04/SCL）
- `RGB_BAND_ORDER` — 合成顺序 ["B04", "B03", "B02"]（即 R-G-B）
- `CLOUD_SCL_VALUES` — 需要去除的 SCL 值 [3, 8, 9, 10]
- `ARIA2_PARAMS` — ARIA2 下载参数
- `MIN_COVERAGE_RATIO` — 最低覆盖率阈值（默认 0.95）
- `ZARR_CHUNK_SIZE` — ZARR 分块大小
- `BOUNDARIES_DIR` — 行政区划 SHP 缓存目录

## 重要约束

- **ZARR 版本**：必须使用 v2（`zarr>=2.16.0,<3`），v3 与当前 xarray 不兼容。`05_tif_to_zarr.py` 启动时会检查版本。
- **numcodecs 版本**：必须 `<0.15`，0.15+ 移除了 `blosc.cbuffer_sizes`，与 zarr v2 不兼容。
- **MPC Token**：签名 URL 约 1 小时过期，下载器每 25 分钟自动刷新。
- **SCL 重采样**：必须用最近邻（nearest），不能用双线性等插值，否则会产生无效的分类值。
- **波段分辨率**：B02/B03/B04 为 10m，SCL 为 20m，需要重采样对齐。
- **Windows 环境**：ARIA2 路径指向 `aria2-1.37.0-win-64bit-build1/aria2c.exe`。

## 数据目录结构

```
data/
├── downloads/        # ARIA2 下载的原始波段 TIF
├── merged/           # 波段合成后的 RGB TIF
├── cloud_masked/     # 去云后的 TIF
├── mosaicked/        # 拼接裁剪后的 TIF
├── zarr/             # ZARR 输出
├── boundaries/       # DataV 行政区划 SHP 缓存
├── urls.txt          # ARIA2 输入文件
├── metadata.json     # 场景元数据（含 coverage_ratio、geometry）
└── aoi_geometry.json # 研究区几何（GeoJSON 格式）
```

## 研究区输入

支持四种方式（优先级：adcode > admin-name > aoi > bbox）：
- `--adcode 110000` — 行政区划代码，自动从阿里云 DataV API 获取边界并缓存为 SHP
- `--admin-name "北京市"` — 行政区划名称，模糊搜索匹配后自动获取边界
- `--aoi path/to/boundary.shp` — SHP 文件（任意多边形，自动读取几何并转为 EPSG:4326）
- `--bbox min_lon min_lat max_lon max_lat` — 矩形区域

DataV API 端点：`https://geo.datav.aliyun.com/areas_v3/bound/{adcode}.json`
