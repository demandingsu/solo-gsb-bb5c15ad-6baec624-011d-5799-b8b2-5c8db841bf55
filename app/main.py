"""FastAPI application exposing the guillotine cutting plan endpoint."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .models import PlanRequest, PlanResponse
from .packer import CutLimitError, LockedLayoutError, UnfitItemError, build_plan

app = FastAPI(
    title="Guillotine Cutting Plan API",
    version="1.0.0",
    description=(
        "面向小型印刷店的裁纸排版服务：给定原纸尺寸、切缝宽度和成品清单，"
        "按可旋转的铡刀式（每刀贯穿当前子矩形）规则生成多张原纸的排版方案。"
    ),
)


@app.get("/")
def root() -> dict:
    return {
        "service": "guillotine-cut-planner",
        "docs": "/docs",
        "health": "/health",
        "plans": "POST /plans",
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/plans", response_model=PlanResponse)
def create_plan(request: PlanRequest) -> dict:
    items = []
    seen_ids = set()
    quantities = {}
    for position, item in enumerate(request.items, start=1):
        item_id = item.id or f"item-{position}"
        if item_id in seen_ids:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"duplicate item id '{item_id}': please assign explicit "
                    "unique ids to every item"
                ),
            )
        seen_ids.add(item_id)
        items.append((item_id, item.width, item.height, item.quantity))
        quantities[item_id] = item.quantity

    locked = []
    locked_counts: dict[str, int] = {}
    for ref, placement in enumerate(request.locked_placements, start=1):
        item_id = placement.item
        if item_id not in seen_ids:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"locked placement #{ref} references unknown item "
                    f"'{item_id}': it must match an id in items"
                ),
            )
        locked_counts[item_id] = locked_counts.get(item_id, 0) + 1
        if locked_counts[item_id] > quantities[item_id]:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"locked placement #{ref} for item '{item_id}' exceeds its "
                    f"requested quantity ({quantities[item_id]}): locked "
                    "placements count towards the requirement"
                ),
            )
        locked.append((item_id, placement.x, placement.y,
                       placement.rotated, ref))

    try:
        return build_plan(
            stock_width=request.stock.width,
            stock_height=request.stock.height,
            kerf=request.kerf,
            items=items,
            locked=locked,
            max_cuts_per_sheet=request.max_cuts_per_sheet,
        )
    except (UnfitItemError, LockedLayoutError, CutLimitError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
