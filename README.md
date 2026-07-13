# RS-Platform 遥感影像处理系统

基于 Sentinel-2 卫星数据的遥感影像下载、处理与展示平台。支持从 Microsoft Planetary Computer 批量获取数据，自动完成去云、波段合成、多景拼接、格式转换等处理流程，并通过 Web 服务提供可视化展示。

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    前端 (React + OpenLayers)                 │
│              地图展示 / 参数配置 / 影像浏览                    │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼──────────────────────────────────┐
│                    后端 (FastAPI)                            │
│           REST API / ZARR 数据服务 / 任务调度                 │
└───────┬──────────────────────────────────────┬──────────────┘
        │                                      │
┌───────▼──────────┐                ┌──────────▼──────────────┐
│ PostgreSQL +     │                │  ZARR 数据存储           │
│ PostGIS          │                │  (分块压缩数组)          │
│ (元数据管理)      │                │                          │
└──────────────────┘                └──────────────────────────┘
                                            ▲
┌───────────────────────────────────────────┴──────────────────┐
│                   数据处理管线                                │
│                                                              │
│  MPC STAC API ──→ 场景选择 ──→ ARIA2 下载 ──→ 波段合成       │
│   (搜索+覆盖率)   (最优组合)    (批量+Token刷新) (RGB TIF)     │
│                                                              │
│  ──→ SCL 去云 ──→ 拼接+裁剪 ──→ ZARR                        │
│       (云掩膜)    (多景拼接)    (格式转换)                     │
└──────────────────────────────────────────────────────────────┘
```

## 功能特性

| 功能 | 说明 |
|------|------|
| **STAC 搜索** | 按区域、日期、云量搜索 Sentinel-2 L2A 影像 |
| **覆盖率计算** | 计算每景影像对研究区的覆盖率，支持单景/多景覆盖率分析 |
| **智能场景选择** | 单日优先、跨日补充，贪心算法选择最小场景集合以完全覆盖研究区 |
| **研究区输入** | 支持 bbox 矩形、SHP 文件、行政区划名称/adcode 四种方式 |
| **批量下载** | 使用 ARIA2 多连接并行下载，支持断点续传 |
| **Token 自动刷新** | 每 25 分钟自动刷新 MPC 签名 URL，避免下载中断 |
| **波段合成** | B02(蓝) + B03(绿) + B04(红) → RGB 真彩色 TIF |
| **SCL 去云** | 使用场景分类层去除云、云阴影、卷云像素 |
| **多景拼接+裁剪** | 自动拼接多景影像并按研究区几何裁剪，输出完整覆盖的单张 TIF |
| **ZARR 转换** | TIF → ZARR 分块存储，前端按需加载，提升展示性能 |
| **REST API** | FastAPI 后端，提供影像列表、切片、概览图等接口 |

## 目录结构

```
RS-Platform/
├── config/
│   └── settings.py              # 全局配置
├── utils/
│   ├── coverage.py              # 覆盖率计算 + 场景选择
│   └── datav_boundary.py        # DataV 行政区划边界获取
├── scripts/
│   ├── 01_search_and_sign.py    # STAC 搜索 + 覆盖率 + URL 签名
│   ├── 02_aria2_download.py     # ARIA2 批量下载 + Token 刷新
│   ├── 03_band_merge.py         # 波段合成
│   ├── 04_cloud_mask.py         # SCL 去云处理
│   ├── 05_tif_to_zarr.py        # TIF → ZARR 转换
│   ├── 06_pipeline.py           # 整合管线（8 步）
│   └── 07_mosaic_clip.py        # 多景拼接 + 研究区裁剪
├── backend/
│   ├── main.py                  # FastAPI 入口
│   ├── routers/
│   │   └── imagery.py           # 影像 API 路由
│   ├── services/
│   │   └── zarr_service.py      # ZARR 数据服务
│   └── models/
│       └── imagery.py           # 数据模型
├── frontend/                    # React + OpenLayers 前端（待开发）
├── data/
│   ├── downloads/               # ARIA2 下载的原始波段
│   ├── merged/                  # 合成后的 RGB TIF
│   ├── cloud_masked/            # 去云后的 TIF
│   ├── mosaicked/               # 拼接裁剪后的 TIF
│   ├── boundaries/              # DataV 行政区划 SHP 缓存
│   └── zarr/                    # ZARR 输出
├── aria2-1.37.0-win-64bit-build1/  # ARIA2 工具
├── requirements.txt
└── README.md
```

## 快速开始

### 1. 环境准备

**Python 依赖：**

```bash
pip install -r requirements.txt
```

**主要依赖说明：**

| 包名 | 用途 |
|------|------|
| `pystac-client` | STAC API 客户端，搜索 Sentinel-2 数据 |
| `planetary-computer` | MPC URL 签名，获取带 SAS Token 的下载链接 |
| `rasterio` | GeoTIFF 读写、影像拼接（merge）、裁剪（mask） |
| `rioxarray` | GeoTIFF ↔ xarray 转换 |
| `xarray` | 多维数组处理，ZARR 读写 |
| `zarr` | ZARR 格式支持 |
| `shapely` | 几何对象处理，覆盖率计算 |
| `geopandas` | 地理空间数据处理，读取 SHP 文件 |
| `fastapi` | Web API 框架 |
| `uvicorn` | ASGI 服务器 |

### 2. 配置参数

编辑 `config/settings.py` 修改默认配置：

```python
# 默认搜索区域（北京市）
DEFAULT_BBOX = [116.0, 39.0, 117.0, 40.0]

# 默认研究区 SHP 文件路径（None 表示使用 bbox）
DEFAULT_AOI_PATH = None

# 默认日期范围
DEFAULT_DATE_RANGE = "2024-01-01/2024-06-30"

# 默认最大云量
DEFAULT_CLOUD_COVER_MAX = 20

# 最低覆盖率阈值（0.95 = 95%）
MIN_COVERAGE_RATIO = 0.95

# ARIA2 路径
ARIA2_PATH = PROJECT_ROOT / "aria2-1.37.0-win-64bit-build1" / "aria2c.exe"
```

### 3. 运行数据处理管线

**方式一：完整管线（一键执行）**

```bash
# 使用 bbox
python scripts/06_pipeline.py \
    --bbox 116.0 39.0 117.0 40.0 \
    --date "2024-01-01/2024-06-30" \
    --cloud-cover 20 \
    --output ./data

# 使用 SHP 文件作为研究区
python scripts/06_pipeline.py \
    --aoi ./data/beijing_boundary.shp \
    --date "2024-01-01/2024-06-30" \
    --cloud-cover 20 \
    --output ./data

# 使用行政区划代码（自动从 DataV 获取边界）
python scripts/06_pipeline.py \
    --adcode 110000 \
    --date "2024-01-01/2024-06-30"

# 使用行政区划名称（模糊搜索）
python scripts/06_pipeline.py \
    --admin-name "北京市" \
    --date "2024-01-01/2024-06-30"

# 自定义最低覆盖率阈值
python scripts/06_pipeline.py \
    --bbox 116.0 39.0 117.0 40.0 \
    --min-coverage 0.90 \
    --output ./data
```

**方式二：分步执行**

```bash
# Step 1: 搜索 + 覆盖率计算 + 场景选择 + URL 签名
python scripts/01_search_and_sign.py --bbox 116.0 39.0 117.0 40.0 --date "2024-01-01/2024-06-30"

# Step 2: ARIA2 批量下载
python scripts/02_aria2_download.py --data-dir ./data

# Step 3: 波段合成
python scripts/03_band_merge.py --data-dir ./data

# Step 4: SCL 去云
python scripts/04_cloud_mask.py --data-dir ./data

# Step 5: 多景拼接 + 研究区裁剪
python scripts/07_mosaic_clip.py --data-dir ./data

# Step 6: TIF → ZARR
python scripts/05_tif_to_zarr.py --data-dir ./data
```

### 4. 启动后端 API 服务

```bash
# 开发模式（热重载）
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

# 生产模式
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 4
```

启动后访问：
- API 文档（Swagger UI）：http://localhost:8000/docs
- API 文档（ReDoc）：http://localhost:8000/redoc

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | 服务健康检查 |
| `GET` | `/health` | 详细健康状态 |
| `GET` | `/api/imagery/list` | 列出所有可用影像 |
| `GET` | `/api/imagery/{id}` | 获取影像详情 |
| `GET` | `/api/imagery/{id}/tile?x_min=&x_max=&y_min=&y_max=` | 获取影像切片（PNG） |
| `GET` | `/api/imagery/{id}/overview` | 获取影像概览图 |
| `POST` | `/api/imagery/download` | 触发新影像下载任务 |

**触发下载请求示例：**

```json
// 使用 bbox
POST /api/imagery/download
{
    "bbox": [116.0, 39.0, 117.0, 40.0],
    "date_range": "2024-01-01/2024-06-30",
    "cloud_cover_max": 20
}

// 使用行政区划代码
POST /api/imagery/download
{
    "adcode": "110000",
    "date_range": "2024-01-01/2024-06-30",
    "cloud_cover_max": 20
}

// 使用行政区划名称（模糊搜索）
POST /api/imagery/download
{
    "admin_name": "北京市",
    "date_range": "2024-01-01/2024-06-30",
    "cloud_cover_max": 20
}
```

**研究区输入优先级**：`adcode` > `admin_name` > `aoi_path` > `bbox`

**影像元数据示例（含覆盖率）：**

```json
{
    "scene_id": "S2A_MSIL2A_20240115T031111_N0510_R075_T50TLK",
    "datetime": "2024-01-15T03:11:11Z",
    "date": "20240115",
    "cloud_cover": 12.5,
    "bbox": [115.9, 38.9, 117.1, 40.1],
    "coverage_ratio": 0.87,
    "bands": { ... }
}
```

## 管线流程

完整管线包含 8 个步骤：

```
Step 1: STAC 搜索 + 覆盖率计算
        ↓
Step 2: 场景选择（单日优先，跨日补充）
        ↓
Step 3: URL 签名（MPC SAS Token）
        ↓
Step 4: ARIA2 批量下载（多连接并行 + Token 自动刷新）
        ↓
Step 5: 波段合成（B02+B03+B04 → RGB TIF）
        ↓
Step 6: SCL 去云（云、云阴影、卷云像素设为 NoData）
        ↓
Step 7: 多景拼接 + 研究区裁剪（rasterio.merge + rasterio.mask）
        ↓
Step 8: TIF → ZARR（分块压缩存储，前端按需加载）
```

## 数据来源

- **卫星数据**：Sentinel-2 L2A（Level-2A，大气校正后）
- **数据平台**：[Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/)
- **API 协议**：STAC（SpatioTemporal Asset Catalog）
- **认证方式**：`planetary_computer.sign()` URL 签名（无需 API Key）
- **行政区划数据**：[阿里云 DataV](https://datav.aliyun.com/) GeoJSON API

## 行政区划边界获取

支持通过行政区划名称或代码自动获取研究区边界：

```bash
# 使用 adcode 获取
python utils/datav_boundary.py --adcode 110000

# 使用名称模糊搜索
python utils/datav_boundary.py --name "北京市"
python utils/datav_boundary.py --name "杭州"
```

**常用 adcode 示例：**

| 地区 | adcode |
|------|--------|
| 北京市 | 110000 |
| 上海市 | 310000 |
| 杭州市 | 330100 |
| 深圳市 | 440300 |
| 成都市 | 510100 |

获取的 SHP 文件自动缓存到 `data/boundaries/` 目录，避免重复请求。

**缓存机制**：
- 首次运行：按树形结构（国家→省→市→区县）获取所有行政区划，保存 `index.json` 索引
- 后续运行：直接读取本地索引，按需获取 GeoJSON，无网络请求
- 强制重建索引：`python utils/datav_boundary.py --name "北京市" --rebuild-index`

## Sentinel-2 波段说明

| 波段 | 名称 | 波长 (nm) | 分辨率 (m) | 用途 |
|------|------|-----------|------------|------|
| B02 | Blue | 490 | 10 | RGB 蓝通道 |
| B03 | Green | 560 | 10 | RGB 绿通道 |
| B04 | Red | 665 | 10 | RGB 红通道 |
| SCL | Scene Classification Layer | - | 20 | 云检测与去云 |

## SCL 去云分类值

| 值 | 类别 | 处理方式 |
|----|------|----------|
| 0 | NO_DATA | 标记为无效 |
| 1 | SATURATED_OR_DEFECTIVE | 标记为无效 |
| 3 | CLOUD_SHADOWS | 去除 |
| 8 | CLOUD_MEDIUM_PROBABILITY | 去除 |
| 9 | CLOUD_HIGH_PROBABILITY | 去除 |
| 10 | THIN_CIRRUS | 去除 |
| 4, 5, 6 | 植被 / 裸土 / 水体 | 保留 |

## 输出文件命名规则

```
{scene_id}_{date}_{cloud_cover}_{类型}.tif

示例（单景文件）：
S2A_MSIL2A_20240115T031111_N0510_R075_T50TLK_20240115T061738_20240115_12.5_B04.tif
S2A_MSIL2A_20240115T031111_N0510_R075_T50TLK_20240115T061738_20240115_12.5_RGB.tif
S2A_MSIL2A_20240115T031111_N0510_R075_T50TLK_20240115T061738_20240115_12.5_RGB_cloudmasked.tif

拼接裁剪输出：
mosaicked_clipped.tif    # 多景拼接后按研究区裁剪的完整覆盖影像
```

## 技术决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 下载工具 | ARIA2 | 多连接并行、断点续传，适合大文件批量下载 |
| 去云时机 | 波段合成后 | 只需读取一次 SCL 文件 |
| Token 刷新间隔 | 25 分钟 | MPC Token 约 1 小时过期，留充足余量 |
| ZARR chunk 大小 | 1024×1024 | 平衡前端切片加载速度和存储效率 |
| SCL 重采样方法 | 最近邻 | 保持分类值完整性，不产生新值 |
| 覆盖率计算 | Shapely 几何交集 | 精确计算影像与研究区的重叠比例 |
| 场景选择策略 | 单日优先、跨日补充 | 优先使用同日影像保持时相一致，不足时跨日补充 |
| 研究区输入 | bbox + SHP + DataV 行政区划 | 支持矩形、任意多边形、行政区划名称/adcode |
| 行政区划获取 | 阿里云 DataV API | 免费服务，自动缓存 SHP，支持模糊搜索 |
| 拼接工具 | rasterio.merge | GDAL 生态成熟，支持大影像拼接 |
| 裁剪工具 | rasterio.mask | 支持按任意几何裁剪，保留空间参考 |

## 后续开发

- [ ] React + OpenLayers 前端界面
- [ ] PostgreSQL + PostGIS 数据库集成
- [ ] GeoServer 影像服务发布
- [ ] 变化检测算法集成
- [ ] 地物分类算法集成
- [ ] 用户认证与权限管理
- [ ] 任务队列（Celery）异步处理
- [ ] 覆盖率可视化（前端展示影像覆盖范围与研究区对比）
- [ ] 交互式场景选择（用户手动选择要下载的场景）

## 许可证

本项目仅供学习和研究使用。Sentinel-2 数据由 ESA 提供，遵循 [Copernicus Open Access Hub](https://scihub.copernicus.eu/) 的数据政策。
