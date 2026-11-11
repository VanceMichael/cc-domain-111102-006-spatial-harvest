# 草原观测点接口的空间覆盖采集

合作方接口对每次框选最多返回三条记录，且只通过 `truncated` 告知结果被截断、
不说明漏掉了哪些点；`count` 不承诺为真实总量。本项目实现一个**只证明查询范围
覆盖情况**的自适应空间采集器（不做遥感分析）：从给定矩形开始四叉细分查询，
直到每个叶子格被完整证实，或因最小格宽 / 查询预算终止并输出精确的未证实区域。

全部代码仅依赖 Python 3.12 标准库；素材为人工构造坐标，不含真实敏感点位。

## 接口与数据约定

- `app/spatial.py` 的 `GridEndpoint` 是闭区间查询接口：端点包含，单次最多返回
  `cap`（默认 3）个点，`count` 只是已返回数量，`truncated=True` 才是继续细分的依据；
  接口本身拒绝跨 180 度经线的矩形（`west > east` 表示跨经线，见 `fixtures/regions.json`）。
- 查询坐标统一 EPSG:4326；点位带稳定 `id`。分割线属于闭区间，同一点会在相邻
  子框的响应中重复出现——采集器按 `id` 去重输出，但完整保留每个点来自哪些查询框。
- `fixtures/points.json` 为 3×3 普通网格点加 ±180 接缝点；`fixtures/dense.json`
  为 8 个完全同坐标、无法靠细分拆开的密集点；`fixtures/conflict.json` 含同 id 矛盾坐标。

## 模块结构

| 模块 | 职责 |
| --- | --- |
| `app/geo.py` | 跨经线展开（`east < west` 时经度 +360）、闭区间四等分、接缝 ±180 归一、球面梯形面积、GeoJSON 拆环 |
| `app/client.py` | 客户端协议：`DirectClient`（进程内合成接口）、`HttpClient`（urllib）、`ReplayClient`（离线重放） |
| `app/harvester.py` | 采集引擎：自适应细分、预算控制、证据缓存、断点续采、冲突检测、覆盖清单与成果落盘 |
| `app/cli.py` | 命令行入口（`run` / `show`） |
| `app/server.py` | 把合成接口暴露为本地 HTTP 服务，演示真实 HTTP 适配路径 |
| `app/demo.py` | 六个端到端场景，展示全部终止条件与精度权衡 |

## 终止条件（不会无限分裂，也不会把未证实区域算作空地）

1. **完整证实**：叶子格响应 `truncated=False`，记为 `complete / not_truncated`；
   空响应的叶子同样是完整叶子，表示经证实的无观测区域。
2. **最小格宽**：格宽（或格高）已到 `--min-width` 仍被截断，记为
   `incomplete / min_width_reached_truncated`，该精确区域写入 `unverified.geojson`，
   不计入已证实面积，更不算空地。
3. **查询预算**：剩余预算不足以完成本格框的底层请求时，未决格保持
   `pending / pending_resumable` 原样落盘，可加大预算后续采；另有 24 层最大深度兜底。
4. 跨经线格框在接口层拆成共享 ±180 边的两个请求，任一截断即整体截断，
   接缝点按展开坐标归一，`180` 与 `-180` 不会产生伪重复或伪冲突。

## 去重、来源与冲突

- `points.geojson` 每个稳定 id 只输出一个要素，`source_cells` 列出该点出现过的
  全部查询框（展开坐标键），`source_cell_count` 即重复命中次数；去重不改变目标边界。
- 同一 id 在不同响应中出现矛盾坐标（1e-9 度容差之外）时，点位保留一份并置
  `in_conflict=true`，全部坐标变体单列于 `conflicts.json` 与清单的 `conflicts` 段。
- 接口若返回请求框之外的点，不丢弃也不采信，隔离进清单 `quarantine` 段。

## 断点续采与参数变更保护

状态目录中 `evidence.json` 保存每个查询框的原始响应（追加式、原子替换落盘），
`state.json` 保存树节点、点位、冲突与预算进度，`config.fingerprint` 保存配置指纹：

- 中断后用**相同参数**重跑：已完成叶子与证据框直接复用，缓存命中不产生请求、
  不消耗预算，只补查 `pending` 节点。
- 指纹覆盖目标边界、最小格宽、`cap`、来源标识（预算是运营上限，不参与指纹，
  因此允许"预算耗尽 → 加大预算续采"）。指纹不一致时直接报错，拒绝套用旧完成标记。
- 显式 `--reuse-evidence <旧目录>` 时只迁移原始响应、重建整棵树；落在新目标框
  之外的旧查询框登记为 `outside_target`，在清单中单列，不参与本次覆盖统计。

## 运行方式

```sh
# 回归测试（含原有来源读取测试，共 20 个）
python3 -m unittest discover -s tests -v
python3 -m compileall -q app tests

# 六个端到端场景：普通网格 / 跨经线 / 同坐标密集 / 预算续采 / 坐标冲突 / 参数变更
python3 -m app.demo
# 隔离模式运行（cwd 需显式加入 sys.path）
python3 -I -c "import sys; sys.path.insert(0,'.'); from app.demo import main; main()"

# CLI：先小预算中断，再加大预算续采
python3 -m app.cli run --bounds 100 35 102 37 --min-width 0.5 --budget 4  --state-dir runtime/run1
python3 -m app.cli run --bounds 100 35 102 37 --min-width 0.5 --budget 200 --state-dir runtime/run1
python3 -m app.cli show --state-dir runtime/run1

# 跨 180 度经线目标（east < west）
python3 -m app.cli run --bounds 179 -1 -179 1 --min-width 0.5 --budget 200 --state-dir runtime/run2

# 可选：HTTP 适配演示
python3 -m app.server --fixture fixtures/points.json --port 8765
python3 -m app.cli run --http http://127.0.0.1:8765 --bounds 100 35 102 37 \
    --min-width 0.5 --budget 200 --state-dir runtime/run3
```

## 成果文件（位于状态目录，runtime/ 已在 .gitignore）

| 文件 | 内容 |
| --- | --- |
| `points.geojson` | 去重点位，含每个 id 的全部来源查询框与冲突标记 |
| `leaves.geojson` | 全部叶子格，属性含状态、原因、深度、面积；跨经线格拆双环 |
| `unverified.geojson` | 尚未证实覆盖的精确区域（最小格宽截断 + 预算未决） |
| `manifest.json` | 覆盖清单：配置指纹、面积汇总、每个叶子格的完整/未完成/未决原因、范围外证据、冲突、隔离区与原因字典 |
| `conflicts.json` | 同 id 矛盾坐标的全部变体 |
| `evidence.json` / `state.json` / `config.fingerprint` | 可复用的原始证据、树状态与配置指纹 |

## 精度选择如何影响请求量与完整性

`app.demo` 场景 3 在同坐标密集点上对比三种最小格宽（目标框 2°×2°）：

| min_width | 实际请求数 | 未完成叶子 | 未证实面积 |
| --- | --- | --- | --- |
| 1.0° | 5 | 4 | 整个目标框量级 |
| 0.1° | 69 | 4 | 约 156 km² |
| 0.02° | 101 | 4 | 约 9.8 km² |

四叉树最坏情况下每深一层请求量约 ×4；最小格宽每减半，未证实区域约缩至 1/4，
但密集同坐标点在任何细分级别都无法被拆开，只能把未证实范围缩小到最小格尺寸并
如实标注。空叶子在首次未截断响应后即停止，因此稀疏区域的实际请求量远小于最坏值
（演示中 0.05° 精度的稀疏网格仅需 21 次请求）。选择精度时应在接口预算与可接受的
未证实区域面积之间权衡，而不是无限调小格宽。
