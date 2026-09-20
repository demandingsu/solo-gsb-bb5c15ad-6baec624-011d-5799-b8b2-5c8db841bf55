"""Request/response schemas for the cutting plan API."""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

# Sanity limits guarding the service against absurd workloads.
MAX_DIMENSION = 100_000.0
MAX_KERF = 1_000.0
MAX_ITEM_TYPES = 500
MAX_QUANTITY = 100_000
MAX_TOTAL_PIECES = 20_000


# ---------------------------------------------------------------------------
# request
# ---------------------------------------------------------------------------

class Stock(BaseModel):
    width: float = Field(gt=0, le=MAX_DIMENSION, description="原纸宽度")
    height: float = Field(gt=0, le=MAX_DIMENSION, description="原纸高度")


class Item(BaseModel):
    id: Optional[str] = Field(
        default=None, min_length=1, max_length=64,
        description="成品标识，缺省时自动生成 item-1、item-2 …",
    )
    width: float = Field(gt=0, le=MAX_DIMENSION, description="成品宽度")
    height: float = Field(gt=0, le=MAX_DIMENSION, description="成品高度")
    quantity: int = Field(ge=1, le=MAX_QUANTITY, description="需求数量")


class LockedPlacement(BaseModel):
    item: str = Field(min_length=1, max_length=64,
                      description="预先定位成品的标识，须出现在 items 中")
    x: float = Field(ge=0, le=MAX_DIMENSION,
                     description="左上角 X 坐标（原纸左上角为原点）")
    y: float = Field(ge=0, le=MAX_DIMENSION, description="左上角 Y 坐标")
    rotated: bool = Field(default=False, description="是否旋转了 90 度")


class PlanRequest(BaseModel):
    stock: Stock = Field(description="原纸尺寸")
    kerf: float = Field(default=0.0, ge=0.0, le=MAX_KERF,
                        description="切缝宽度，每次切割损耗的材料宽度")
    max_cuts_per_sheet: Optional[int] = Field(
        default=None, ge=1,
        description=(
            "每张原纸允许的最大刀数（按 cut_tree 中 cut 节点计数），"
            "缺省表示不限制"
        ),
    )
    items: list[Item] = Field(min_length=1, max_length=MAX_ITEM_TYPES,
                              description="成品尺寸及数量")
    locked_placements: list[LockedPlacement] = Field(
        default_factory=list, max_length=MAX_TOTAL_PIECES,
        description=(
            "预先定位、必须原样保留的成品（均位于第一张原纸）。"
            "锁定数量计入对应 item 的需求数量；布局必须能在给定 kerf 下"
            "递归分解为铡刀切割树。"
        ),
    )

    @model_validator(mode="after")
    def _check_request(self) -> "PlanRequest":
        ids = [item.id for item in self.items if item.id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("item ids must be unique")
        total = sum(item.quantity for item in self.items)
        if total > MAX_TOTAL_PIECES:
            raise ValueError(
                f"total piece count {total} exceeds the limit of {MAX_TOTAL_PIECES}"
            )
        return self


# ---------------------------------------------------------------------------
# response
# ---------------------------------------------------------------------------

class Placement(BaseModel):
    item: str = Field(description="成品标识")
    x: float = Field(description="左上角 X 坐标（原纸左下角为原点）")
    y: float = Field(description="左上角 Y 坐标")
    width: float = Field(description="放置后的宽度（旋转后为原高度）")
    height: float = Field(description="放置后的高度")
    rotated: bool = Field(description="是否旋转了 90 度")
    locked: bool = Field(default=False,
                         description="是否为请求中预先定位（locked）的成品")


class ItemNode(BaseModel):
    type: Literal["item"] = "item"
    item: str
    x: float
    y: float
    width: float
    height: float
    rotated: bool


class WasteNode(BaseModel):
    type: Literal["waste"] = "waste"
    x: float
    y: float
    width: float
    height: float


class CutNode(BaseModel):
    """An internal node: one guillotine cut spanning the whole rectangle.

    "vertical" cuts produce a left (first) and right (second) child,
    "horizontal" cuts a top (first) and bottom (second) child.  The second
    child starts exactly ``kerf`` units after the first child ends.
    """

    type: Literal["cut"] = "cut"
    direction: Literal["vertical", "horizontal"]
    x: float
    y: float
    width: float
    height: float
    children: list["TreeNode"] = Field(min_length=2, max_length=2)


TreeNode = Annotated[
    Union[CutNode, ItemNode, WasteNode],
    Field(discriminator="type"),
]
CutNode.model_rebuild()


class SheetPlan(BaseModel):
    index: int = Field(description="原纸序号，从 0 开始")
    width: float
    height: float
    utilization: float = Field(ge=0.0, le=1.0,
                               description="成品面积 / 原纸面积")
    cut_count: int = Field(ge=0, description="切割树中的 cut 节点（刀）数")
    placements: list[Placement]
    cut_tree: TreeNode = Field(description="与 placements 坐标一致的二叉切割树")


class Summary(BaseModel):
    sheet_count: int
    piece_count: int
    used_area: float
    stock_area: float
    utilization: float


class PlanResponse(BaseModel):
    stock: Stock
    kerf: float
    sheets: list[SheetPlan]
    summary: Summary
