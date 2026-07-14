# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

RS-Platform 是一个基于 Sentinel-1/2 卫星数据的遥感影像处理系统。从 Microsoft Planetary Computer (MPC) 搜索、下载影像，经过波段合成、去云/预处理、拼接裁剪后转为 ZARR 格式，通过 FastAPI 后端提供 Web 服务。支持 Sentinel-1 GRD/SLC 影像下载、SAR 预处理和 InSAR 形变监测。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 安装 esa_snappy（S1 预处理和 InSAR 需要）：
# 1. 安装 ESA SNAP Desktop: https://step.esa.int/ （默认路径 D:\esa-snap）
# 2. pip install esa-snappy
# 3. 解压 jpy 模块（如果 import esa_snappy 报错 jpyutil）：
#    python -c "import zipfile,glob;whl=glob.glob(r'D:\Anaconda\Lib\site-packages\esa_snappy\lib\jpy*win*whl')[0];z=zipfile.ZipFile(whl);[z.extract(n,r'D:\Anaconda\Lib\site-packages\esa_snappy') for n in z.namelist() if n.endswith('.py') or n.endswith('.pyd')]"
# 4. 设置环境变量 JAVA_HOME=D:\esa-snap\jre

# ==================== Sentinel-2（光学影像）====================
# 运行完整管线（9 步：搜索→选择→签名→下载→合成→S1预处理→去云→裁剪→ZARR）
# 默认交互式选择时相，也可用 --auto-select 自动选择最优
python scripts/06_pipeline.py --bbox 116.0 39.0 117.0 40.0
python scripts/06_pipeline.py --aoi ./data/beijing_boundary.shp  # 使用 SHP 文件
python scripts/06_pipeline.py --adcode 110000                   # 使用 DataV 行政区划
python scripts/06_pipeline.py --admin-name "北京市"              # 模糊搜索行政区划
python scripts/06_pipeline.py --auto-select                     # 自动选择云量最低的达标时相

# S2 波段选择（支持交互式选择或命令行指定）
python scripts/06_pipeline.py --bbox 116.0 39.0 117.0 40.0              # 交互式选择波段
python scripts/06_pipeline.py --bbox 116.0 39.0 117.0 40.0 --bands B02 B03 B04 B08 SCL  # 指定波段
python scripts/06_pipeline.py --bbox 116.0 39.0 117.0 40.0 --bands false_color          # 使用预设
python scripts/06_pipeline.py --bbox 116.0 39.0 117.0 40.0 --bands rgb_scl              # RGB + 去云

# ==================== Sentinel-1 GRD（SAR 影像）====================
python scripts/06_pipeline.py --satellite sentinel1 --bbox 116.0 39.0 117.0 40.0
python scripts/06_pipeline.py --satellite sentinel1 --adcode 110000
python scripts/06_pipeline.py --satellite sentinel1 --admin-name "北京市"

# ==================== Sentinel-1 SLC（用于 InSAR）====================
python scripts/06_pipeline.py --satellite sentinel1 --s1-product slc --bbox 116.0 39.0 117.0 40.0 --date "2024-01-01/2024-03-01"

# ==================== 任务管理 ====================
# 每次运行管线会自动创建独立的任务目录（data/tasks/时间戳_卫星类型）
python scripts/task_manager.py list           # 列出所有任务
python scripts/task_manager.py info TASK_ID   # 查看任务详情
python scripts/task_manager.py cleanup --keep 5  # 清理旧任务（保留最近5个）

# ==================== InSAR 形变监测 ====================
# 交互式选择主从影像
python scripts/09_insar_analysis.py --data-dir ./data --polarization vv
# 直接指定主从影像
python scripts/09_insar_analysis.py --master path/master.tif --slave path/slave.tif --polarization vv

# ==================== 分步执行 ====================
python scripts/01_search_and_sign.py --bbox 116.0 39.0 117.0 40.0
python scripts/02_aria2_download.py --data-dir ./data
python scripts/03_band_merge.py --data-dir ./data
python scripts/04_cloud_mask.py --data-dir ./data
python scripts/07_mosaic_clip.py --data-dir ./data
python scripts/08_s1_preprocess.py --data-dir ./data   # S1 GRD 预处理（snappy）
python scripts/05_tif_to_zarr.py --data-dir ./data

# ==================== 启动后端 API ====================
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

## 架构

系统分为两大部分：**数据处理管线**（scripts/）和 **Web 后端**（backend/）。

### 数据处理管线（scripts/）

各脚本以数字编号，按顺序执行。`06_pipeline.py` 是整合入口，动态导入各步骤模块（因为模块名以数字开头，Python 不允许直接 import，使用 `importlib` 动态加载）。

管线流程（`--satellite sentinel1` 时 S1 特定步骤自动适配）：

| 步骤 | 脚本 | 说明 | S1 GRD | S1 SLC | S2 |
|------|------|------|--------|--------|-----|
| 1 | `01_search_and_sign.py` | STAC 搜索 + 覆盖率计算 | ✓ | ✓ | ✓ |
| 2 | — | 交互式时相选择 | ✓ | ✓ | ✓ |
| 3 | — | URL 签名 + 生成 urls.txt | ✓ | ✓ | ✓ |
| 4 | `02_aria2_download.py` | ARIA2 批量下载 | ✓ | ✓ | ✓ |
| 5 | `03_band_merge.py` | 波段合成 | vv+vh→双通道 | vv+vh→双通道 | B02+B03+B04→RGB |
| 6 | `08_s1_preprocess.py` | snappy 预处理（轨道→定标→滤波→TC→dB） | **执行** | 跳过 | 跳过 |
| 7 | `04_cloud_mask.py` | SCL 去云 | 跳过 | 跳过 | **执行** |
| 8 | `07_mosaic_clip.py` | 独立裁剪或拼接后裁剪 | ✓ | ✓ | ✓ |
| 9 | `05_tif_to_zarr.py` | TIF → ZARR v2（1024×1024 chunk） | ✓ | ✓ | ✓ |

独立分析模块：
- `09_insar_analysis.py` — InSAR 形变监测：对两幅 SLC 影像执行干涉处理，输出形变图和分析报告

### 覆盖率与场景选择（utils/coverage.py）

- `load_aoi_geometry()` — 加载研究区（bbox 转 Polygon 或读取 SHP 文件）
- `compute_coverage()` / `compute_union_coverage()` — 计算单景/多景对研究区的覆盖率
- `analyze_coverage_by_date()` — 按日期分析各时相的覆盖率情况（不做选择）
- `select_optimal_scenes()` — 场景选择入口：默认交互式让用户选择时相；`auto_select=True` 时自动选最优（云量最低的达标日期）
- `interactive_select_dates()` — 交互式时相选择：展示各日期覆盖率/云量/达标状态，用户输入序号选择

场景选择策略：
- 默认（交互模式）：展示所有时相的覆盖率和云量，用户手动选择要下载的时相
- `--auto-select`：自动选择云量最低且覆盖率 >= 阈值的日期；若无单日达标则按日期从近到远累积

拼接裁剪策略（`07_mosaic_clip.py`）：
- 多景各自覆盖率都 >= 阈值时，对每景独立裁剪（输出多张 TIF）
- 否则拼接所有影像后统一裁剪（输出单张 TIF）

管线范围控制：
- 管线中 Steps 5/6/7 只处理 Step 2 选中的场景（通过 `scene_ids` 参数过滤），不会处理历史残留文件
- 独立运行各脚本时（不通过管线），处理目录下所有文件

### Web 后端（backend/）

- `main.py` — FastAPI 入口，注册路由
- `routers/imagery.py` — 影像 API（列表、详情、切片、概览图、触发下载）
- `services/zarr_service.py` — ZARR 数据读取服务
- `models/imagery.py` — Pydantic 数据模型

## 关键配置（config/settings.py）

- `MPC_STAC_API_URL` — MPC STAC API 端点
- `DATAV_API_BASE` — 阿里云 DataV 行政区划 API 端点
- `SENTINEL2_COLLECTION` — Sentinel-2 L2A 集合名
- `SENTINEL1_GRD_COLLECTION` / `SENTINEL1_SLC_COLLECTION` — Sentinel-1 GRD/SLC 集合名
- `BANDS` — S2 波段定义（B02/B03/B04/SCL）
- `S1_BANDS` — S1 波段定义（vv/vh）
- `RGB_BAND_ORDER` — S2 合成顺序 ["B04", "B03", "B02"]
- `S1_BAND_ORDER` — S1 合成顺序 ["vv", "vh"]
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
- **esa_snappy 依赖**：S1 GRD 预处理（`08_s1_preprocess.py`）和 InSAR 分析（`09_insar_analysis.py`）需要 `esa_snappy`。安装步骤：① 安装 ESA SNAP Desktop ② `pip install esa-snappy` ③ 解压 `esa_snappy/lib/` 下的 jpy wheel 包 ④ 设置 `JAVA_HOME=D:\esa-snap\jre`。轨道文件首次运行时自动下载。代码中使用 `from esa_snappy import ProductIO, GPF, HashMap`。
- **S1 SLC vs GRD**：`--s1-product grd`（默认）下载 GRD 产品并执行 snappy 预处理；`--s1-product slc` 下载 SLC 产品用于 InSAR 分析。

## 数据目录结构

```
data/
├── tasks/                          # 任务目录（每次运行自动创建）
│   ├── 20240115_120000_S1_SLC/    # 任务1：时间戳_卫星类型
│   │   ├── downloads/             # ARIA2 下载的原始波段 TIF
│   │   ├── merged/                # 波段合成后的 TIF
│   │   ├── cloud_masked/          # 去云后或 S1 GRD 预处理后的 TIF
│   │   ├── mosaicked/             # 拼接裁剪后的 TIF
│   │   ├── zarr/                  # ZARR 输出
│   │   ├── insar/                 # InSAR 输出（如适用）
│   │   ├── urls.txt               # ARIA2 输入文件
│   │   ├── metadata.json          # 场景元数据
│   │   ├── aoi_geometry.json      # 研究区几何
│   │   └── task_info.json         # 任务元信息
│   ├── 20240116_093000_S2/        # 任务2
│   └── task_latest -> ...         # 软链接指向最新任务
├── boundaries/                     # DataV 行政区划 SHP 缓存
└── ...                             # 其他全局数据
```

## 研究区输入

支持四种方式（优先级：adcode > admin-name > aoi > bbox）：
- `--adcode 110000` — 行政区划代码，自动从阿里云 DataV API 获取边界并缓存为 SHP
- `--admin-name "北京市"` — 行政区划名称，模糊搜索匹配后自动获取边界
- `--aoi path/to/boundary.shp` — SHP 文件（任意多边形，自动读取几何并转为 EPSG:4326）
- `--bbox min_lon min_lat max_lon max_lat` — 矩形区域

DataV API 端点：`https://geo.datav.aliyun.com/areas_v3/bound/{adcode}.json`
