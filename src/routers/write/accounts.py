"""Accounts write endpoints.

POST / PATCH / DELETE for /ledgers/{ledger_id}/accounts(ledgers 自身除外)。
依赖 `._shared` 里的 _commit_write / _prepare_write / normalize helper /
WRITE 响应表。Endpoint 自身只管参数校验 + mutate lambda 的构造。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import func, select, update

from ...models import SyncChange, UserAccountProjection
from ._shared import *  # noqa: F401,F403 — 集中从 _shared 取所有 symbol

router = APIRouter()


@router.post(
    "/ledgers/{ledger_id}/accounts",
    response_model=WriteCommitMeta,
    responses=_WRITE_RESPONSES,
)
async def create_acc(
    ledger_id: str,
    req: WriteAccountCreateRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    device_id: str = Header(default="web-console", alias="X-Device-ID"),
    _scopes: set[str] = Depends(_WRITE_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WriteCommitMeta:
    payload = req.model_dump(mode="json")
    ledger, replay = _prepare_write(
        db=db,
        current_user=current_user,
        ledger_external_id=ledger_id,
        required_roles=_OWNER_ONLY_ROLES,
        idempotency_key=idempotency_key,
        device_id=device_id,
        method=request.method,
        path=request.url.path,
        payload=payload,
    )
    if replay:
        return replay
    mutate_payload = _payload_with_actor(payload, current_user, ledger=ledger)
    return await _commit_write(
        request=request,
        db=db,
        current_user=current_user,
        ledger=ledger,
        base_change_id=req.base_change_id,
        request_payload=payload,
        idempotency_key=idempotency_key,
        device_id=device_id,
        audit_action="web_account_create",
        mutate=lambda snapshot: create_account(snapshot, mutate_payload),
    )


@router.patch(
    "/ledgers/{ledger_id}/accounts/{account_id}",
    response_model=WriteCommitMeta,
    responses=_WRITE_RESPONSES,
)
async def update_acc(
    ledger_id: str,
    account_id: str,
    req: WriteAccountUpdateRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    device_id: str = Header(default="web-console", alias="X-Device-ID"),
    _scopes: set[str] = Depends(_WRITE_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WriteCommitMeta:
    payload = req.model_dump(mode="json", exclude_unset=True)
    ledger, replay = _prepare_write(
        db=db,
        current_user=current_user,
        ledger_external_id=ledger_id,
        required_roles=_OWNER_ONLY_ROLES,
        idempotency_key=idempotency_key,
        device_id=device_id,
        method=request.method,
        path=request.url.path,
        payload=payload,
    )
    if replay:
        return replay
    _validate_autorepay_patch(
        db, current_user=current_user, account_id=account_id, payload=payload)
    mutate_payload = _payload_with_actor(payload, current_user, ledger=ledger)
    return await _commit_write(
        request=request,
        db=db,
        current_user=current_user,
        ledger=ledger,
        base_change_id=req.base_change_id,
        request_payload=payload,
        idempotency_key=idempotency_key,
        device_id=device_id,
        audit_action="web_account_update",
        mutate=lambda snapshot: (update_account(snapshot, account_id, mutate_payload), account_id),
    )


def _validate_autorepay_patch(
    db: Session, *, current_user: User, account_id: str,
    payload: dict,
) -> None:
    """自动还款配置的**写前**校验(0022)。

    校验必须在**写库之前**做,不能靠调度时才发现问题 ——
    `payment_due_day=0` 会让调度永远不触发,用户几个月后才发现「一直
    没还过」,那时候已经很难归因到某次保存。

    只在**启用**时校验;暂停不需要合法配置(「暂停」语义上就是把配置留着
    不动,哪怕此刻它是脏的)。

    禁用时若显式清了扣款账户,一并清掉 `autorepay_last_period` ——
    否则重新启用时会因为「本期已还过」被跳过。
    """
    enabled = payload.get("autorepay_enabled")
    if enabled is not True:
        # 暂停时**保留**配置(用户「这个月先不还」期望关掉,不是删掉重填)。
        # 但若显式把扣款账户清空了,`autorepay_last_period` 要一并清掉 ——
        # 否则重新启用时会因为「本期已还过」被跳过,用户会以为绑定失败。
        if enabled is False and payload.get("autorepay_from_account_id") is None:
            from ...models import UserAccountProjection as _UA

            db.execute(
                update(_UA)
                .where(_UA.user_id == current_user.id)
                .where(_UA.sync_id == account_id)
                .values(autorepay_last_period=None)
            )
        return
    from ...services.credit_card.config import (
        AutoRepayConfigError,
        validate_autorepay_config,
    )

    try:
        validate_autorepay_config(
            db,
            user_id=current_user.id,
            card_account_sync_id=account_id,
            from_account_sync_id=payload.get("autorepay_from_account_id"),
        )
    except AutoRepayConfigError as e:
        # 转成 400 而不是让它冒成 500 —— 配置写错了是用户的输入问题,
        # 而且 500 会让前端显示「服务器错误」,用户完全不知道错在哪。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"write validation failed: {e}",
        ) from e


@router.delete(
    "/ledgers/{ledger_id}/accounts/{account_id}",
    response_model=WriteCommitMeta,
    responses=_WRITE_RESPONSES,
)
async def delete_acc(
    ledger_id: str,
    account_id: str,
    req: WriteEntityDeleteRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    device_id: str = Header(default="web-console", alias="X-Device-ID"),
    _scopes: set[str] = Depends(_WRITE_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WriteCommitMeta:
    payload = req.model_dump(mode="json")
    ledger, replay = _prepare_write(
        db=db,
        current_user=current_user,
        ledger_external_id=ledger_id,
        required_roles=_OWNER_ONLY_ROLES,
        idempotency_key=idempotency_key,
        device_id=device_id,
        method=request.method,
        path=request.url.path,
        payload=payload,
    )
    if replay:
        return replay
    mutate_payload = _payload_with_actor(payload, current_user, ledger=ledger)
    return await _commit_write(
        request=request,
        db=db,
        current_user=current_user,
        ledger=ledger,
        base_change_id=req.base_change_id,
        request_payload=payload,
        idempotency_key=idempotency_key,
        device_id=device_id,
        audit_action="web_account_delete",
        mutate=lambda snapshot: (delete_account(snapshot, account_id, mutate_payload), account_id),
    )




@router.post(
    "/ledgers/{ledger_id}/accounts/{account_id}/autorepay/run",
    response_model=WriteCommitMeta,
    responses=_WRITE_RESPONSES,
)
async def run_autorepay_now(
    ledger_id: str,
    account_id: str,
    request: Request,
    device_id: str = Header(default="web-console", alias="X-Device-ID"),
    _scopes: set[str] = Depends(_WRITE_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WriteCommitMeta:
    """**手动**触发一次这张卡的自动还款。

    不是「强制还款」—— 仍然走完整的判断链(今天是不是还款日、本期是否
    已还过、应还多少、扣款账户余额够不够)。所以:

    - 今天不是还款日 → `skipped_not_due`
    - 本期已还过 → `skipped_already_repaid`
    - 余额不足 → 部分还款
    - 用户已手动还清 → `skipped_zero_outstanding`

    存在的意义是**调度被禁用时的兜底**:多进程部署会自动关掉调度器
    (`credit_card/scheduler.py::detect_multi_process`),这时用户仍可以
    手动跑一次。也用于验证配置是否生效。
    """
    from datetime import date

    from ...services.credit_card.repay import repay_one
    from ...services.credit_card.selfcall import make_self_call

    card = db.scalar(
        select(UserAccountProjection).where(
            UserAccountProjection.user_id == current_user.id,
            UserAccountProjection.sync_id == account_id,
        )
    )
    if card is None:
        raise HTTPException(status_code=404, detail="account not found")

    outcome = repay_one(
        db, user_id=current_user.id, card=card, today=date.today(),
        self_call=make_self_call(),
    )
    return WriteCommitMeta(
        ledger_id=ledger_id,
        base_change_id=0,
        new_change_id=db.execute(
            select(func.coalesce(func.max(SyncChange.change_id), 0))
            .where(SyncChange.ledger_id == ledger_id)
        ).scalar() or 0,
        server_timestamp=datetime.now(timezone.utc),
        idempotency_replayed=False,
        entity_id=outcome.tx_sync_id or account_id,
    )
