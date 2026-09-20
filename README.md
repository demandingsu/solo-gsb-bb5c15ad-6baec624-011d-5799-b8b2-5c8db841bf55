# 裁纸排版 API（Guillotine Cutting Plan API）

面向小型印刷店的裁纸排版服务：输入原纸尺寸、切缝宽度和成品清单，按**可旋转的铡刀式切割**规则（每一刀都贯穿当前子矩形）生成多张原纸的排版方案。每张原纸返回成品坐标、旋转标记、利用率，以及与坐标完全一致的二叉切割树；切缝计入每一次分割，成品互不重叠、不越界。

## 一键启动

```bash
docker compose up --build
```

依赖安装已包含在镜像构建中，无需任何额外步骤。

- API 地址：**http://localhost:8000**
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 宿主机映射端口，如 `PORT=9000 docker compose up --build` |

业务约束在 `app/models.py` 顶部以常量定义（最大尺寸 100000、最大切缝 1000、单种成品最大数量 100000、单批次成品总数上限 20000 等），可按需调整。

## API

### `POST /plans`

**请求体**

```json
{
  "stock": { "width": 900, "height": 600 },
  "kerf": 3,
  "items": [
    { "id": "flyer", "width": 210, "height": 148, "quantity": 8 },
    { "id": "card",  "width": 90,  "height": 55,  "quantity": 12 }
  ],
  "locked_placements": [
    { "item": "flyer", "x": 0, "y": 0, "rotated": false }
  ],
  "max_cuts_per_sheet": 6
}
```

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `stock.width` / `stock.height` | number | `> 0` | 原纸宽/高 |
| `kerf` | number | `>= 0`，可省略，默认 `0` | 切缝宽度，每刀损耗的材料宽度 |
| `items[].id` | string | 可选，需唯一 | 成品标识，缺省自动生成 `item-1`、`item-2`… |
| `items[].width` / `items[].height` | number | `> 0` | 成品宽/高 |
| `items[].quantity` | integer | `>= 1` | 需求数量 |
| `locked_placements[]` | array | 可选，默认空 | 预先定位、必须**原样保留**在第一张原纸上的成品 |
| `locked_placements[].item` | string | 必须出现在 `items[].id` 中 | 被锁定成品标识 |
| `locked_placements[].x` / `y` | number | `>= 0` | 左上角坐标，旋转后的占位不得越出原纸 |
| `locked_placements[].rotated` | boolean | 可省略，默认 `false` | 是否旋转 90° |
| `max_cuts_per_sheet` | integer | 可选，正整数（`>= 1`），缺省不限制 | 每张原纸允许的**刀数上限** |

**锁定数量计入需求**：每种成品的锁定件数从其 `quantity` 中扣除，超出 `quantity` 返回 422；锁定项之间不得重叠。锁定布局还必须在给定 `kerf` 下能递归分解为铡刀切割树（每件之间留得出贯穿全矩形的切缝），否则返回 422 并在 `detail` 中指出冲突项的序号（从 1 开始）与 `item` id。验证通过后，引擎只在切割树的**可用余料叶**中放置剩余数量，放不下再开新原纸；锁定成品的坐标永不移动。

**每纸刀数上限（`max_cuts_per_sheet`）**：刀数按 `cut_tree` 中的 `cut` 节点计数。第一张原纸的锁定布局同样计入：锁定树自身的刀数一旦超过上限，直接返回 422（`detail` 含实际刀数与上限）。后续排版选位时，只采用**预计切分后整纸刀数仍不超上限**的余料候选；在适配程度（短边余量、剩余面积）相同的情况下，优先选择新增刀数更少的候选。所有已开纸张的余料都不合用时可以新开一张空白原纸；若单件成品在空白原纸上（含任意朝向）也无法在上限内形成合法切割树，则返回 422，例如：`item 'A' cannot be placed within the max_cuts_per_sheet limit of 1: even a blank stock sheet requires 2 guillotine cuts ...`。注意一件恰好铺满原纸的成品需要 0 刀（仅保留不大于一个切缝的修边）。

**响应（200）**

```json
{
  "stock": { "width": 900.0, "height": 600.0 },
  "kerf": 3.0,
  "sheets": [
    {
      "index": 0,
      "width": 900.0,
      "height": 600.0,
      "utilization": 0.570444,
      "placements": [
        { "item": "flyer", "x": 0.0, "y": 0.0, "width": 210.0, "height": 148.0, "rotated": false, "locked": true }
      ],
      "cut_tree": {
        "type": "cut",
        "direction": "vertical",
        "x": 0.0, "y": 0.0, "width": 900.0, "height": 600.0,
        "children": [ { "…": "…" }, { "…": "…" } ]
      },
      "cut_count": 1
    }
  ],
  "summary": {
    "sheet_count": 1,
    "piece_count": 20,
    "used_area": 308040.0,
    "stock_area": 540000.0,
    "utilization": 0.570444
  }
}
```

- `placements[]`：每件成品的左上角坐标、放置后的宽高与 `rotated`（是否旋转 90°；旋转后 `width`/`height` 为原高/宽），以及 `locked`（`true` 表示该件来自请求中的 `locked_placements`，坐标与请求完全一致；锁定件全部位于 `index: 0` 的第一张原纸）。坐标系原点在**原纸左上角**，x 向右、y 向下。
- `cut_tree`：同时覆盖锁定件与新增件，锁定件对应 `item` 叶的坐标即请求坐标；树中其余的 `waste` 叶即剩余数量可使用的余料。
- `cut_count`：该原纸的刀数，与 `cut_tree` 中 `cut` 节点的实际数量严格一致（可据此直接核对）；请求给出 `max_cuts_per_sheet` 时保证 `cut_count <= max_cuts_per_sheet`。
- `utilization`：成品面积 ÷ 原纸面积，切缝与边角废料均计入损耗（分子包含锁定件与新增件）。
- `summary`：总用纸数、总件数（锁定件 + 剩余件，即全部 `items` 数量之和）与整体利用率。

### 二叉切割树（`cut_tree`）

每个节点都带有外接矩形 `x, y, width, height`，共三种节点：

- `cut`（内部节点）：一刀**贯穿当前整个子矩形**。
  - `direction: "vertical"`：竖直一刀，`children[0]` 为左侧、`children[1]` 为右侧；
  - `direction: "horizontal"`：水平一刀，`children[0]` 为上侧、`children[1]` 为下侧；
  - 第二个子节点的起点 = 第一个子节点的终点 + `kerf`（切缝被这刀消耗）。
- `item`（叶子）：一件成品，坐标与 `placements` 中的条目一一对应。
- `waste`（叶子）：剩余边角料（可回收利用的余料）。

切割顺序即自根向下的前序遍历。若余料宽度不足一个切缝（刀片会把它完全吃掉），则作为修边损耗，不再产生节点。

**示例**：竖切节点中 `children[1].x === children[0].x + children[0].width + kerf`。

### 错误

| 状态码 | 情形 |
| --- | --- |
| `422` | 输入校验失败：尺寸非正数、数量小于 1、成品列表为空、`id` 重复等，`detail` 含具体字段 |
| `422` | 某成品两种朝向都放不进一张原纸，`detail` 明确指出该成品，例如：<br>`item 'poster' (150.0 x 80.0) does not fit on stock 100.0 x 100.0 in either orientation` |
| `422` | 锁定项引用了不存在的 `item`，或锁定数量超过该成品的 `quantity`，`detail` 指出锁定项序号（`#1`、`#2`…） |
| `422` | 锁定成品越界、彼此重叠：`locked placements #1 item 'A' and #2 item 'B' overlap` |
| `422` | 锁定布局无法在给定 `kerf` 下分解为铡刀切割树（不存在贯穿整个子矩形、且留出 `kerf` 切缝的分割刀），`detail` 指出冲突项，例如：<br>`locked placements cannot be produced by guillotine cuts with kerf 3: no full-span cut ... separates the conflicting placements (#2 item 'A', #3 item 'B'); adjust their coordinates or the kerf` |
| `422` | 锁定布局自身需要的刀数超过 `max_cuts_per_sheet`，`detail` 给出实际刀数，例如：<br>`locked layout on the first sheet requires 4 guillotine cuts but max_cuts_per_sheet is 3: the locked placements cannot be retained within the cut budget` |
| `422` | 单件成品即使放在空白原纸上也无法在 `max_cuts_per_sheet` 内形成合法树（已开过的余料均已超限），`detail` 明确指出该成品与所需刀数，例如：<br>`item 'A' cannot be placed within the max_cuts_per_sheet limit of 1: even a blank stock sheet requires 2 guillotine cuts (remaining offcuts narrower than the kerf aside)` |
| `422` | `max_cuts_per_sheet` 不是正整数（`0`、负数、小数等），`detail` 含具体字段 |

## 本地开发

```bash
pip install -r requirements-dev.txt
pytest                 # 运行测试（含切割树一致性、不重叠、不越界校验）
uvicorn app.main:app --reload
```

## 排版算法说明

- 成品按尺寸从大到小排序，逐件放入所有已开纸张中**最紧凑的空余矩形**（最佳短边适配）；放不下则新开一张原纸。
- 每次放置用铡刀式切分空余矩形，切缝宽度从余料中扣除，因此结果天然可表示为二叉切割树。
- 成品可旋转 90°，引擎自动选择能放下的朝向；两种朝向都可行时优先不旋转。
- 存在 `locked_placements` 时，先在第一张原纸上验证锁定布局：自顶向下递归寻找一条贯穿整个子矩形、并留出恰好 `kerf` 切缝的空刀线，把成品分成两个非空组，直到每片只剩一件；任一层找不到这样的刀线（经典的不可切片排版，如风车形互锁）即判定为 422。候选刀线取自各成品边沿及其前后一个切缝位置，保证可用余料尽量保留为大树叶；递归设有搜索预算上限，异常复杂的布局会被快速、确定性地拒绝。
- 锁定布局分解成功后，其树中的空余叶（响应里的 `waste`）作为余料参与后续最佳适配放置；若某余料放入成品后剩下的窄边不足一个切缝（无法形成合法刀线），则跳过该余料、改放其他余料或新开原纸。
- 给出 `max_cuts_per_sheet` 时，每个候选余料都会先预估放置后该纸切割树新增的 `cut` 节点数（恰好铺满的 0 刀、仅一个方向有可用余料的 1 刀、两个方向都有的 2 刀嵌套切分；窄于一个切缝的修边不计刀）。只有「当前刀数 + 新增刀数 ≤ 上限」的余料才会被采用；适配程度（短边余量、剩余面积）相同的候选优先新增刀数更少者。所有旧纸都无合法候选时新开一张原纸；若该件在空白原纸上选哪种朝向所需刀数都超过上限，则返回 422。第一张纸锁定布局所需的刀数在分解完成后立即校验，超限直接 422。
- 测试（`tests/check_plan.py`）对每份方案递归校验：每刀贯穿子矩形、切缝恰好等于 `kerf`、成品不重叠、不越界、树叶与坐标列表一致、利用率吻合、锁定件均在第一张原纸，以及每个 `sheet.cut_count` 与树中 `cut` 节点数一致。
